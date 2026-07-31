# Wiki Task Resolution (WTR) — interactive resolve

> Status: spec-locked 2026-06-14 · Owner: PO (this session) · ID prefix: `WTR`
>
> 문제: 현재 `/wiki/tasks` resolve 버튼은 `PATCH /wiki/tasks/{slug}` `{resolved}`
> 한 줄로 frontmatter의 `resolved` bool만 뒤집는다. `conflict`/`open_question`
> task는 **아무 작업 없이 사라진다** — 모순은 그대로 남고, 질문엔 답이 안 달린다.
> 즉 지금 resolve는 "해결"이 아니라 "숨김"이다. 이 스펙은 kind별로 의미 있는
> 입력 + write-back을 추가한다.

관련 코드: `orthus/wiki/consolidate.py`(task 생성), `orthus/api/routes/wiki.py`
(resolve endpoint), `orthus/wiki/store.py`(markdown SoR),
`web/src/components/wiki/wiki-tasks-content.tsx`(FE). 설계 원칙은
`AGENTS.md` "설계 원칙" 7번(모순 silent overwrite 금지 → task로 가시화)의
**마무리 단계**를 구현하는 것이다.

---

## 1. Scope / Non-goals

### In scope
- `conflict` task: keep-existing / use-incoming / merge(직접 텍스트) 결정 + 실제
  `WikiClaim` 텍스트 write-back.
- `open_question` task: 답변 입력 → `author_from_qa` compile 경로로 새 `WikiClaim`
  생성, task는 분리 resolve.
- cleanup kind(`stale_audit`/`dedup`/`provenance_fix`/`entity_conflict`): 기존
  one-click resolve 유지(입력 불필요). + 모든 kind 공통 `dismiss`(내용 변경 없이
  닫기).
- 내용 변경(claim write / compile)은 **owner/admin 전용**. dismiss/단순 close/reopen은
  현행 권한 유지.
- resolution 메타데이터(decision/note/resolved_by/resolved_at) frontmatter 영속화.
- 모든 resolve 경로에 `audit()` span.
- FE: kind-aware resolution UI + `KIND_LABEL`에 `entity_conflict` 추가(잠복 버그).

### Non-goals (이번 슬라이스 아님)
- conflict task **생성** 로직 변경(false-positive redaction collision 자체를 줄이는
  것). 단, incoming 텍스트를 구조 저장하는 최소 변경은 포함(아래 §3).
- Agent Work / policy gate 연동.
- 새 Postgres 테이블/마이그레이션(WikiTask는 markdown-only).
- raw-chunk RAG, `/assistant/query` 부활, central write 신규 경로(기존 compile
  파이프 재사용만).
- reopen 후 resolution 메타 자동 롤백(reopen은 `resolved=false`만, 메타는 감사용 보존).

---

## 2. Kind → resolution matrix

| kind | 입력 | write-back | 권한 | 결과 decision |
|---|---|---|---|---|
| `conflict` | decision(keep/incoming/merge) + merge면 텍스트 + optional note | claim 텍스트 갱신(incoming/merge) 또는 무변경(keep) | owner/admin | `keep_existing`/`use_incoming`/`merge` |
| `open_question` | answer 텍스트 + optional note | `author_from_qa` → 새 claim(+consolidate) | owner/admin | `answered` |
| `stale_audit`/`dedup`/`provenance_fix`/`entity_conflict` | 없음 | 없음(task flip만) | 현행(session user) | `cleanup` |
| 모든 kind | optional note | 없음 | 현행(session user) | `dismissed` |

`reopen`(resolved→open): 현행 권한, 메타 보존, `audit("wiki.task.reopen")`.

---

## 3. Data model

`WikiTask`(`orthus/schemas/canonical.py`)에 **optional 필드만** 추가(전부 기본 None →
기존 task 로드 하위호환). 마이그레이션 없음.

```python
class WikiTaskResolution(BaseModel):
    decision: Literal[
        "keep_existing", "use_incoming", "merge", "answered", "cleanup", "dismissed"
    ]
    note: str | None = None
    resolved_by: UUID
    resolved_at: datetime
    # compile 결과 추적(open_question answered일 때만)
    produced_claim_slugs: list[str] = Field(default_factory=list)

class WikiTask(BaseModel):
    # ... 기존 필드 ...
    resolved: bool = False
    # conflict (b) same-slug-diff-text일 때 skip된 incoming claim 텍스트.
    # use_incoming write-back을 위해 생성 시점에 구조 저장(description 파싱 금지).
    incoming_claim: str | None = None
    resolution: WikiTaskResolution | None = None
```

- `consolidate.py` conflict case (b) 생성 시 `incoming_claim=c.claim` 채운다(case (a)
  explicit-conflict와 open_question은 None).
- store 직렬화: `_task_to_md`/`_md_to_task`/`_redact_task`(`orthus/wiki/store.py`)에
  새 필드 추가. `incoming_claim`/`resolution.note`는 PII redaction 대상(다른 task
  필드와 동일 정책 — P6/P8 ingest 예외 경로 아님).
- `resolution`은 중첩 모델 → frontmatter에 평탄화 직렬화 또는 YAML 중첩. store의
  기존 직렬화 방식에 맞춘다(executor 판단; round-trip 테스트 필수).

---

## 4. API contract

기존 `PATCH /wiki/tasks/{slug}` `{resolved}`는 **reopen + 하위호환**용으로 유지
(`resolved=true`는 `decision="dismissed"` no-note와 동일 취급, owner/admin 불필요 —
단 conflict/open_question에 대한 내용 변경은 절대 하지 않음). 신규 rich 경로는 별도
endpoint.

### `POST /wiki/tasks/{slug}/resolve`
의존성: `current: AuthenticatedUser = Depends(get_current_user)`.

요청(typed, 분기별 allowlist — extra 금지):
```python
class TaskResolveIn(BaseModel):
    decision: Literal[
        "keep_existing", "use_incoming", "merge", "answered", "cleanup", "dismissed"
    ]
    merged_text: str | None = None   # decision="merge"일 때 필수, 그 외 금지
    answer: str | None = None        # decision="answered"일 때 필수, 그 외 금지
    note: str | None = None          # 모든 decision optional
```

검증/분기:
- `decision ∈ {keep_existing, use_incoming, merge}` → task.kind == `conflict` 강제,
  아니면 422.
- `decision == answered` → task.kind == `open_question` 강제, `answer` 필수.
- `decision == cleanup` → task.kind ∈ cleanup set 강제.
- `decision == dismissed` → 모든 kind 허용, write-back 없음.
- 내용 변경 decision(`use_incoming`/`merge`/`answered`) → `require_node_operator(current)`.
  `keep_existing`/`cleanup`/`dismissed`는 게이트 없음(내용 무변경).
- `use_incoming`: `task.incoming_claim`이 None이면 422("no incoming text recorded").
- write-back은 task와 **같은 scope/owner_id**(`_wiki_scope`)로 수행.

응답: 갱신된 `WikiTask`(resolution 채워짐). 200.

audit span:
- conflict write-back: `audit("wiki.task.resolve")` + `span.add_meta(slug, kind,
  decision, claim_slug, node_id, user_id)`.
- open_question compile: 내부 `author_from_qa`가 `audit("wiki.author")` 자체 span을
  남김 + 바깥 `wiki.task.resolve` span에 `produced_claim_slugs` 기록.
- reopen(PATCH): `audit("wiki.task.reopen")`.

### conflict write-back 상세
- `keep_existing`: claim 무변경. `resolution.decision="keep_existing"`, `resolved=true`.
- `use_incoming`: `existing = store.load_claim(related[0])`;
  `store.write_claim(existing.model_copy(update={"claim": task.incoming_claim}), ...)`.
  `last_reviewed=today`. produced_claim_slugs=[related[0]].
- `merge`: 위와 동일하되 `claim=merged_text`.
- 셋 다 claim의 `conflicting` 필드는 건드리지 않음(범위 밖).

### open_question 상세
- `answer` 텍스트로 `author_from_qa(user_id, question=task.description, answer=answer,
  source_refs=[related→WikiSourceRef], scope=<task scope>, owner_id=...)` 호출.
  → 내부 distill 없이 question+answer를 claim으로 compile + consolidate.
- 반환된 claim slug들을 `resolution.produced_claim_slugs`에 기록.
- consolidate가 새 conflict task를 부생성할 수 있음(정상 — 답변이 기존 claim과 충돌하면
  또 가시화). 그대로 둔다.
- task `resolved=true`, `decision="answered"`.
- `author_from_qa` default `scope="personal"` → company task엔 `scope="company"` 명시.

---

## 5. Frontend (`wiki-tasks-content.tsx`)

`TaskDetail`을 kind-aware로 분기. `api.ts`에 `resolveWikiTask(slug, body)` 추가.

- `KIND_LABEL`에 `entity_conflict: "entity conflict"` 추가(현재 누락 → 빈 칩 렌더 버그).
- conflict:
  - existing claim 텍스트 vs `incoming_claim` 나란히 표시(있을 때).
  - radio: keep existing / use incoming(incoming 없으면 disabled) / merge.
  - merge 선택 시 textarea(기본값 existing 또는 incoming 프리필).
  - optional note 입력.
  - "resolve" → `POST .../resolve`. owner/admin 아니면 내용변경 버튼 disabled +
    "dismiss"만 허용(403 사전 방지; 서버가 최종 게이트).
- open_question:
  - answer textarea(필수) + optional note.
  - "answer & resolve" → `decision="answered"`. 성공 시 produced claim slug를 결과로 표시.
- cleanup kinds: 현행 단일 resolve 버튼 유지(`decision="cleanup"`).
- 모든 kind: "dismiss"(note optional) 버튼.
- resolved task: 기존 reopen + resolution 메타(decision/note/resolved_at) 표시.
- 권한: `/auth/me`의 role로 내용변경 컨트롤 gate. role 모르면 보수적으로 dismiss만.
- 모바일 parity 유지(P5: 44px tap target, `<760px` compact, 가로 스크롤 없음).

---

## 6. 권한 / 보안

- 내용 변경(claim write/compile)은 `require_node_operator` = `auth_mode=="session"`에서
  role ∈ {owner, admin}. demo/jwt 모드는 통과(로컬/테스트).
- personal node: task는 owner 자신의 scope. `_wiki_scope`가 owner_id를 강제하므로
  타 tenant claim write 불가(fail-closed).
- company node: conflict/open_question는 company scope. owner/admin write-back =
  사람이 리뷰한 central wiki write(원칙 7 허용 — LLM-only 아님).
- redaction: 새 `incoming_claim`/`resolution.note`는 task 저장 시 기존 redact 정책
  통과. 단 `use_incoming`/`merge`/`answered`로 **claim에 쓰는 텍스트**는 claim 저장
  경로(`write_claim`)의 기존 redaction을 그대로 탄다.

---

## 7. Success criteria (검증 루프)

backend pytest(`make test`, orthus_test DB):
1. conflict `keep_existing`: claim 무변경 + task.resolved + decision 기록.
2. conflict `use_incoming`: claim.claim == incoming_claim, last_reviewed=today,
   resolution.produced_claim_slugs.
3. conflict `merge`: claim.claim == merged_text.
4. conflict인데 `merged_text` 없이 merge → 422. incoming None인데 use_incoming → 422.
5. open_question `answered`: author_from_qa 호출 → 새 claim 존재, task.resolved,
   produced_claim_slugs 비어있지 않음.
6. cleanup kind `cleanup`: task flip만, claim 무변경.
7. `dismissed`: 모든 kind 닫힘, write-back 없음.
8. 권한: session role=member가 use_incoming/merge/answered → 403. dismiss/cleanup →
   허용. owner/admin → 허용.
9. reopen: resolved→open, resolution 메타 보존, audit("wiki.task.reopen").
10. WikiTask frontmatter round-trip(새 필드 포함) write→load 동일.
11. 회귀: 기존 검증 게이트 reject 5종(`tests/unit/test_validate.py`,
    `test_structured.py`) 무영향. 기존 `patch_wiki_task` reopen 동작 유지.
12. audit: `wiki.task.resolve` span이 decision/slug 기록.

frontend: `pnpm --dir web lint && pnpm --dir web build` clean.

browser QA(로컬, 공통 체크리스트): company node에서 conflict task 1건 use_incoming
resolve → claim 텍스트 실제 변경 확인. open_question 1건 answer → 새 claim 확인.
member role로 내용변경 버튼 disabled 확인. 모바일 390×844 레이아웃 확인. 증거 기록.

---

## 8. 구현 슬라이스 (병렬)

- **WTR.1 (schema/store)**: `WikiTask` + `WikiTaskResolution` 필드, `_task_to_md`/
  `_md_to_task`/`_redact_task`, frontmatter round-trip 테스트. consolidate.py conflict
  (b)에 `incoming_claim` 채우기. → 다른 슬라이스의 선행(스키마 계약).
- **WTR.2 (backend resolve)**: `POST /wiki/tasks/{slug}/resolve`, `TaskResolveIn`,
  분기/권한/write-back/compile/audit, reopen audit. pytest 1–12.
- **WTR.3 (frontend)**: `TaskDetail` kind-aware UI, `api.ts` 메서드, `KIND_LABEL`
  entity_conflict, role gate, 모바일. lint+build.
- **WTR.4 (verify)**: 전체 pytest + FE build + browser QA 증거 + diff review. 실패 시
  해당 슬라이스로 회귀.

WTR.1이 스키마 계약을 고정하면 WTR.2/WTR.3은 이 API contract(§4) 기준으로 병렬 진행.
