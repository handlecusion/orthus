# LLM Wiki — Phase 1 설계 (self-authoring 그라운딩 레이어)

> status: W0–W4 완료 / main 머지 (PR #1, commit `2750a62`, 2026-05-28)
> updated: 2026-05-28
> confidence: high (W0–W4 구현 완료, 128 tests passed)

이 문서는 orthus-ai Phase 1에 **에이전트가 self-author하는 내부 지식 레이어(LLM Wiki)**를 추가하는 설계서다. Andrej Karpathy의 LLM Wiki 방법론(`<local>/wiki` 로컬 구현 참조)을 제품 내부 그라운딩 레이어로 이식한다.

내부 문서(비공개) / `data-model.md` / `operations.md`는 **더 큰 제품 비전(LangGraph/Neo4j/persona)** 문서이며 본 레이어를 다루지 않는다. 본 문서가 LLM Wiki 레이어의 canonical 참조다. Phase 1 운영 규칙은 `AGENTS.md`, 전체 작업 스펙은 `agent-prompt-p1-archive-secretary.md` 참조.

> **P8 예고 (2026-06-10):** 내부 문서(비공개)에 따라 wiki
> store/compile은 central 단일 런타임으로 통합되고, personal wiki
> source/claim/page는 owner-only row-level 경계(wiki-store는 owner별
> 네임스페이스)로 central에 저장된다. distill/consolidate 파이프라인 계약
> (결정론 오케스트레이션 + LLM 압축/추출, compiled page 전용 grounding)은
> 불변이며 실행 위치만 central로 모인다. P8 구현 slice merge 전까지 본 문서
> 계약이 현행이다.

---

## 0. 포지셔닝

- **LLM wiki = 에이전트가 self-author하는 내부 지식 레이어.** 사용자 직접 접근 불가.
- 역할: 비서/wiki Q&A의 **그라운딩 + 추론 근거 소스**. 사용자는 답변과 근거만 본다.
- **compounding:** 세션을 넘어 누적. ingest/Q&A마다 claim·page를 갱신 → 답 품질↑, 재발견 비용↓.
- 기존 raw-chunk RAG(`orthus/wiki/rag.py`)는 **제거**한다(W3). 그라운딩은 compiled wiki page 전용.

핵심 구분(이전 RAG와의 차이):

| | 구 RAG wiki | LLM wiki (본 설계) |
|---|---|---|
| 지식 위치 | corpus chunk(정적) | self-author한 claim·page(성장) |
| 동작 | 검색→그라운딩 | 저작·큐레이션 + 검색→그라운딩 |
| 세션 누적 | 없음 | 있음 (compounding) |
| 사용자 접근 | (질문) | 불가, 내부 전용 |

---

## 1. 흐름 변경 (단방향 → 메모리 레이어 삽입)

**이전 (단방향, RAG):**
```
Notion → 에디터(SoR) → corpus(chunk/embed/pgvector) → {RAG wiki, 비서 그라운딩}
```

**이후 (corpus=raw 레이어, LLM wiki=메모리 레이어):**
```
Notion ─┐
에디터 ─┼→ corpus(raw chunks) ──→ [distill → consolidate] ──→ LLM wiki(claims+pages)
Q&A ────┘   (=local wiki의 raw/+sources/)        (self-author, 내부전용)   │
                                                                          ▼ grounding(유일)
                                          비서/wiki 답변 (근거 = page + claim provenance)
```

- 에디터는 여전히 SoR. **corpus는 raw 레이어로 강등**, LLM wiki가 메모리 레이어.
- 답변 그라운딩은 **wiki page 전용**. raw chunk 직접 응답 경로 없음. corpus_chunk는 provenance/검증용으로만 잔존.
- LLM wiki는 embedding/vector DB에 의존하지 않는 **compiled memory layer**다.
  Embedding은 corpus/wiki 검색을 위한 보조 인덱스이며, distill/compile 엔진이 아니다.
  small/local mode에서는 retrieval index를 keyword/file search 기반으로 대체 가능해야 한다.

---

## 2. 레이어 매핑 (local wiki ↔ orthus-ai)

| Karpathy local wiki | orthus-ai 대응 |
|---|---|
| `raw/` (불변 원본) | `documents` + `corpus_chunks` (에디터 SoR + 청크) |
| `sources/` (소스별 요약) | `WikiSource` (corpus doc / Q&A 세션 / 대화 단위) |
| `claims/` (원자 주장, provenance·confidence·conflict) | `WikiClaim` |
| `wiki/` (개념 중심 정전 페이지) | `WikiPage` |
| `tasks/` (미해결/모순) | `WikiTask` |
| `index.md` / `log.md` | `wiki-store/index.md` / `log.md` (+ audit span) |

규약(섹션 필드, provenance 명시, 모순 silent overwrite 금지)은 local wiki를 그대로 차용한다.

---

## 3. 저장 substrate (Hybrid: markdown SoR + pgvector)

- **markdown 파일이 SoR** — git 버전관리, Obsidian 호환, 사람이 감사 가능.
- **Postgres는 인덱스/검색/그래프/감사** — 기존 pgvector·audit 인프라 재사용.

```
wiki-store/                  # repo 내, 버전관리(gitignore 아님)
  index.md  log.md           # 네비 맵 + append-only ops 로그
  sources/<slug>.md          # WikiSource
  claims/<slug>.md           # WikiClaim
  wiki/<concept>.md          # WikiPage
  tasks/<slug>.md            # WikiTask
```

각 파일 frontmatter = §5 canonical 모델 직렬화. 본문은 local wiki 섹션 규약.

---

## 4. 데이터 모델 (Postgres — 인덱스/검색/그래프)

> `data-model.md` §6 버전 규약(UUID v4, `TIMESTAMPTZ` UTC, `schema_version`) 준수. Alembic 마이그레이션.

```sql
-- LLM wiki 인덱스 (markdown이 SoR, 아래는 검색/그래프/감사용 미러)
CREATE TABLE wiki_pages (
  page_id        UUID PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE,        -- canonical key = 파일명
  kind           TEXT NOT NULL CHECK (kind IN ('source','claim','page','task')),
  path           TEXT NOT NULL,               -- wiki-store/ 상대 경로 (SoR markdown)
  title          TEXT NOT NULL,
  confidence     TEXT CHECK (confidence IN ('high','medium','low')),  -- claim/page만
  content_hash   TEXT NOT NULL,               -- 본문 해시 (재인덱싱 판단)
  schema_version INT  NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE wiki_chunks (
  chunk_id     UUID PRIMARY KEY,
  page_id      UUID NOT NULL REFERENCES wiki_pages(page_id) ON DELETE CASCADE,
  ordinal      INT  NOT NULL,
  content      TEXT NOT NULL,
  embedding_id UUID REFERENCES embeddings(embedding_id),  -- 기존 pgvector 재사용 (kind='wiki_chunk')
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_wiki_chunks_page ON wiki_chunks(page_id, ordinal);

-- backlink/relation 그래프 + provenance 체인
CREATE TABLE wiki_links (
  src_page_id  UUID NOT NULL REFERENCES wiki_pages(page_id) ON DELETE CASCADE,
  dst_slug     TEXT NOT NULL,                 -- [[slug]] 대상, 미생성(dangling) 허용
  rel          TEXT NOT NULL CHECK (rel IN ('backlink','supports','conflicts','derived_from')),
  PRIMARY KEY (src_page_id, dst_slug, rel)
);
```

- `embeddings.kind` CHECK에 `'wiki_chunk'` 추가(corpus와 동일 1024-d 파이프라인 재사용).
- provenance 체인: `WikiClaim` → (`supports`) `WikiSource` → corpus `chunk_id`(frontmatter 기록). 답변 시 끝까지 추적 가능.

---

## 5. canonical 스키마 (`orthus/schemas/canonical.py` 확장)

거대 통합 스키마 금지(AGENTS.md 원칙 2). 슬롯별 Pydantic v2 모델.

```python
class WikiSource(BaseModel):
    slug: str
    title: str
    source_type: Literal["corpus_doc", "qa_session", "conversation"]
    source_ref: str                 # doc_id / query_id 등
    ingested_at: datetime
    summary: str
    key_concepts: list[str]
    key_claims: list[str]           # claim slug
    terminology: list[str]
    related_pages: list[str]
    open_questions: list[str]
    confidence_notes: str | None = None

class WikiClaim(BaseModel):
    slug: str
    claim: str
    supporting: list[str]           # source slug / corpus chunk ref
    conflicting: list[str]
    confidence: Literal["high", "medium", "low"]
    last_reviewed: date
    related_pages: list[str]
    evidence: str
    notes: str | None = None

class WikiPage(BaseModel):
    slug: str
    title: str
    definition: str
    overview: str
    relations: list[str]
    evidence: list[str]
    competing: list[str]            # 경쟁 해석
    open_questions: list[str]
    sources: list[str]
    backlinks: list[str]

class WikiTask(BaseModel):
    slug: str
    kind: Literal["open_question", "conflict", "stale_audit", "dedup", "provenance_fix"]
    description: str
    related: list[str]
    created_at: datetime
    resolved: bool = False
```

---

## 6. 파이프라인 모듈 (`orthus/wiki/` — 구 rag.py 대체)

**LLM은 압축/추출만, 오케스트레이션·검증은 결정론 코드**(AGENTS.md 원칙 1). 모든 외부 호출은 `audit()` span(원칙 3).

| 모듈 | 책임 |
|---|---|
| `store.py` | markdown read/write(SoR) + Postgres 인덱스 동기화 |
| `distill.py` | raw(corpus doc / Q&A) → `WikiSource` + `WikiClaim[]` (LLM 추출, 코드가 검증·dedupe) |
| `consolidate.py` | claim → `WikiPage` 병합. **모순 시 silent overwrite 금지 → `WikiTask` 생성**. source `open_questions`도 `WikiTask(kind="open_question")`로 승격 |
| `retrieve.py` | 그라운딩 검색 — wiki page 전용, provenance 동봉 |
| `author.py` | 트리거 진입점 (오케스트레이션, audit span) |

audit span 이름: `wiki.distill`, `wiki.consolidate`, `wiki.retrieve`, `wiki.author`, `wiki.embed`(페이지/claim 본문 임베딩).

Chat provider와 embedding provider는 별도 slot이다. `ORTHUS_LLM=solar`는
`wiki.distill` 같은 compile 호출을 Solar로 보내지만, `ORTHUS_EMBEDDING`은
wiki/corpus retrieval index provider만 고른다. wiki 생성 계약은 provider나
vector index에 묶이지 않는다 — 어댑터 교체가 설정 변경으로 끝나는 이유다.

---

## 7. 저작 트리거

- **T1 — corpus ingest 시** (Notion 임포트 / 에디터 저장) → `distill` → `consolidate`.
- **T2 — 비서/wiki Q&A 후** → 답변·근거를 claim으로 누적. **compounding 핵심.**
  - 필터: 비서 **검증 게이트 통과** + 비-trivial 질의만 distill (노이즈 방지).
- **T3 (후순위, W5)** — 대화/사용 로그 → 관심사 맵 (local wiki `user-interest-map` 패턴). 스코프 큼.
- **주기 refine/audit job** — stale·conflict 정리, `WikiTask` 처리.

---

## 8. 그라운딩 (retrieve)

- `retrieve.py`: `wiki_chunks` pgvector top-k → 관련 `WikiPage`/`WikiClaim` → 답변에 **provenance(page slug + source + corpus chunk) 동봉**.
- **raw-chunk RAG(구 `rag.py`) 제거.** corpus_chunk는 provenance/검증용으로만 잔존, 응답 그라운딩에 직접 사용 금지.
- cold-start: T1이 corpus ingest 시 wiki를 채우므로, 답변 시점엔 wiki에 근거 존재.

---

## 9. 마일스톤 (Phase 1 수정 빌드)

| ID | 내용 | verify |
|---|---|---|
| **W0** ✅ | AGENTS.md hard constraint + agent-prompt 스펙 개정 + 본 문서 작성 | 문서 diff (완료) |
| W1 ✅ | §5 스키마 + §3 markdown layout + §4 Postgres 마이그레이션 + `wiki-store/` init | `make migrate` green (commit `359bbf6`) |
| W2 ✅ | `distill`+`consolidate` (corpus→claim→page), 모순→task, open question→task | corpus 1문서 E2E (commit `0a480cb`, open-question task update 2026-05-31) |
| **W3** ✅ | `rag.py` 삭제 + `retrieve.py`(wiki-grounding) + `qa.py`(`ask`) 신설, `/wiki/ask` 교체, T1 author-on-save(`documents.py`) 연결 (**원자적**). `WikiAnswer.sources`는 이제 `WikiSourceRef`(page_slug/provenance). 비서(assistant) 그라운딩은 여전히 `corpus.search`(SQL 컴파일용 그라운딩) — wiki-grounding 교체는 별도 후속(opt). | 답변에 page provenance, 구 raw-chunk RAG 경로 제거 (commit `ef8c212`) |
| **W4** ✅ | Q&A 후 self-author 트리거 (T2, compounding). 그라운딩된 답변(`sources` 비어있지 않음)을 `author_from_qa`가 결정론적으로(추가 LLM 호출 없음) `qa_session` source + low-confidence claim 으로 wiki에 적재 → `consolidate`로 페이지/백링크/임베딩 갱신(T1과 동일 경로). slug 은 정규화 질문 해시(`qa-<sha256[:8]>`)로 유도해 동일 질문 재질의 시 in-place 덮어쓰기(중복/자기-conflict 없음); 동일 질문·다른 답변만 정당한 conflict 경로. `related_pages`는 page-kind 그라운딩만 채택(자기증폭 가드). `ask(learn=True)` 기본, `learn=False`로 비활성. | 그라운딩 답변이 qa claim으로 적재, 재질의 멱등, 빈 그라운딩/learn=False 무적재 (commit `bc28c0c`) |
| W5 (opt / 보류) | 대화/사용 로그 ingest (T3) | 관심사 맵 생성 — v2 scope에서 P2.3으로 흡수 |
| W6 (opt / 보류) | wiki→에디터 write-back (사용자 승격) — Notion write-back(P2.4)과 통합 검토 | 보류 |

> W3 주의: `rag.py` 삭제는 `/wiki/ask` 그라운딩 교체와 **같은 마일스톤에서 원자적으로**. 선행 deprecation 단계 없음.

> **전체 상태 (2026-05-28):** W0–W4 전부 main 머지 완료 (PR #1, merge commit `2750a62`). W5/W6는 v2 아키텍처에서 P2.3/P2.4로 흡수·보류.
> **운영 UI (2026-05-31):** `/wiki/tasks`에서 node-local `WikiTask`를 조회하고 resolved 상태를 markdown SoR에 반영한다.

---

## 10. 결정 / default (확정)

- **raw-chunk RAG 즉시 제거** (공존 단계 없음, W3에서 교체와 동시).
- **wiki-store 위치:** repo 내 `wiki-store/`, 버전관리(gitignore 아님). 별도 repo 아님.
- **Q&A self-author 필터:** 검증 게이트 통과 + 비-trivial만 distill.
- **redaction:** wiki page도 직렬화/저장 전 `redact_pii()` 통과 (operations §2.3, 내부전용이라도).
- **사용자 직접 접근 불가:** wiki는 에이전트 내부 그라운딩 전용.

---

## 11. 미해결 질문

- Q&A self-author "비-trivial" 판정 구체 기준 (길이/신규성/confidence?).
- 주기 refine/audit job 스케줄·트리거 (수동 `make wiki-rebuild` vs 주기).
- T3 대화/사용 로그 수집 범위·redaction 경계 (W5에서 확정).
- W6 write-back UX (사용자 초기 설명 "에디터가 wiki에서 꺼내 씀") — 보류, 별도 결정.

---

## 12. 회귀 / 수용 기준

- **비서 검증 게이트 5종 reject는 불변** (이 레이어 변경과 무관하게 유지).
- 신규 테스트: wiki provenance 체인, 모순→task 생성, redaction, distill/consolidate 멱등.
- W3 수용: `/wiki/ask` 답변이 wiki page provenance 포함 + 구 raw-chunk RAG 경로 부재 확인.
- W4 수용: 같은 질문 2회차에서 누적된 claim이 답변 근거에 반영.
