# Agent-chat 답변 품질 — 원인 분석 + 수정 (2026-07-16)

`/agent-work` 채팅이 저품질 답변을 내던 실제 세션을 역추적해 원인 5개를 코드 수준에서
특정하고 수정한 기록. 재현 세션(prod company node):

| 턴 | 사용자 입력 | 실제 응답 | 무엇이 잘못됐나 |
|---|---|---|---|
| 1 | ai 툴 관련해서 내용 요약해줘 | "AI 영상관련툴에 대한 순위를 정리한 문서가 있으며, 이 문서는 …정보를 제공합니다. (문서 [1], [2], [3] 참조)" | 내용이 아니라 **문서가 있다는 사실**을 요약 |
| 2 | 그거 내용 알려줘 | 턴 1과 **글자 그대로 동일** | 지시어 미해소 → 같은 검색 → 같은 답 |
| 3 | 내용 요약해서 나에게 알려줘 | "앱 설명에 대한 내용을 다룬 문서 (문서 [1])…" 목록 | 문서 목록 나열 |
| 4 | 해당 내용 요약된 거 메일 보내줘 | 또 다른 문서 요약 목록 | **메일 명령이 감지조차 안 됨** |

---

## 1. 원인 (근본 → 표면)

### C1. 위키 페이지 본문이 첫 claim 하나로 영구 고정 — **최대 병목**

`consolidate`는 페이지를 seed할 때 `overview`를 첫 claim의 evidence로 쓰고, 이후 fold
루프는 `evidence`/`sources`/`backlinks`(슬러그 목록)만 병합했다. `definition`/`overview`는
**seed 이후 한 번도 갱신되지 않는다**.

`wiki_chunks`는 페이지 **본문**을 인덱싱하고, `retrieve._merge_candidates`는 페이지를
claim보다 상위로 랭크한다. 따라서 claim 20개가 folding된 페이지도 본문은 첫 문장 하나 —
나머지 19개의 실체는 페이지 단위 검색에 **구조적으로 보이지 않았다**.

실측(company DB): `wiki_chunks` 평균 212자 / 중앙값 210자.

> **범위 정정 (2026-07-17 실측).** 최초 조사에서 이 항목을 "최대 병목"으로 적었으나
> 근거 없는 단정이었다. company 페이지 2,618개의 claim 분포를 재보니:
>
> | 페이지당 claim | 페이지 수 | C1 효과 |
> |---|---|---|
> | 1개 | 1,655 (63.2%) | **없음** (축적할 claim이 없다) |
> | 2–3개 | 304 (11.6%) | 작음 |
> | 4–10개 | 603 (23.0%) | 있음 |
> | 11개+ | 56 (2.1%) | 큼 |
>
> **C1이 실질 효과를 내는 건 약 25%(659개)**이고 63%는 무효다. 특히 재현 사례인
> `ai-영상관련툴`은 claim이 **1개뿐이고 그게 메타 문장**이라 C1으로 고쳐지지 않는다 —
> 그 페이지의 진짜 원인은 아래 **C6(distill fallback)**이다. C1은 여전히 유효한 수정이지만
> "최대 병목"은 아니다.

### C2. T2 학습 자기오염 루프

`answer_from_hits`의 T2 학습(`author_from_qa`)이 **답변 원문을 그대로 claim으로 위키에
재흡수**했다. 그 claim이 다음 질문의 grounding으로 다시 걸리면서 같은 저품질 답이
영원히 되돌아온다.

실측: 학습된 `qa-*` 페이지 179건(인사말 "안녕?" 포함), 그중 위키 청크 23개가 죽은 번호
인용("(문서 [1] 및 [2] 참조)")을 품은 과거 답변이었다. `detect_gap`은 이걸 못 잡는다 —
답변에 "모르겠다" 류 부재 표지가 없어 **충분히 근거 있는 답으로 보이기 때문**이다.

> **인용 판정 주의**: `[N]` 단순 매칭은 오탐한다. 살아 있는 company wiki에는
> "parents[2] path-walking", "framings[1] is consistently B", "slugs[0]/slugs[1]" 같은
> **정상 기술 지식 청크가 35개** 있다. 인용은 항상 공백/`(` 뒤에 오지만 아래첨자는
> 식별자에 바로 붙으므로, `_ANSWER_CITATION`은 `[` 앞의 단어 문자를 negative
> lookbehind로 배제한다. 정리 스크립트도 같은 패턴을 import해 쓴다(판정 단일화).

### C3. 후속질문 지시어가 검색에 도달하지 못함

히스토리는 wiki 답변 프롬프트에 "참고용" 블록으로만 붙고 **retrieval에는 절대 닿지
않는다**(설계 의도). 그래서 "그거 내용 알려줘"는 지시어가 해소되지 않은 문자열 그대로
검색된다. 턴 2가 턴 1과 글자까지 같았던 이유 — 캐시가 아니라(orchestrate는
`allow_cache=False`) **같은 입력 → 같은 검색 → 같은 답**이다.

명령 감지·라우팅·decompose 게이트도 전부 히스토리를 못 본다.

### C4. 메일 명령이 읽기 동사에 삼켜짐

`detect_assistant_command_action`은 `email_send` family를 잡아도, recipient가 없으면
strong이 아니라 판단하고 `_INFO_QUERY_TERMS`("요약") 가드에 걸려 `None`을 반환했다 →
위키 읽기로 강등.

"해당 내용 요약된 거 메일 보내줘"와 "메일 답장 요약해줘"는 **집합 검사로 구분 불가**
(둘 다 메일 단어 + 메일 동사 + 요약을 갖는다). 구분하는 건 **순서**뿐이다.

### C5. 답변 프롬프트가 실체를 요구하지 않음

시스템 프롬프트는 "제공된 passage에서만 답하라"만 말하고 **내용을 요약하라고 말하지
않았다**. `[N] (slug) excerpt` 컨텍스트 형식을 모델이 그대로 흉내 내 "(문서 [1] 참조)"를
출력했고(프롬프트에 없던 동작), passage 자체가 메타 서술이면 메타 서술을 충실히 되풀이했다.

### C6. distill이 문서 요약을 지식으로 승격 — **재현 사례의 실제 원인**

`distill_document`는 LLM이 claim을 0개 뽑으면 `summary`를 claim으로 만드는 fallback을
갖고 있었다(`_should_create_fallback_claim`, `len(summary) >= 20`만 검사). 그런데 claim
0개는 "이 문서엔 검증 가능한 사실이 없다"는 신호이고, `summary`는 **문서에 대한 서술**이다.
결과적으로 fallback은 지식을 보존한 게 아니라 **메타 페이지를 제조**했다.

실측(2026-07-17, company wiki): fallback claim 23개가 **예외 없이 전부** 메타 서술이었다
(전체 claim 7,535개의 0.3%).

```text
"AI 영상관련툴에 대한 순위를 정리한 문서입니다."          ← 재현 사례 답변의 근거
"이 문서는 아틀라스 B2B 전략에 대한 내용을 다루고 있습니다."
"The document outlines the schedule for the week of May 25 …"
```

재현 사례의 원문은 **11자**짜리다 — `AI관련 순위 정리`. 동일 문서·동일 LLM(gpt-4o-mini)
대조 실증:

| | claim | 생성 페이지 | `source.summary` |
|---|---|---|---|
| 수정 전 | 1개 (메타) | `['ai-영상관련툴']` | 보존 |
| 수정 후 | **0개** | **`[]`** | **보존** |

유실 없음: 원문은 corpus에, 요약은 `WikiSource.summary`에 남는다. 사라지는 건 가짜
claim과 그게 만들던 페이지뿐이며, 문서에 실제 내용이 있으면 정상 경로에서 이미 claim이 된다.

---

## 2. 수정

| ID | 수정 | 파일 | 플래그 |
|---|---|---|---|
| C6 | summary→claim fallback 제거 (**재현 사례 해결**) | `orthus/wiki/distill.py` | 없음 (rebuild 필요) |
| C1 | 페이지 `overview`가 folding된 모든 claim 텍스트를 축적 (결정론, 중복제거, 상한 20개/2000자) — 페이지 25%에 효과 | `orthus/wiki/consolidate.py::_merge_overview` | 없음 (rebuild 필요) |
| C2 | T2 학습 게이트 — gap 있음 / 6자 미만 질문 / 번호 인용 답변은 학습 금지 | `orthus/wiki/qa.py::_should_learn` | 없음 (즉시) |
| C2 | 기존 오염 정리 스크립트 | `scripts/wiki/purge_qa_learned_wiki.py` | 운영자 `--apply` |
| C3 | 지시어 해소 재작성 (결정론 프리필터 → LLM 1콜 → fail-open) | `orthus/router/rewrite.py` | `ORTHUS_CHAT_FOLLOWUP_REWRITE_ENABLED` |
| C4 | head-final 규칙 — 문말 주동사가 발송 동사면 명령 | `orthus/agentwork/service.py::_email_action_is_head_final` | 없음 (즉시) |
| C5 | 실체 요구 + 문서 카탈로그 금지 + 번호 인용 금지 | `orthus/wiki/qa.py::_SYSTEM` | 없음 (즉시) |

### C1 상세 — 결정론 유지

`consolidate`는 LLM을 쓰지 않는다(AGENTS.md §7: 결정론 코드가 오케스트레이션, LLM은
압축/추출만). `_merge_overview`도 순수 텍스트 병합이다 — 합성이 아니라 **축적**.

```markdown
## Conceptual Overview
- Hugging Face는 AI 모델을 유통·실행·협업하게 해주는 플랫폼이다.
- 알리바바 qwen 오픈소스는 이미지와 동영상에서 가장 잘한다.
- LongCat-Video는 오디오로 영화를 만들어주는 오픈소스 툴이다.
```

(위는 claim 3개를 `_seed_page` + fold에 실제로 태워 얻은 출력이다 — 이전 코드였다면 첫
claim의 evidence 한 문장만 남았다. 재실행해도 동일: 텍스트 기준 중복제거로 멱등.)

재폴드 시 텍스트 기준 중복제거라 **멱등**이다. 허브 페이지 청크 폭주를 막기 위해
20개/2000자 상한. 이 변경 이전에 쓰인 단문 overview는 첫 항목으로 보존된다.

### C4 상세 — head-final 규칙

한국어는 head-final이라 **마지막 서술어가 주동사**다. `_EMAIL_ACTION_TERMS`의 마지막
출현 위치가 `_INFO_QUERY_TERMS`의 마지막 출현 위치보다 뒤면 명령이다.

| 입력 | 판정 | 근거 |
|---|---|---|
| 해당 내용 요약된 거 메일 **보내줘** | `email_send` | 요약(수식) < 보내(주동사) |
| 메일 답장 **요약해줘** | `None` (질문) | 답장(목적어) < 요약(주동사) |
| 회사 휴가 규정 **요약해줘** | `None` (질문) | 메일 신호 없음 |

recipient 없는 메일 명령은 이제 게이트에 도달해 `request_more_data` + `required_data`로
"누구에게 보낼까요?"를 묻는다 — **P3.4c가 원래 의도한 UX**다. 기존 계약
(`test_agent_work.py:252`의 "메일 답장 요약해줘" → None)은 그대로 보존된다.

### C3 상세 — 히스토리는 여전히 grounding에 안 들어간다

재작성된 **질문만** retrieval로 가고 사실은 계속 retrieved passages에서만 나온다
(AGENTS.md 원칙 7 유지). LLM은 재작성(입력 생성)만 하고 실행 결정을 하지 않으므로
설계 원칙 1과 충돌하지 않는다. 출력이 비었거나 300자 초과거나 예외면 **원문으로
fail-open**한다.

---

## 2.1 실제 전후 측정 (company 스냅샷 · 실 LLM · 2026-07-17)

같은 질문(`ai 툴 관련해서 내용 요약해줘`, `scope=all`)을 실제 retrieve + 실 LLM으로 실행.

**C2 정리 전** — 재현 세션의 답이 그대로 나온다:

> AI 툴에 관련된 내용은 AI 영상관련툴에 대한 순위를 정리한 문서가 있으며, 이 문서는 AI
> 영상관련툴에 대한 정보를 제공합니다.

근거 5개 중 **3개가 오염된 personal 페이지**(`qa-24187b36-claim`, `aistudio-panel-…`).
같은 slug로 페이지가 2개였다 — company 원본과, 과거 답변이 T2로 저장된 personal 사본:

```text
company  → "AI 영상관련툴에 대한 순위를 정리한 문서입니다."
personal → "Q: 우리 ai 툴에 관련된 문서 뭐가 있지? A: …있습니다. (문서 [1] 및 [2] 참조)"
```

**C2 정리 후** (`purge_qa_learned_wiki --company --apply`, 204건 삭제):

> AI 영상관련툴에 대한 순위를 정리한 문서가 있으며, **사용된 AI 툴은 Kling입니다.** AI의
> 기능은 의도대로 정확하게 작동하는 것으로 보입니다. 회장님은 AI가 모든 것을 처리한다고
> 생각하십니다.

근거 5개가 **전부 company scope로 교체**됐고, "Kling"이라는 실제 사실이 처음 등장했다.

**남은 문제 (정직한 평가)**: 첫 문장이 아직 "…문서가 있으며" 메타이고, 무관한 잡음
("회장님은 …")이 섞이며, 진짜 알맹이(Hugging Face·qwen·Wan2.2·LongCat)는 여전히 안 나온다.
그 알맹이는 823자 원문에 있고 §3의 **Notion 중첩 블록 미수집** 때문에 corpus에 안 들어와
있다 — 재임포트 없이는 안 고쳐진다. C6는 메타 페이지 자체를 없애 검색이 실제 내용 있는
페이지(`ai-tools`/`hugging-face`)로 가게 하지만, **rebuild가 선행돼야** 효과가 난다.

---

## 3. 남은 작업 (미수정 — 별도 슬라이스)

`orthus/connectors/notion.py`의 **수집 단계 콘텐츠 손실**은 이 PR 범위 밖이다. Notion
재임포트가 선행돼야 하고 API 호출량이 늘어 운영 판단이 필요하다:

1. **중첩 블록 미수집** (`_get_blocks:413-429`) — `has_children` 재귀가 없어 토글/중첩
   불릿/컬럼 안의 내용이 corpus에 **아예 안 들어온다**. "AI 영상관련 툴" 원문이 823자밖에
   안 되는 유력한 이유. *가장 큰 잔여 손실.*
2. **북마크/임베드 블록 통째 유실** (`_map_block:112-115` → `:475-476`) — `url`만 있고
   `rich_text`가 없어 빈 markdown이 되고 필터에서 탈락. 링크 위주 페이지는 전량 손실.
3. **인라인 링크 href 유실** (`_rich_text_plain:41-43`) — 링크가 맨 텍스트로 강등.
4. **DB row 동어반복 claim** (`_render_properties:207-220`) — row가 `**속성**: 값` 덤프로
   서술형 프롬프트를 타면서 "Wan2.2의 이름은 Wan2.2이다", "우선순위는 높음" 같은
   무정보 claim을 만든다. `task_hygiene.is_structured_row_source`는 open_question만
   억제하고 claim은 의도적으로 놔둔다.

> **~~5. fallback claim~~ → C6로 이번 PR에서 수정됨** (§1 C6 참조).

또한 `docs/distill-solar-migration-handoff.md:67-72`는 **stale**이다 — cap 8 / `get_chat_model()`로
적혀 있으나 실제 코드는 cap 20 + 전수 추출 + Solar 배정이 이미 반영돼 있다(T14). 이번
조사에서 실제로 오독을 유발할 뻔했다.

---

## 4. 활성화 순서 (운영자)

1. **머지 즉시 유효**: C2 게이트, C4, C5 — 신규 쓰기/답변부터 적용. 새 오염이 더는 안 쌓인다.
2. **기존 오염 정리** (가장 체감 큰 단계): `uv run python -m scripts.wiki.purge_qa_learned_wiki
   --company` (dry-run) → 확인 후 `--apply`. 로컬 company 스냅샷 실측: **204건 삭제**,
   혼합 페이지 4개는 자동 SKIP. 적용 후 `make node-kg-rebuild NODE=company` 1회
   (`store.delete_item`은 KG 비인지 — 삭제 수렴 권위는 full rebuild).
3. **C6/C1은 위키 재구축이 선행돼야 반영된다**: `make node-wiki-rebuild NODE=company`
   (반드시 `--clean --concurrency N` — 안 그러면 orphan 누적 + 6시간).
   - ⚠️ **cwd 주의**: `node-wiki-rebuild`는 cwd의 코드를 쓴다. 이 PR이 main에 머지된 뒤
     main에서 돌려야 C6/C1이 적용된다. 옛 코드로 돌리면 6시간 + LLM 비용이 헛된다.
   - 비용: 문서 3,822개 = LLM 3,822콜. `--source-prefix notion`(1,400개)으로 좁힐 수 있다.
   - 재구축 전까지 기존 페이지는 옛 본문을 유지한다(fail-safe).
4. **C3 켜기**: `ORTHUS_CHAT_FOLLOWUP_REWRITE_ENABLED=true` — 턴당 LLM 1콜이 추가되지만
   결정론 프리필터를 통과한 후속질문에만 붙는다.

**순서가 중요하다**: Notion 수집 수정(§3) → 재임포트 → rebuild가 하나의 사슬이다. rebuild를
먼저 돌리면 §3 수정 후 **다시** 6시간을 내야 한다. §3까지 갖춘 뒤 rebuild 1회가 경제적이다.
