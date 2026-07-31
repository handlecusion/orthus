# /ask 분할 라우터 + Company Agent 오케스트레이션 계약

> **성격:** canonical 계약서. 내부 문서(비공개) / `docs/architecture-v2.md`가 전체 시스템 계약이고,
> 이 문서는 그 안의 `/ask 분할 라우터(decompose)` 슬라이스를 최종 구현 기준으로 제어한다.
> 다른 문서와 충돌 시 이 문서가 이긴다.
>
> **기준일:** 2026-06-24. 앱 코드는 담지 않는다 (시그니처·스키마는 계약 스케치).
>
> **개정 (2026-06-29) — 오케스트레이터 진입점 이동:** 오케스트레이터(decompose +
> 단발 명령 intake + read→act + synthesize)는 더 이상 `POST /ask`에 붙지 않는다.
> `/ask`는 **순수 검색 전용**(wiki/structured/graph)으로 되돌렸고, 오케스트레이터는
> **agent-work 채팅**(`POST /agent-work/chats/{id}/orchestrate`, 라이브 SSE는
> `GET /agent-work/chats/stream/{stream_id}`)으로 옮겼다. 엔진 코드
> (`orthus/router/{__init__,decompose}.py`, 플래그 `ask_decompose_enabled`/
> `agentic_ask_enabled`)는 그대로 재사용한다 — `/ask`는 `allow_agentic=False,
> allow_decompose=False`로, orchestrate 엔드포인트는 둘 다 `True`로 호출한다.
> 이 문서의 "`/ask` decompose/agentic/명령 intake"는 모두 **orchestrate 엔드포인트**로
> 읽는다. event_orchestration(MA.8a, 메일 트리거)은 `answer_or_decompose`를 직접
> 호출하므로 무영향이다.

---

## 0. 목적 + 범위

### 목적

`/ask`에 복합 질문이 들어올 때 sub-part로 분할 → 기존 경로(wiki/structured/graph/action-intake)로 병렬 fan-out → 합성하는 **단일-pass parallel dispatch**(Phase 1 MVP)를 구현하고, Phase 2 오케스트레이터로의 확장 자리를 명시한다.

### 범위

| 안 | 밖 |
| --- | --- |
| `/ask` decompose 분기 (Phase 1) | Personal Node 내부 runner 추론 루프 |
| `should_decompose` / `split_question` / `classify_subpart` 알고리즘 | LangGraph / persona / confidence routing |
| `fan_out` (병렬) + `synthesize` + `coordinate` no-op | `agent_task` 위임 내부 (→ 내부 문서(비공개)) |
| Tool 레지스트리 7개 named adapter | 의미기반 캐싱 (Phase 3) |
| SSE `sub_answer_ready` / `synthesized` / `done` 이벤트 | P6.7 멀티 계정 메일 / P8 cutover |
| FE `mode=decomposed` 렌더러 | `docs/inline-agentic-ask.md` 범위 agentic 루프 |

### 우선순위

Phase 1 완료 후 측정(MA.5) → 긍정이면 Phase 2(coordinate 활성화 + depends_on 위상 정렬 + 루프 구조 전환). 측정 부정이면 Phase 3 보류 + Phase 1 운영 유지.

---

## 1. 핵심 원칙

1. **LLM은 입력만.** sub-question 텍스트 분할(split)·should_decompose LLM enum 보조만. 실행 결정(경로 선택, 루프 종료, 권한)은 전부 결정론 코드.
2. **기존 게이트 재사용.** wiki grounding, SELECT-only 검증, P3 policy gate — 새로 만들지 않는다.
3. **Tool 감싸기.** 모든 액션 경로가 닫힌 레지스트리(7개 고정) named adapter로 진입한다.
4. **Reviewer 없음.** 검증은 typed gate + grounding requirement.
5. **단일 질문 무회귀.** `should_decompose=False`면 기존 경로 100% 동일.

---

## 2. Phase 로드맵

| Phase | 구조 명칭 | 내용 | 현황 |
| --- | --- | --- | --- |
| **Phase 1 (MVP)** | 단일-pass parallel dispatch | 분할 + 병렬 fan-out + 합성. 루프 없음. | **구현·머지 완료** (MA.1/2/2b/4 + MA.5 측정 하니스) |
| **Phase 2** | bounded 위상 실행 | `coordinate()` 활성화, `depends_on` 위상 정렬(읽고→액션), context_from 주입, `answer_or_decompose()` wave-loop 구조 전환 | **§P2 계약이 구현** (MA.6, flag 뒤; 운영 적용은 MA.5 게이트) |
| **Phase 3-A** | 의미기반 답변 캐시 | company-scope grounded 답 캐시 + watermark 무효화 (§Phase 3-A 계약) | **MA.7a/MA.7b/MA.7c 구현** (flag 뒤; exact-normalized + embedding 유사도 매칭 + TTL/stale 행 GC) |
| **Phase 3-B** | DAG 오케스트레이터 | 이벤트 트리거, 전체 DAG | **§P3B 계약 + MA.8a/MA.8b 구현** (이벤트 트리거 + `ask_event_jobs` 큐/worker + 지식 브리프 sink + depth > 2 full DAG, flag 뒤). 다중 Personal Node fan-out은 owner 경계 위반으로 **기각**(§P3B.4) |

Phase 1에서 두 자리를 미리 남긴다:

- `coordinate(sub_answers) → CoordinateResult` — Phase 1에서 항상 `done=True` no-op. **테스트로 고정:** `coordinate()` 결과 항상 `done=True` 검증.
- `SubQuestion.depends_on: list[UUID]` — reserved 필드. Phase 1에서 항상 무시. **테스트로 고정:** `depends_on` 항상 무시 검증.

> **Phase 1은 ReAct 패턴이 아니다.** Observation→Thought 피드백 경로(루프)가 없다. ReAct 레이블은 Phase 2 이후에만 사용한다.

---

## 3. 경로 우선순위

> **2026-06-29 개정:** 이 우선순위 사다리는 그대로지만 **누가 켜느냐**가 바뀌었다.
> `/ask`(검색 전용)는 `allow_decompose=False, allow_agentic=False`로 호출하므로 항상
> legacy(wiki/structured/graph)로만 떨어진다. decompose/agentic을 켜는 호출자는 이제
> **orchestrate 엔드포인트**(`POST /agent-work/chats/{id}/orchestrate`,
> `allow_decompose=True, allow_agentic=True`)뿐이다. 단발 명령 intake도 그 엔드포인트가
> `detect_assistant_command_action`로 처리한다(`/ask` 아님).

`orthus/router/__init__.py::answer()` 함수 내부에 주석으로 고정:

```python
# Priority: decompose > agentic > legacy
# decompose: ORTHUS_ASK_DECOMPOSE_ENABLED + not should_federate(scope)
# agentic:   decompose off 또는 should_federate(scope)=True일 때만 독립 동작
# legacy:    agentic/decompose 둘 다 미진입

if decompose_enabled and not should_federate(scope):
    answer_or_decompose(..., allow_agentic_in_leaf=agentic_enabled and allow_agentic)
    # leaf 안에서 allow_decompose=False 고정 → 재귀 방지 (깊이 1 고정)

elif agentic_enabled and allow_agentic and not should_federate(scope):
    run_agentic_answer(...)   # decompose off + not federate일 때만

else:
    answer()                  # legacy
```

**경계 케이스:**

- `ORTHUS_ASK_DECOMPOSE_ENABLED=true` + `ORTHUS_AGENTIC_ASK_ENABLED=true` → decompose 진입, agentic은 leaf 내부로 전달.
- personal node + `scope="all"` → `should_federate=True` → decompose + agentic 모두 skip → legacy.
- knowledge-token 호출 (`is_token=True`) → `allow_agentic=False` 강제 → legacy (agentic + decompose 미진입).

---

## 4. `should_decompose()` 알고리즘

**두 단계로 분리한다: 빠른 False-prefilter → LLM enum.**

### 4.1 결정론 False-prefilter (LLM 없음)

아래 두 조건이 **동시에** 성립하면 즉시 `False` — LLM을 호출하지 않는다:

- **명령 동사 0개:** `_COMMAND_VERBS` (`orthus/agentwork/service.py:150`) 목록에 속하는 토큰이 전혀 없음.
- **접속·열거 힌트 0개:** 아래 패턴이 전혀 없음.
  - 접속어: `그리고`, `그다음`, `또한`, `동시에`, `그후`, `알려주고`, `해주고`
  - 열거: `①②③`, `1.`, `2.` 연속, `첫째`, `둘째`

두 조건 **모두** 성립(명령 동사도 없고 접속·열거도 없음)할 때만 즉시 `False`. 둘 중 하나라도 있으면 prefilter를 통과해 LLM으로 간다.

> **이 단계의 유일한 역할은 "명백히 단일 질문"을 LLM 없이 빠르게 걸러내는 것이다.** 실질적 복합 판단은 전부 LLM이 한다.

### 4.2 LLM enum (prefilter 통과 시)

prefilter를 통과한 모든 질문에 대해 LLM을 1회 호출한다:

```text
System:
You are a compound-question detector. Decide if the user's question contains
MULTIPLE INDEPENDENT sub-questions that can each be answered separately.
A question is compound if it contains two or more independent requests
(different topics, different actions). A question that asks multiple aspects
of the SAME topic is NOT compound.
Respond with JSON only: {"decompose": "yes"} | {"decompose": "no"} | {"decompose": "uncertain"}
```

- `yes` → `True`
- `no` + `uncertain` → `False` (불확실 → 단일 fall-through)

### 4.3 추가 제약

- 분할 폭 상한 `K = ORTHUS_ASK_DECOMPOSE_MAX_PARTS` (default 5).
- split 결과 N(개수) < 2 또는 N > K이면 분할 취소 → 단일 fall-through.
- `should_federate(scope)=True`이면 `should_decompose()` 자체를 호출하지 않고 즉시 `False`.

---

## 5. `split_question()` 알고리즘

### 5.1 입력 / 출력

```python
def split_question(question: str, *, k: int, chat_model: ChatModel) -> list[str]:
    """LLM 1회: compound question → sub-question text list (len 2 ≤ N ≤ k).
    len < 2 또는 파싱 실패 → [] 반환 → 호출자가 단일 fall-through 처리."""
```

### 5.2 프롬프트

```text
System (cache_control prefix):
You are a question-splitter for a company knowledge assistant.
Split the compound Korean/English question into at most {k} independent sub-questions.
Rules:
1. Each sub-question must be FULLY self-contained (answerable without the others).
2. Keep phrasing close to the original. Do not add information not in the original.
3. If the question cannot be split into 2 or more independent parts, return an empty list `[]`.
4. Respond with JSON only: {"sub_questions": ["<sub-q 1>", "<sub-q 2>", ...]}

User:
{question}
```

### 5.3 출력 파싱

```python
# 파싱 실패 / 필터 후 len < 2 / len > k → [] 반환
items = json.loads(raw).get("sub_questions", [])
if not isinstance(items, list) or len(items) < 2 or len(items) > k:
    return []
items = [str(s).strip() for s in items if str(s).strip()]
if len(items) < 2:   # 빈 문자열 필터 후 재확인
    return []
return items
```

빈 리스트 반환 시 `answer_or_decompose()`가 단일 fall-through로 처리한다.

### 5.4 캐싱

`ORTHUS_ASK_PROMPT_CACHE_ENABLED=true`이면 `split_question` system prompt에 Anthropic `cache_control` prefix 적용.

---

## 6. `classify_subpart()` 알고리즘

결정론 dispatcher. LLM을 호출하지 않는다.

```python
def classify_subpart(sub_q: str) -> tuple[Leaf, str | None]:
    """
    Returns (leaf, action_family | None).
    Leaf = "action-intake" | "structured" | "graph" | "wiki"
    """
    # Step 1: action-intake (reuse detect_assistant_command_action)
    # orthus/agentwork/service.py:2049
    action_family = detect_assistant_command_action(sub_q)
    if action_family is not None:
        return ("action-intake", action_family)

    # Step 2: route (reuse classify() — 내부에서 _rule_based_route 선행, None이면 LLM enum)
    # orthus/router/route.py:125 — _rule_based_route를 직접 호출하지 않는다: classify()가
    # 이미 첫 번째로 호출하므로 Step 2+3 분리 시 이중 호출이 발생한다.
    route = classify(sub_q)
    return (route, None)
```

**경계:**

- action-intake leaf 진입 시 `action_family`를 `queue_agent_work` / `create_reply_draft` Tool에 전달한다.
- off-list leaf 이름은 결정론 거부 (`ValueError` raise).
- `_INFO_QUERY_TERMS`(`orthus/agentwork/service.py:177`) 포함 sub-question은 `detect_assistant_command_action` 내부에서 `None` 반환 → action-intake 미진입 → wiki/structured/graph 정상 처리.

---

## 7. Tool 레지스트리

결정론 dispatcher(`classify_subpart`)가 직접 호출하는 named adapter 집합. LLM function-call advertise가 아니다. 동적 추가·off-list 호출 금지.

| Tool (named adapter) | 연결된 gate | 외부 쓰기 |
| --- | --- | --- |
| `split(question)` | `split_question()` — LLM 1회 | 없음 |
| `query_wiki(text, page_slugs?)` | `wiki/qa.ask` — compiled wiki grounding | 없음 |
| `query_structured(text)` | SELECT-only 검증 게이트 | 없음 |
| `query_graph(text)` | K4b `try_graph_answer` — fail-open demote | 없음 |
| `detect_gap_from_sub_answer(sub_answer)` | `detect_gap()` 집계 — 합성 단계 순회 | 없음 |
| `queue_agent_work(part, family?)` | P3 policy gate → AgentWork 후보 | 없음 — 큐잉까지 |
| `create_reply_draft(mail_id, ...)` | P7.1 `build_reply_candidate` 래핑 | 없음 — 초안 큐잉까지 |

> **`create_reply_draft` 적용 범위 (Phase 1):** `/ask` 분할 leaf의 action-intake 경로에서 `reply_context`(특정 메일 ID)가 명확히 존재하는 경우에만 호출. 1차 경로는 `queue_agent_work`. 메일 수신 이벤트 트리거(`mail/ingest.py`)에서 직접 호출하는 경우가 더 자연스럽다.

---

## 8. Leaf 구조

| Leaf | 처리 대상 | named adapter | 결과 |
| --- | --- | --- | --- |
| **지식** | 비정형 지식 질의 | `query_wiki`, `query_graph` | `SubAnswer{ grounded?, gap? }` |
| **데이터** | 집계/구조화 | `query_structured` | `SubAnswer{ routed.structured }` |
| **action-intake** | 액션·5초 초과·검토 필요 | `queue_agent_work`, `create_reply_draft` | `SubAnswer{ agent_work_id }` |

Leaf 간 통신 없음. 각 leaf는 `contextvars.copy_context().run(leaf_fn)`으로 격리한다.

---

## 9. 데이터 흐름

```text
POST /agent-work/chats/{id}/orchestrate (복합 질문; 2026-06-29 이동 — 구 POST /ask)
  │
  ├─ assistant_command 먼저 (orchestrate 핸들러 orthus/api/routes/agent_work.py::orchestrate_chat_route — decompose 이전에 실행)
  │    detect_assistant_command_action(text) is not None + operator → AgentWork
  │
  └─ answer_or_decompose()
       │
       ├─ should_decompose(question) → False → 기존 answer() 그대로 (무회귀)
       │
       └─ True → audit("router.decompose")
            │
            │ [Thought]
            │ split_question(question, k=K, chat_model) → [sq1, sq2, ...]
            │   └─ 반환 [] 또는 len < 2 → 단일 fall-through
            │
            ├─ 병렬 fan-out ─────────────────────────────────────────────────
            │  ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) + threading.Semaphore
            │  각 leaf = executor.submit(copy_context().run, leaf_fn, ...)
            │  (부모 correlation_id 명시 전달, 교차 리셋 방지)
            │  완료 수집: concurrent.futures.as_completed(futures, timeout=...)
            │
            │  ┌─ [지식/데이터 Leaf]
            │  │  query_wiki | query_structured | query_graph
            │  │  answer(sub_q, allow_decompose=False, allow_agentic=allow_agentic_in_leaf,
            │  │         learn=False, record_gaps=False)  ← sub-question 학습·gap 중복 억제
            │  │
            │  └─ [action-intake Leaf]
            │     queue_agent_work | create_reply_draft
            │     P3 policy gate → AgentWork 후보
            └──────────────────────────────────────────────────────────────
            │
            │ [coordinate — Phase 1: no-op]
            │ coordinate(sub_answers) → CoordinateResult(done=True)
            │
            │ [합성]
            │ grounded = [sa for sa in sub_answers if sa.grounded]
            │ if not grounded:          ← early-exit: grounded leaf 없음 → LLM 없이 synthesized_body=None
            │     → parts 나열 렌더 fall-through
            │ 합성 LLM (grounded sub-answer hits 한정, learn=False, record_gaps=False)
            │ structured sub-answer / action-intake sub-answer → synthesized_body 제외
            │
            └─ RoutedAnswer(
                 mode="decomposed",
                 synthesized_body: SynthesizedBody | None,
                 parts=[SubAnswer...],
                 warnings=[...],
               ) + queued AgentWork links
```

**핵심:**

- `answer_or_decompose()`는 `orthus/router/decompose.py`에 정의, `orthus/router/__init__.py`에서 호출. 각 leaf를 `answer(allow_decompose=False, learn=False, record_gaps=False)`로 호출해 재귀 방지 + 학습 억제.
- 합성 LLM은 sub-answer 발췌·재배열만. 새 사실 생성 금지.
- **Timeout:** 개별 `Future.result(timeout=LEAF_TIMEOUT_SEC)` 초과 시 해당 `SubAnswer.error="timeout"` 격리 — 전체 응답 실패 아님.
- **예외 격리:** leaf 내부에서 `TimeoutError` 외의 예외(`RuntimeError`, `ConnectionError`, 기타)가 발생해도 `SubAnswer(error="leaf_error", routed=None)`으로 격리. 예외 메시지는 audit span에만 기록, `SubAnswer.error`에 raw 메시지 포함 금지 (PII 가능성). 어떤 예외도 fan-out 전체를 500으로 만들지 않는다.
- `executor.submit` 사용 시 반드시 `copy_context().run(leaf_fn, ...)` 래퍼로 ContextVar 명시 전달 (`correlation_id` 포함). `run_in_executor`는 ContextVar를 자동 복사하지 않으므로 사용 금지.

---

## 10. 스키마 계약

```python
# orthus/schemas/canonical.py 확장

SubQuestion(
    id: UUID,
    text: str,
    scope: str,            # 부모 effective scope 상속
    depends_on: list[UUID] = [],  # reserved — Phase 2 위상 정렬 예정
)

SubAnswer(
    sub_question_id: UUID,
    routed: RoutedAnswer | None,  # 실패 시 None
    grounded: bool,
    gap: WikiGap | None,
    agent_work_id: UUID | None,
    error: str | None,            # "timeout" | "leaf_error" | None
)

CoordinateResult(
    done: bool,    # Phase 1: 항상 True. Phase 2에서 루프 계속 여부로 확장 예정.
)

SynthesizedBody(
    answer: str,
    sources: list[WikiSourceRef],  # wiki/graph leaf grounded hits만
    warnings: list[str],
)

# RoutedAnswer.mode 추가
mode: str  # 기존 + "decomposed"

# RoutedAnswer 신규 필드
synthesized_body: SynthesizedBody | None = None
parts: list[SubAnswer] = []
```

---

## 11. sub-question scope 상속

부모 `/ask` 엄격 상속. sub-query가 scope를 격상하거나 타 owner 데이터를 볼 수 없다.

- `scope`: 부모 effective scope (company/all/personal) 그대로
- `owner_id`: 부모 caller 기준 (K7 owner filter 그대로)
- `should_federate`: 부모 결과 동일 적용
- graph leaf: K4b company node + `scope ∈ {company,all}` + `kg_available` 3중 가드 그대로

---

## 12. SSE 스트리밍

`GET /agent-work/chats/stream/{stream_id}` 채널 (2026-06-29 이전엔 `GET /ask/{stream_id}/stream`;
오케스트레이터 이동으로 agent-work로 옮김). 기존 `orthus/agentwork/stream.py` pub/sub 패턴,
키 `f"{user_id}:{stream_id}"` 그대로 재사용.

**이벤트 순서:**

```text
event: sub_answer_ready
data: {"index": 0, "sub_question": "...", "routed": {...}, "grounded": true}

event: sub_answer_ready
data: {"index": 1, "sub_question": "...", "agent_work_id": "uuid", "grounded": false}

event: synthesized
data: {"synthesized_body": {...}, "warnings": [...]}

event: done
data: {}
```

**발행 방식:**

fan-out 스레드(ThreadPoolExecutor worker)에서 SSE에 프레임을 쓸 때는 반드시 `publish_threadsafe(stream_key, frame)`를 사용한다 (`publish()` 직접 호출 금지 — asyncio 큐는 이벤트 루프 스레드에서만 안전). 스트림 키는 `f"{user_id}:{stream_id}"` — 기존 agentic SSE와 동일 공간, 동일 채널 재사용.

**FE 계약:**

- `sub_answer_ready` 인덱스를 추적해 누락 index가 있으면 `synthesized` 렌더 지연.
- 클라이언트 SSE 연결 해제 시 pub/sub 구독 해제. ThreadPoolExecutor future는 이미 실행 중이면 Python `Future.cancel()`로 중단 불가 — 남은 leaf는 자연 완료될 때까지 실행되고 결과는 버려진다. SSE는 best-effort feedback 전용이므로 연결 해제가 서버 오류를 유발하지 않는다.
- `event: error`는 **fan-out이 시작조차 못한 경우**(split 반환 후 executor 초기화 오류 등 예외적 상황)에만 전달한다. 개별 leaf가 `error="timeout"|"leaf_error"`로 격리되는 경우는 전체 fan-out 실패가 아니므로 `synthesized` + `done`을 정상 envelope으로 전달한다. **모든 leaf가 error인 경우**도 동일하게 all-error `parts`를 담은 `synthesized`(body=None) + `done`을 반환한다 — `event: error`를 내지 않는다.

---

## 13. FE 렌더 계약

`mode="decomposed"` 분기 렌더러 — MA.2b로 `ask/page.tsx`에 구현했으나(commit `956730e`),
2026-06-29 오케스트레이터 이동으로 지금은 **agent-work 채팅**
(`web/src/app/(work)/agent-work/page.tsx`)이 렌더한다(commit `ee28b8f`; `/ask`는 검색
전용이라 `mode="decomposed"` 응답이 오지 않는다).

```text
1. synthesized_body 있음 → 상단 통합 합성 답변 + 하단 'parts 세부 근거' (접기/펼치기)
2. synthesized_body 없음 → parts를 순서대로 카드/섹션 나열

각 part:
- sub-question 텍스트 + (grounded 답 + 근거 칩)
- structured sub-answer → routed.body (집계 결과) 직접 표시, synthesized_body 미포함
- agent_work_id → /agent-work 딥링크
- error → 실패 표기

warnings → 하단 노출
```

기존 단일-mode 렌더(wiki/structured/graph/agent_work) 무변경.

**flag on 게이트:** MA.2b(FE 렌더러) 완료 전에 `ORTHUS_ASK_DECOMPOSE_ENABLED=true` 운영 환경 적용 금지.

---

## 14. action-intake → agent_task 연결 전략

**채택: P3 위임 (Option B).** `queue_agent_work` Tool은 P3 policy gate(`agentwork/service.py`)에만 넘긴다. `agent_task` 승격 여부는 P3가 자체 평가한다(`ORTHUS_AGENT_TASK_ENABLED` + daemon 등록 + actor 권한). Decompose 레이어는 `agent_task` 조건을 알 필요 없다.

| 옵션 | 장점 | 단점 |
| --- | --- | --- |
| **A. 자동 평가** | 사용자에게 투명 | decompose가 P3 내부 조건 의존. 복잡도↑ |
| **B. P3 위임 (채택)** | 코드 단순. 기존 P3 완전 재사용 | "누가 처리할지" 실시간 파악 불가 |
| **C. Two-step 비동기** | 완전 비동기 | 같은 pass 합성 불가. UX 불가 |

Phase 2 오케스트레이터 전환 시 Option A 재검토.

---

## 15. 캐싱 전략

### (A) 프롬프트 캐싱 (Phase 1 포함)

Anthropic `cache_control` prefix. `ORTHUS_ASK_PROMPT_CACHE_ENABLED=true`로 제어.

- `split_question` system prompt — 모든 decompose 호출에 동일한 정적 prefix
- `should_decompose` LLM enum system prompt 동일

cross-owner leak 없음 (모델 응답이 context에 붙어 있기 때문).

### (B) 의미기반 캐싱 (Phase 3)

global wiki/corpus watermark primitive 구축 필요 (현재 미존재). `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=false` default. 캐시 키: `owner_id` + `scope` + `project` + `federation` + `node_id` + watermark 버전 전부 필수. `ask_cache`에 개인/owner-scope 답 central 자동저장 금지.

---

## 16. DB 커넥션 풀 안전

병렬 fan-out은 PG 커넥션 풀 동시 소모. MA.2 수용 게이트에서 아래 부등식 계산·첨부 필수:

```text
leaf당 최대 DB 커넥션 점유량 × MAX_CONCURRENCY × 동시 /ask 요청 수
    ≤ pool_size(10) + max_overflow(30)
```

- `MAX_CONCURRENCY` = `ORTHUS_ASK_DECOMPOSE_MAX_CONCURRENCY` (default 4)
- `동시 /ask 요청 수` = FastAPI 스레드풀 크기(기본 40) 대비 실제 동시 decompose 요청 예상치
- FastAPI 자체 스레드풀 + fan-out ThreadPoolExecutor 가 PG 커넥션 풀을 공유하므로 둘 다 반영

`Future.result(timeout=...)` 초과 시 해당 SubAnswer만 `error="timeout"` 격리. 전체 응답 실패 아님.

---

## 17. 리스크

| # | 리스크 | 완화 |
| --- | --- | --- |
| R1 | **하드룰 인접** — 병렬+반복이 LangGraph로 보일 수 있음 | 결정론 루프 + bounded leaf. `orthus-operator-reviewer` 사전 검토 |
| R2 | **분할 환각** — sub-question이 원 질문과 어긋남 | split 결과 K<2 → 취소. 불확실→단일 |
| R3 | **부분 실패** | `SubAnswer.routed=None+error`, `warnings` 사유 |
| R4 | **scope leak** | 부모 scope 엄격 상속 + 경계 테스트 |
| R5 | **중복 학습/gap** | `learn=False`, `record_gaps=False`, gap dedup |
| R6 | **비용** | K 상한(5), rule-first 억제, 프롬프트 캐시 |
| R7 | **의미기반 캐시 cross-owner leak** | 키에 owner_id+scope+project+federation+노드 필수 (Phase 3) |
| R8 | **합성 품질 (Reviewer 없음)** | grounded hits에서만 합성. 불변식 위반 시 PR 거부 |
| R9 | **action-intake → agent_task 조건 변경** | Option B 채택 → decompose 코드 무영향 |
| R10 | **Phase 2 전환 비용** | coordinate no-op + depends_on reserved로 최소화 |
| R11 | **DB 커넥션 풀 고갈** | pool_timeout SubAnswer 격리 + 부등식 계산 (MA.2 게이트) |
| R12 | **correlation_id 교차 리셋** | `copy_context().run(leaf_fn)` 패턴 필수 |
| R13 | **action_family LLM 오염** | `classify_subpart`는 결정론 전용. LLM enum 입력 경로 없음 |
| R14 | **FE 폴백 노출** | MA.2b 필수. flag on 전 완료 게이트 |
| R15 | **Phase 2 context_from scope escalation** | context_from 전달은 PII redaction + owner_id/scope 경계 강제 |

---

## 18. 코드 접점

### 진입 / 응답

- [`orthus/router/__init__.py:71`](../orthus/router/__init__.py) `answer()` — decompose/agentic/legacy 우선순위 분기 주석 고정 위치. **신규 파라미터 구현 완료:** `allow_decompose: bool = True`, `learn: bool = True`, `record_gaps: bool = True` (재귀 방지·학습 억제·gap 중복 억제). **(2026-06-29 개정)** `/ask`(`ask.py:75`)는 `allow_agentic=False, allow_decompose=False` 고정 — decompose/agentic은 orchestrate 엔드포인트(`orthus/api/routes/agent_work.py`)만 켠다.
- [`orthus/api/routes/ask.py:60`](../orthus/api/routes/ask.py) `ask_endpoint` — HTTP 핸들러만 (분기 로직 없음)
- [`orthus/schemas/canonical.py:650`](../orthus/schemas/canonical.py) `RoutedAnswer` — mode/synthesized_body/parts 확장

### 재사용 (변경 없음)

- [`orthus/router/route.py:125`](../orthus/router/route.py) `classify()` — sub-question LLM 분류
- [`orthus/router/route.py:142`](../orthus/router/route.py) `_rule_based_route()` — 결정론 rule
- [`orthus/router/route.py:62`](../orthus/router/route.py) `_GRAPH_TERMS`, `_STRUCTURED_TERMS`, `_STRUCTURED_ENTITIES`, `_WIKI_TERMS`
- [`orthus/router/graph.py`](../orthus/router/graph.py) `try_graph_answer()` — fail-open demote 그대로
- [`orthus/wiki/qa.py`](../orthus/wiki/qa.py) `ask()`, `answer_from_hits()` — `learn=False, record_gaps=False`
- [`orthus/wiki/gap.py`](../orthus/wiki/gap.py) `detect_gap()`, `record_gap()`, `maybe_missing_link()`
- [`orthus/federation/query.py`](../orthus/federation/query.py) `should_federate(scope)`
- [`orthus/agentwork/service.py:2049`](../orthus/agentwork/service.py) `detect_assistant_command_action()` — action leaf 진입 기준
- [`orthus/agentwork/service.py:150`](../orthus/agentwork/service.py) `_COMMAND_VERBS` — should_decompose 신호 A 재사용
- [`orthus/agentwork/stream.py`](../orthus/agentwork/stream.py) `publish`, `subscribe`, `unsubscribe` — SSE pub/sub 재사용

### Agent Work 연계

- [`orthus/agentwork/service.py`](../orthus/agentwork/service.py) `classify_candidate_with_policy_memory`, `persist_agent_work_item`
- [`orthus/mail/reply.py`](../orthus/mail/reply.py) `build_reply_candidate` — `create_reply_draft` Tool 래핑 대상

### 감사

- 신규 span: `router.decompose`, `router.decompose.synthesize`
- sub-query는 기존 span 유지, `correlation_id`로 묶음

### 신규 파일

- `orthus/router/decompose.py` — `should_decompose()`, `split_question()`, `fan_out()`, `coordinate()`, `synthesize()`, `answer_or_decompose()`
- `orthus/router/tools.py` — 결정론 dispatcher(`classify_subpart`) + named adapter 7개

---

## 19. 구현 슬라이스

### 플래그 (전부 default false)

- `ORTHUS_ASK_DECOMPOSE_ENABLED`
- `ORTHUS_ASK_PROMPT_CACHE_ENABLED`
- `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED` (Phase 3)
- `ORTHUS_ASK_COMMAND_SPLIT_ENABLED` (구현 완료 — S1–S5/B게이트/C7·C8 main 머지: PR #563/#594/#597/#603/#611, 최종 2026-07-06; `ask_decompose_enabled`에 **AND 종속** — off면 강등)

### 설정값

| 환경변수 | settings.py 필드명 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `ORTHUS_ASK_DECOMPOSE_MAX_PARTS` | `ask_decompose_max_parts` | 5 | K (split 상한) |
| `ORTHUS_ASK_DECOMPOSE_MAX_CONCURRENCY` | `ask_decompose_max_concurrency` | 4 | ThreadPoolExecutor max_workers |
| `ORTHUS_ASK_DECOMPOSE_LEAF_TIMEOUT_SEC` | `ask_decompose_leaf_timeout_sec` | 30 | 개별 leaf Future.result timeout (초) |
| `ORTHUS_ASK_COMMAND_SPLIT_ENABLED` | `ask_command_split_enabled` | false | 구현 완료(main; PR #563/#594/#597/#603/#611, 최종 2026-07-06 — C7 조각 칩 재수화 `c1fabf7` + C8 빈 `?ids=` 빈 응답·위임 프리픽스 strong 우선 `f2f7b2f` 포함). 복합 명령을 인지 split해 명령절 fragment를 held AgentWork로 fan-out. `ask_decompose_enabled` AND 종속(off면 강등), 미발화(flag off byte-identical), 측정 비상속(MA.5 상속 아님 — 별도 게이트) |
| `ORTHUS_ASK_DECOMPOSE_COMMAND_GUARD` | `ask_decompose_command_guard` | true | 명령 보존 가드. decompose가 명령절을 read로 유실하지 않게 강한 명령을 별도 큐잉/보존 |

### Phase 1 — 무조건 구현

| 슬라이스 | 내용 | 통과 게이트 |
| --- | --- | --- |
| **MA.1** | `should_decompose` (§4 rule-first + LLM enum), `split_question` (§5 포맷), `classify_subpart` (§6 결정론). 분할만, 답은 단일 fall-through. decompose/agentic/legacy 우선순위 분기 주석 고정. | 기존 5 router 회귀 PASS, flag off 무변경, should_decompose 신호 A/B 단위 테스트, LLM enum 3-class 단위 테스트, personal+scope=all에서 legacy 진입 테스트, off-list tool 거부 |
| **MA.2** | 병렬 read-only fan-out + 합성 + envelope. 지식/데이터 typed leaf, `RoutedAnswer(mode="decomposed", synthesized_body, parts)`. 프롬프트 캐싱. `ThreadPoolExecutor + executor.submit(copy_context().run, leaf_fn)` 격리. SSE `sub_answer_ready` + `synthesized` + `done`. | 복합 질문 e2e 인라인 답, grounding 불변식, 병렬 부분실패 격리, owner-scope 상속, correlation_id 연결 확인, pool_timeout SubAnswer 격리, PII redaction 확인, DB 커넥션 부등식 계산 첨부, SSE 순차 렌더 브라우저 QA |
| **MA.2b** | FE `mode=decomposed` 렌더러. `ask/page.tsx` mode switch에 decomposed 분기. synthesized_body 우선 렌더 + parts 세부 근거. agent_work_id 딥링크. | 390×844 브라우저 QA PASS, 폴백('응답 본문이 비어 있습니다') 미노출, action-intake + read-only 혼합 렌더 확인 |
| **MA.4** | action-intake leaf + `queue_agent_work` + Agent Work 연계. action/검토 필요 sub-part → P3 gate → 후보. | P3 policy gate 무변경 재사용, 큐잉 surface, audit span, PII redaction 확인 |

> **flag on 게이트:** MA.2b 완료 전에 `ORTHUS_ASK_DECOMPOSE_ENABLED=true` 운영 환경 적용 금지.

### Phase 1 — 선택 (mail 이벤트 트리거 연결 시)

| 슬라이스 | 내용 | 통과 게이트 |
| --- | --- | --- |
| **MA.3 구현** | `create_reply_draft` Tool **어댑터만 완성**(Option B, 호출자 전달 reply_context). P7.1 `build_reply_candidate` + `persist_reply_candidate` 래핑 → canonical `ReplyDraftResult(outcome, item, required_data, reason)`(Pydantic, `orthus/schemas/canonical.py`). 입력 `reply_context: MailIngestRequest \| None`; `owner_scoped`는 **필수 인자**(default 없음 — 개인 메일함 답장이 company-wide로 fail-open 노출되는 것 방지). None → `request_more_data(required_data=["reply_context"], reason="no_reply_context")`, 비라우팅(outbound·flag off·non-company·non-routable) → `request_more_data(required_data=[], reason="no_routable_reply")`(호출자가 보완할 입력 아님), 정상 → `draft_for_review` 큐잉(발송 없음, 불변식 9). 경계 typed 검증(`MailReplyContext.model_validate`) + `audit("router.create_reply_draft")` span. **action-intake leaf 자동 라우팅 + `/ask` mail context 파라미터는 후속 슬라이스**(§7 note). **`tests/integration/test_ma3_create_reply_draft.py` 7종 PASS.** | typed payload 검증(PASS), `reply_context` 미지정/비라우팅 시 `request_more_data` + 정확한 `reason`/`required_data`(PASS), owner-scope 필수 바인딩(PASS), audit span(PASS) |
| **MA.3b 구현** | `/ask` mail context + decompose reply-draft 재사용(**D안**: 복원 대신 P7.1 초안 재사용). `AskIn.context_mail_id`(canonical mail id) → `answer`→`_route_answer`→`answer_or_decompose`→`fan_out`/`_run_waves`→`_run_leaf` 배선(`context_wiki_slug`와 동형, knowledge-token은 미전달, semantic cache 키에서 제외). action-intake leaf가 `context_mail_id` + `action_family=="email_send"`이면 `find_reply_draft_for_mail`(owner-scope fail-closed, `source_kind="mail_reply"`, idempotent on canonical id)로 기존 P7.1 `draft_for_review` 초안을 조회해 `SubAnswer.agent_work_id`로 링크(`audit("router.decompose.reply_reuse")`), 없으면 `queue_agent_work` fallback. 신규 build/복원·마이그레이션 없음. **`tests/integration/test_ma3b_reply_reuse.py` 6종 PASS.** | owner-scope 조회 경계(PASS), 기존 초안 재사용(PASS), 초안 없음/`context_mail_id` 없음 시 fallback(PASS), decompose/router/event-orch/ask-cache 무회귀(PASS) |

### Phase 2 — 측정 게이트

**MA.5:** Phase 1 운영 후 회사 스냅샷으로 측정.

- (a) 복합 질문 빈도/형태
- (b) 독립 병렬 복합 질문 비율
- (c) decompose 답이 단일 route 대비 실제로 더 나은가
- (d) 5초 인라인 예산 현실성
- (e) Phase 1 스트리밍 latency 체감

측정 부정이면 Phase 3 보류.

**MA.6 (순서 의존성):** "읽고→액션" 패턴 지원. `depends_on` 활성화 + 위상 정렬 + `answer_or_decompose()` 루프 구조 전환 + `context_from` PII redaction + owner_id/scope 경계.

### Phase 3 — 오케스트레이터

`coordinate` 루프 활성화, DAG 전체, 이벤트 기반 트리거, 의미기반 캐싱(B).

---

## 20. 수용 게이트

### 자동화 검증

- **무회귀:** 기존 router 회귀 5종 + 검증 게이트 reject 5종
- **flag off:** `ORTHUS_ASK_DECOMPOSE_ENABLED=false`에서 `/ask` 출력 바이트 동일
- **우선순위:** decompose on + not federate → decompose 진입. decompose off + agentic on + not federate → agentic 진입. federate=True → legacy 진입
- **off-list tool 거부:** off-list adapter 이름 입력 시 `ValueError`
- **owner-scope 경계:** sub-query가 타 owner 데이터 미노출 (K7 B-시리즈 차용)
- **grounding:** wiki/graph leaf → compiled wiki. structured leaf → SELECT-only + `notion_rows`
- **should_decompose 단위 테스트:** prefilter 경계 케이스 — (명령 동사 ≥1 → LLM enum 진입), (접속·열거 ≥1 → LLM enum 진입), (둘 다 0 → 즉시 False). LLM enum 3-class: yes→True, no→False, uncertain→False
- **`coordinate()` no-op 고정:** 항상 `done=True` 반환 검증
- **`depends_on` 무시:** 항상 무시 검증
- **PII redaction:** sub-question/synthesized_body 저장 경로, `queue_agent_work` 내부
- **fan-out correlation_id 교차 리셋 없음:** `executor.submit(copy_context().run, leaf_fn)` 패턴 적용 확인. `run_in_executor` 직접 사용 금지
- **DB 커넥션 부등식 계산 첨부** (leaf당 최대 커넥션 × K × 동시 요청 ≤ pool_size+max_overflow)
- **pool_timeout SubAnswer 격리** (전체 응답 실패 아님)
- **action_family 결정론 보장** (LLM enum 입력 경로 미포함 확인)
- **FE `mode=decomposed` 렌더러 완료** (MA.2b 또는 flag on 전 완료 게이트)
- **의미기반 캐시 키** (`ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=true` 시에만): `owner_id`+scope+project+federation 미포함 시 빌드 실패

### 사람 검토

- `orthus-operator-reviewer`로 R1 직접 검토 — LangGraph/agentic/LLM-only 아님을 코드 구조로 입증
- 복합 질문 3–5 케이스 브라우저 QA (390×844 포함)
- FE 렌더러: mode=decomposed 분기 확인 (`ask/page.tsx`), mobile 390×844 PASS

---

## 21. 보존 불변식 (위반 시 PR 거부)

1. **read-only + 검증 게이트.** 분할/합성은 read-only. 액션은 P3 policy gate 경유만.
2. **그라운딩 경로.** wiki/graph leaf → compiled wiki page. structured leaf → SELECT-only + `notion_rows`. raw-chunk RAG 금지.
3. **결정론 outcome.** 분할 여부·병렬 스케줄·Agent Work outcome 모두 결정론.
4. **LangGraph/persona/drift/confidence routing 없음.** 빈 stub도 금지.
5. **owner-scope/federation 상속.** sub-query scope 격상·타 owner personal 노출 금지.
6. **PII redaction.** sub-question/query_runs 저장 경로, `queue_agent_work` 내부 필수.
7. **P3 "단순 작업" 규칙.** 5초 초과·외부 쓰기·검토 필요는 Agent Work.
8. **기존 단일 `/ask` 무회귀.** `should_decompose=False`면 기존 경로 100% 동일.
9. **닫힌 레지스트리.** named adapter 7개 고정. 각 adapter는 결정론 gate에 1:1 매핑. off-list 거부. `queue_agent_work`/`create_reply_draft` 후보 생성까지만.
10. **캐시 키 owner-scope 포함.** 의미기반 캐시 활성화 시 `owner_id`+scope+project+federation+노드+watermark 필수.
11. **학습 메모리 = 입력 전용.** 자유형식 자가파일 금지. outcome 자율 변경 없음.
12. **Phase 2 확장 자리 유지.** `coordinate` no-op + `depends_on` reserved 제거 금지. 테스트로 동작 고정.
13. **decompose/agentic 우선순위 고정.** 분기 주석은 `router/__init__.py::answer()` 내부에 고정.
14. **action_family 결정론 전용.** LLM enum 입력 경로 사용 금지. `detect_assistant_command_action` keyword match 재사용.
15. **fan-out 격리.** 병렬 leaf는 `executor.submit(copy_context().run, leaf_fn)` 패턴. `run_in_executor` 직접 사용 금지. correlation_id 교차 리셋 금지.
16. **Phase 1 = 단일-pass.** ReAct 레이블은 Phase 2 coordinate 루프 활성화 이후만 사용.

---

## 22. 하드룰 준수

- [x] read-only 라우터 + 검증 게이트 우회 없음
- [x] 그라운딩 = compiled wiki page 전용 (structured leaf는 SELECT 게이트)
- [x] LLM-only 실행 없음 / 결정론 outcome
- [x] LangGraph/persona/drift/confidence routing 코드 없음
- [x] Reviewer 단계 없음 — typed gate + grounding
- [x] owner-scope/personal 경계·PII redaction (`queue_agent_work` 내부 포함)
- [x] Tool 레지스트리 통일 — 결정론 dispatcher. LLM function-call advertise 아님
- [x] `create_reply_draft` Tool P7.1 typed payload 규칙 준수
- [x] fail-closed 플래그 default false
- [x] Phase 2 확장 자리 (coordinate no-op, depends_on reserved) + 테스트 고정
- [x] `ask_cache`에 개인/owner-scope 답 central 자동저장 없음
- [x] decompose/agentic 우선순위 + action_family 결정론 + fan-out 격리 + DB 커넥션 풀 안전

---

## Phase 2 계약 — depends_on 위상 실행 + coordinate 활성화

> **성격:** 본 문서 §2 로드맵의 Phase 2 슬라이스를 §3–§22(Phase 1)와 동일한 강도로 확정한다.
> Phase 1 무회귀가 1순위다. Phase 2 코드는 전용 flag(`ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED`,
> default false) 뒤로 머지하며, **운영 적용은 여전히 MA.5 측정 게이트**다.
>
> **기준일:** 2026-06-25.

---

## P2.0 범위

| 안 | 밖 |
| --- | --- |
| `depends_on` 도출 (split LLM 확장, index-based) | 일반 ReAct / 오픈루프 / LLM-driven 루프 제어 |
| bounded 위상 실행 (`topo_levels` → wave loop) | DAG 전체 (Phase 3) |
| `coordinate()` 활성화 (결정론 loop-continue) | 이벤트 트리거 / 의미기반 캐시 (Phase 3) |
| `context_from` 주입 (redaction + scope 경계) | depth > `MAX_DEPTH` |
| 실패 전파 (upstream 실패 → downstream skip) | sub-question scope 격상 / 타 owner 노출 |

핵심 사용 패턴: **"읽고→액션"** (예: "지난주 매출 조회해서 그 숫자로 김부장한테 메일 보내줘"). depth 2 고정이 1순위 타깃이다.

---

## P2.1 핵심 원칙 (Phase 1 §1 유지 + 추가)

- **D1. Phase 2는 결정론 위상 실행이다. LangGraph/ReAct 프레임워크가 아니다.**
  루프는 (a) **acyclic-by-construction**(backward-only 의존성), (b) **depth ≤ `MAX_DEPTH`**(default 2) bounded,
  (c) LLM은 `depends_on` 도출(입력)만 하고 **루프 종료·다음 wave 선정은 전부 결정론 `coordinate()`**.
  persona/drift/confidence routing 없음. → R1 완화, `orthus-operator-reviewer` 필수 (하드룰: LangGraph 코드 금지).
- **D2. Phase 2 전용 flag.** `ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED` default false.
  off이면 `depends_on` 무시 + `coordinate()` no-op → **Phase 1 flat parallel과 byte-identical**(불변식 8 유지).
- **D3. 운영 활성화는 MA.5 게이트.** 코드는 flag 뒤로 머지 가능하지만,
  `ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED=true` 운영 적용은 MA.5 긍정 + MA.6 게이트 통과 후.
- **D4. ReAct 레이블 사용 가능 시점.** §21 불변식 16 — Phase 2 `coordinate()` 루프 활성화 이후에만.
  단 "bounded 위상 실행"이 정확한 명칭이며, 무한/오픈 ReAct가 아님을 문서·코드 주석에 명시한다.

---

## P2.2 `depends_on` 도출 — `split_question` 확장

Phase 2 flag on일 때만 `split_question` 출력 포맷을 확장한다.

### P2.2.1 출력 포맷 (flag on)

```json
{"sub_questions": [
  {"text": "지난주 매출 얼마였어?", "depends_on": []},
  {"text": "그 숫자로 김부장에게 매출 보고 메일 보내줘", "depends_on": [0]}
]}
```

- **index-based(positional) 의존성.** `split_question`이 index → `SubQuestion.id`(UUID) 매핑.
- **backward-only 강제:** `depends_on`의 모든 index < 자기 index. forward/self/out-of-range index는 **그 의존성만 drop**한다(분할 자체는 유지). → 위상 그래프가 **구조적으로 acyclic**.
- **하위호환 파싱:** flag off이거나 LLM이 Phase 1 포맷(`{"sub_questions": [str, ...]}`)을 내면 그대로 받는다 — item이 `str`이면 `depends_on=[]`, `dict`이면 `text`/`depends_on` 추출.

### P2.2.2 프롬프트 rule 추가 (§5.2 확장, flag on일 때만)

```text
5. If sub-question B can only be answered AFTER sub-question A (B uses A's result),
   set B's "depends_on" to [A's index]. Independent sub-questions have "depends_on": [].
   Only reference EARLIER indices (a sub-question may depend only on ones before it).
6. Respond with JSON only:
   {"sub_questions": [{"text": "<sub-q>", "depends_on": [<earlier idx>, ...]}, ...]}
```

### P2.2.3 파싱 가드 (§5.3 확장)

```python
# dict item: "text" 없으면 drop. depends_on이 list[int] 아니면 [].
# index ∉ [0, self_index) → 그 index drop (backward-only).
# 필터 후 len < 2 또는 > k → [] (단일 fall-through, Phase 1 동일).
```

---

## P2.3 위상 실행 — wave loop

`answer_or_decompose()`를 단일 `fan_out` 호출에서 **wave loop**로 전환(flag on일 때만; off이면 Phase 1 단일 fan_out 그대로).

```python
levels = topo_levels(sub_questions)   # 결정론 Kahn-style 레벨 분해. cycle 불가(backward-only)
if len(levels) > MAX_DEPTH:
    # depth 초과 → 의존성 전부 무시하고 flat 단일 wave로 강등 (warning "depth_exceeded")
    levels = [sub_questions]
sub_answers: dict[UUID, SubAnswer] = {}
for wave in levels:
    ctx = {sq.id: resolve_context(sq, sub_answers) for sq in wave}   # P2.4
    wave_answers = fan_out(wave, context_from=ctx, ...)              # Phase 1 fan_out 재사용
    sub_answers.update({sa.sub_question_id: sa for sa in wave_answers})
    coord = coordinate(sub_answers, sub_questions)                   # P2.5
    if coord.done:
        break
```

- **wave 내부 = Phase 1 `fan_out` 병렬 그대로.** wave 간 barrier(앞 wave의 `as_completed` 완료 대기).
- `topo_levels`: backward-only라 Kahn 알고리즘이 항상 종료. cycle은 구조적으로 불가하지만 방어적으로 잔여 노드 발견 시 `depth_exceeded` 강등으로 fail-safe.
- `MAX_DEPTH` = `ORTHUS_ASK_DECOMPOSE_MAX_DEPTH` default 2 (읽고→액션 = depth 2).

---

## P2.4 `context_from` 주입

downstream leaf 실행 전, 의존 upstream sub-answer 본문을 **redaction + scope 경계** 통과시켜 주입한다.

```python
def resolve_context(sq: SubQuestion, sub_answers: dict[UUID, SubAnswer]) -> str | None:
    parts: list[str] = []
    for dep_id in sq.depends_on:
        up = sub_answers.get(dep_id)
        if up is None or not up.grounded or up.routed is None:
            continue  # 실패/미그라운디드 upstream은 주입 안 함 → P2.6에서 별도 처리
        assert up.routed.... scope == sq.scope  # R15: owner/scope 경계(부모 상속이라 항상 동일; 위반 시 raise)
        body = _extract_body(up.routed)          # wiki.answer 또는 structured 결과 요약
        parts.append(redact_pii_text(body))      # 불변식 6 확장
    return "\n\n".join(p for p in parts if p) or None
```

- **주입 방식:**
  - **knowledge/data leaf:** question 앞에 prepend — `"[관련 맥락]\n{context}\n\n[질문]\n{sub_q.text}"`.
  - **action-intake leaf:** `queue_agent_work` payload의 `context.upstream`(redacted text)로 전달. 메일 초안 등이 조회 결과를 본문에 반영한다. (`create_assistant_command_work_item`에 `upstream_context: str | None` optional 추가, `payload["context"]["upstream"]`에 저장.)
- **PII redaction 필수** (`redact_pii_text`, `orthus/audit/redact.py:42`). scope/owner_id 경계는 부모 상속이라 구조적으로 동일하지만 `assert`로 고정(R15). 위반은 leaf error로 격리.

---

## P2.5 `coordinate()` 활성화

Phase 1 no-op → Phase 2 결정론 loop-continue (LLM 없음).

```python
CoordinateResult(done: bool, next_wave: list[UUID] = [])

def coordinate(sub_answers: dict[UUID, SubAnswer], sub_questions: list[SubQuestion]) -> CoordinateResult:
    pending = [sq for sq in sub_questions if sq.id not in sub_answers]
    if not pending:
        return CoordinateResult(done=True)
    ready = [sq.id for sq in pending if all(d in sub_answers for d in sq.depends_on)]
    return CoordinateResult(done=False, next_wave=ready)
```

- **flag off → 항상 `done=True`** (Phase 1 §2 고정 테스트 유지, flag-gated). 불변식 12 보존.
- 순수 결정론 — LLM enum 입력 경로 없음(불변식 13 확장).

---

## P2.6 실패 전파

upstream leaf가 `error` 또는 not grounded → 그것에 의존하는 downstream leaf는 **실행하지 않고** `SubAnswer(error="upstream_unavailable")`로 격리한다.

- **read→act에서 read 실패 시 act를 blind 실행하지 않는다**(안전 — 잘못된 메일 발송/액션 방지).
- action-intake leaf가 upstream 실패로 skip되면 **AgentWork 큐잉도 하지 않는다**(불변식 7/20).
- warning: `"upstream_unavailable:{idx}"`. SSE는 skip leaf도 `sub_answer_ready`(grounded=false, error) 발행.

---

## P2.7 스키마 변경 (§10 확장)

```python
# SubQuestion.depends_on: reserved → 활성 (필드 변화 없음, 동작만 활성화)

CoordinateResult(
    done: bool = True,
    next_wave: list[UUID] = [],   # Phase 2 신규 — 다음 wave 후보
)

# SubAnswer.error: "timeout" | "leaf_error" | "upstream_unavailable" | None  ← 값 추가
# SubAnswer.context_injected: bool = False  — upstream 맥락 주입 여부 (FE 표식)
# SubAnswer.text: str | None = None          — MA.6c: sub-question 텍스트 (envelope enrich)
# SubAnswer.depends_on: list[int] = []       — MA.6c: parts 내 positional 의존 인덱스 (FE 표식)
```

> `text`/`depends_on`은 leaf 실행이 아니라 **envelope 빌드 시 `_enrich_parts`로 채운다**(wire는 UUID가 아닌 positional index — FE 친화). 합성 LLM(§9 `_SYNTHESIZE_SYSTEM`)은 Phase 2에서 rule 5 추가: 원 질문에 액션이 섞여 있어도 **액션을 언급/약속/거부하지 않고 지식 사실만** 출력한다(read→act에서 액션은 part + AgentWork로 별도 표면).

---

## P2.8 설정 + 플래그 (§19 확장)

| 환경변수 | settings.py 필드명 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED` | `ask_decompose_depends_enabled` | false | Phase 2 위상 실행 on/off (off=Phase 1 byte-identical) |
| `ORTHUS_ASK_DECOMPOSE_MAX_DEPTH` | `ask_decompose_max_depth` | 2 | wave depth 상한 (초과 시 flat 강등) |

---

## P2.9 SSE (§12 유지)

Phase 1 이벤트 순서 유지. 다음 wave leaf는 자연히 늦게 `sub_answer_ready` 발행되므로 FE는 index 추적만으로 충분(별도 `wave_complete` 이벤트 불필요). `upstream_unavailable`로 skip된 leaf도 `sub_answer_ready`(`grounded:false`, `error:"upstream_unavailable"`)를 발행해 FE가 빠짐없이 렌더한다.

---

## P2.10 구현 슬라이스 (MA.6)

| 슬라이스 | 내용 | 통과 게이트 |
| --- | --- | --- |
| **MA.6a** | `split_question` depends_on 확장(backward-only 가드) + `topo_levels` + flag + 스키마. **위상 분류까지만, context 미주입(flat 실행 유지)**. | flag off Phase 1 byte-identical, backward-only/cycle 불가 단위 테스트, `depth_exceeded` flat 강등, 하위호환 파싱(str/dict), 기존 5 router + Phase 1 회귀 PASS |
| **MA.6b** | `context_from` 주입 + `coordinate()` 활성화 + wave loop. read→act e2e. | read→act e2e 인라인 답, context PII redaction 확인, scope 경계 assert, `upstream_unavailable` 전파 + 액션 미큐잉, `coordinate` `next_wave` 결정론, DB 커넥션 부등식 재확인 |
| **MA.6c** | FE: parts `#N` 인덱스 + sub-question 텍스트 + `depends_on` 칩("#N 답변 사용") + `맥락 반영`(context_injected) + `선행 답변 없음`(upstream_unavailable) 렌더. envelope `_enrich_parts`. synthesize 액션 무언급 룰. | 390×844 QA PASS, 폴백 미노출, no h-scroll |

> **flag on 게이트:** MA.6b 완료 + MA.5 측정 긍정 전에 `ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED=true` 운영 적용 금지.

---

## P2.11 수용 게이트 (§20 + 추가)

- **flag off byte-identical:** `ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED=false`에서 Phase 1 출력 바이트 동일.
- **backward-only/cycle 불가:** forward/self/out-of-range index drop, 잔여 cycle은 `depth_exceeded` flat 강등.
- **depth 상한:** `len(levels) > MAX_DEPTH` → flat 강등 + warning.
- **context_from PII redaction:** 주입 전 `redact_pii_text` 통과 확인.
- **scope 경계:** `resolve_context` scope assert, sub-query 타 owner 미노출(K7 B-시리즈 차용).
- **실패 전파:** upstream 실패 → downstream `upstream_unavailable` + 액션 미큐잉.
- **coordinate 결정론:** LLM 입력 경로 미포함, `next_wave` 결정론.
- **사람 검토:** `orthus-operator-reviewer` R1 재검토 — bounded 위상 실행이 LangGraph/오픈 ReAct 아님을 코드 구조(depth cap + backward-only + 결정론 coordinate)로 입증. read→act 3–5 케이스 브라우저 QA(390×844 포함).

---

## P2.12 보존 불변식 (§21 확장 — 위반 시 PR 거부)

17. **Phase 2 위상 실행은 결정론.** `depends_on`은 LLM 입력, 루프 종료·다음 wave 선정은 `coordinate()` 결정론.
18. **acyclic-by-construction + depth bounded.** backward-only 의존성 + `depth ≤ MAX_DEPTH`. 위반 시 flat 강등(전체 실패 아님).
19. **`context_from` 경계.** 주입 전 PII redaction + scope/owner 경계 assert 필수.
20. **upstream 실패 → downstream skip.** 액션 leaf는 큐잉도 하지 않는다(blind 액션 금지).
21. **Phase 2 flag off → Phase 1 byte-identical.** `coordinate()` no-op + `depends_on` 무시 회귀 고정.

---

## Phase 3-A 계약 — 의미기반 답변 캐시 (semantic answer cache)

> **성격:** 본 문서 §2 로드맵의 Phase 3 첫 슬라이스를 §3–§22(Phase 1)·§P2(Phase 2)와 동일한
> 강도로 확정한다. Phase 3은 §2에서 「DAG 오케스트레이터(이벤트 트리거 + 전체 DAG +
> 의미기반 캐시)」로 묶여 있으나, **Phase 3-A는 그중 의미기반 캐시만** 떼어 단독 구현한다.
> 이벤트 트리거·전체 DAG는 Phase 3-B 이후로 미룬다(다중 노드 fan-out은 owner 경계 위반으로 기각, §P3B.4).
>
> **disambiguation:** 본 섹션의 "Phase 3"는 *이 문서의 decompose 로드맵 Phase 3*이며,
> `CLAUDE.md`/`AGENTS.md`의 **P3 Autonomous Agent Work Loop(완료)와 무관**하다. 슬라이스
> 식별자는 Phase 1(MA.1–4)·MA.5(측정)·Phase 2(MA.6) 진행을 이어 **MA.7x**를 쓴다.
>
> **전제:** Phase 1/2 무회귀가 1순위다. 캐시 코드는 기존 `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED`
> (default false) 뒤로 머지하며, off면 `/ask` 출력 바이트가 캐시 도입 전과 동일하다.
> **MA.5 측정 긍정 확인됨(2026-06-25, owner)** — Phase 3-A 진행 게이트 통과.
>
> **기준일:** 2026-06-25.

---

## P3A.0 범위

| 안 | 밖 |
| --- | --- |
| `/ask` 진입부 캐시 lookup → hit 시 재계산 skip | 이벤트 트리거(메일/커넥터 → decompose) (Phase 3-B) |
| company-scope 단일-mode grounded 답 캐시 | 전체 DAG (Phase 3-B) |
| watermark 기반 무효화(staleness) | personal/owner-scope/federated 답 캐시 (하드룰 금지) |
| scope-partitioned 캐시 키(§R7) | decomposed(`mode="decomposed"`) 답 캐시 (MA.7 밖, 후속) |
| `ask_cache` 테이블 + lazy on-read 무효화 | 의미 유사도(embedding) 매칭 (MA.7b, 별도 sub-flag) |
| exact-normalized 질문 매칭 (MA.7a) | TTL 외 능동 sweep / GC 데몬 (선택, MA.7c) |

핵심 사용 패턴: 회사 지식이 안정적인 구간에 **같은(또는 정규화 후 동일한) company 질문 반복** →
LLM/검색 호출 0회로 즉답. 지식이 바뀌면 watermark 전진으로 자동 무효화.

---

## P3A.1 핵심 원칙 (Phase 1 §1 유지 + 추가)

- **SC1. 캐시는 grounding 우회가 아니다.** 캐시는 *이미 grounded된 과거 답*을 재생할 뿐,
  새 RAG/raw-chunk 경로를 만들지 않는다(불변식 2 보존). 캐시 미스는 항상 기존 `answer()`로 떨어진다.
- **SC2. company-scope 전용.** personal/owner-scope/federated(`should_federate=True`) 답은
  **절대 저장하지 않는다**. `ask_cache`에 개인/owner 답 central 자동저장 금지(§15B, §R7, 하드룰).
- **SC3. 결정론 hit/miss.** 캐시 키 + watermark 일치 여부만으로 결정. LLM 판단 입력 경로 없음.
  의미 유사도(MA.7b)는 **고정 임계치 τ**의 결정론 비교이며 별도 sub-flag 뒤에만 둔다.
- **SC4. watermark 무효화.** 캐시 엔트리는 저장 시점 watermark를 박고, 읽을 때 현재
  watermark와 다르면 무효(miss)로 처리한다. 추가로 TTL backstop을 둔다(2중 방어).
- **SC5. flag off byte-identical.** `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=false`면 lookup/store
  경로 자체를 타지 않아 Phase 1/2 출력과 바이트 동일.

---

## P3A.2 캐시 가능 조건 (하드룰 — 모두 성립해야 store)

store 후보는 아래를 **전부** 만족할 때만 `ask_cache`에 기록한다. 하나라도 불성립이면 store skip
(정상 응답은 그대로 반환).

1. `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=true`.
2. `scope == "company"` (즉 `all`/`personal` 제외 — `all`은 personal 혼입 가능, conservative).
3. `should_federate(scope) == False` (federation 경로는 캐시 금지).
4. `is_token == False` (knowledge-token 호출은 캐시 미진입 — agentic 가드와 동일 정책).
5. `context_wiki_slug is None` **AND** `history` 없음 — 캐시 키는 `(question, scope, project)`뿐이므로
   **request-scoped grounding 입력**(wiki-page context, 대화 history)이 있으면 캐시하지 않는다(다른
   context/session 답을 재생할 위험).
6. 결과가 **`mode == "wiki"`** **AND** grounded(본문 비어있지 않고 "근거 없음" 미포함) **AND** `gap` 없음.
   structured(`notion_rows` substrate)와 graph-success(KG substrate)는 wiki_pages watermark가 커버하지
   않아 **제외**한다. KG outage로 wiki로 demote된 transient 답(`_GRAPH_DEGRADED_MESSAGE`/
   `_CONFLICT_VIEW_UNAVAILABLE` 경고 보유)도 제외(D6 — KG 복구 후 stale 재생 방지).
7. owner-scope 콘텐츠 미포함 (company 답이므로 `owner_id` 없음 — 키에는 sentinel로 박음).
   `cache_store`/`cache_lookup`은 자체적으로 `scope != "company"`를 거부한다(defense-in-depth, 불변식 22).

> **decompose leaf/fall-through 호출은 `allow_cache=False`** — sub-answer(일회성 context 주입 포함)와
> 단일 fall-through는 캐시하지 않고, 캐싱은 최상위 `answer()` wrapper만 담당한다(불변식 22, 중복 store 방지).
> gap/`grounded=False`/error/structured/graph/decomposed 답은 캐시하지 않는다(틀린 "모름"·낡은 집계·
> 비-wiki substrate를 굳히지 않기 위함). structured/graph 캐시는 각자 substrate watermark가 필요해 MA.7
> 범위 밖(후속 재검토).

---

## P3A.3 캐시 키 + watermark

### P3A.3.1 캐시 키 구성 (§R7 — 전부 필수)

```python
# 논리 키 (저장·조회 모두 동일 partition tuple + 질문 식별자)
CacheKey(
    owner_id: str,      # company 답 → 고정 sentinel "company" (개인 답은 애초에 store 안 함)
    scope: str,         # "company" 고정 (P3A.2)
    project: str,       # 부모 effective project (company|atlas|nova|orbit)
    federation: bool,   # False 고정 (P3A.2)
    node_id: str,       # 현재 node 식별자
    watermark: str,     # P3A.3.2 — 해당 partition 지식 버전
    question_key: str,  # P3A.4 — 정규화 질문의 sha256 (MA.7a) / + embedding (MA.7b)
)
```

- partition tuple(`owner_id+scope+project+federation+node_id`) 누락 시 캐시 빌드 실패(§수용 게이트).
- **질문 원문 평문을 키로 저장하지 않는다** — `question_key`는 정규화 질문의 sha256 해시.
  (PII 노출 방지. MA.7b embedding은 별도 컬럼에 vector + redacted 질문 보관 — P3A.4.)

### P3A.3.2 watermark 표현 — 대안 (구현 시 1개 채택; 권장 = B′)

| 안 | 표현 | 삭제 감지 | 쓰기 훅 | 비용 | 비고 |
| --- | --- | --- | --- | --- | --- |
| A | `MAX(wiki_page.updated_at)` (partition별) | ✗ (삭제는 MAX 전진 안 함) | 불필요 | 집계 1회 | 신규 테이블 0, 단 hard-delete blind |
| **B′ (권장)** | `(row_count, MAX(updated_at))` 복합 (partition별) | ✓ (삭제→count 감소) | 불필요 | 집계 1회(count+max) | 수정=max 전진, 삭제=count 감소로 둘 다 포착. 동률 비교(equality)라 max 후퇴 무관 |
| C | 증가 generation 카운터 (`:KgMeta`식 단조 정수) | ✓ | **필요** (`wiki/store.py::_persist` 등 단일 쓰기 지점) | 조회 1회 | 가장 정확하나 쓰기 경로 결합 + 저장소(작은 테이블/row) 추가. K3 outbox enqueue 자리 재사용 가능 |

> **권장 B′ 근거:** `:KgMeta.last_sync_at`(watermark) 선례와 달리 쓰기 훅 없이 read-time 집계만으로
> 수정·삭제를 모두 포착하고, equality 비교라 "MAX 후퇴(=max row 삭제)" 엣지도 count 변화로 잡힌다.
> 정확도가 더 필요하면 C로 승급(쓰기 훅 + generation row). **최종 채택은 MA.7a 구현 PR에서 확정**하고
> 본 표의 trade-off를 설계 이력에 남긴다.
>
> **primitive 위치:** `orthus/router/cache.py::knowledge_watermark(scope, project, node_id) -> str`
> 단일 함수로 캡슐화(B′→C 교체 시 호출부 무변경). corpus 변경이 wiki page에 반영되는 경로(distill/
> consolidate가 `wiki_page.updated_at` 갱신)를 watermark 소스로 삼는다 — 답변 그라운딩이 compiled
> wiki page 전용(불변식 2)이므로 wiki page 집계가 곧 "답에 영향을 주는 지식 버전"이다.

### P3A.3.3 무효화 (lazy on-read)

```text
lookup(key_partition, question_key):
  row = SELECT ... WHERE partition match AND question_key match
  if not row:                         → MISS
  if row.watermark != current_watermark(partition):  → MISS (stale; 행은 남겨두고 store가 덮어씀)
  if row.created_at + TTL < now:      → MISS (backstop)
  else:                               → HIT (row.answer 반환)
```

- **lazy(읽을 때 검증)** 채택 — 쓰기 경로 결합 없음. B′ 집계가 충분히 싸서 read마다 계산해도 됨.
- stale/만료 행은 즉시 삭제하지 않고 다음 store가 같은 키로 덮어쓴다(upsert). 누적분은 TTL GC(선택 MA.7c).

---

## P3A.4 의미 매칭 방식 — 대안 (권장: MA.7a exact → MA.7b embedding 단계적)

| 안 | 매칭 | 장점 | 단점 |
| --- | --- | --- | --- |
| **MA.7a (권장 시작)** | 정규화 exact (lowercase·strip·공백 collapse·구두점 정리) sha256 | 결정론 100%, embedding 호출 0, false-hit 없음 | "지난주 매출?" vs "지난주 매출 얼마야?" 미스 (진짜 의미 매칭 아님) |
| **MA.7b (후속)** | 질문 embedding cosine ≥ τ (pgvector) + partition 필터 | 패러프레이즈 흡수(진짜 semantic) | embedding 호출 1회 + 유사도 false-hit 위험. τ 튜닝 필요. **별도 sub-flag** |

> "의미기반 캐시"의 최종 목표는 MA.7b이지만, **watermark 무효화 + 키 partition + store 게이트가
> 검증되기 전엔 exact(MA.7a)로 시작**한다 — 결정론·무위험으로 캐시 인프라를 먼저 굳히고, 그 위에
> embedding 레이어를 얹는다. MA.7b는 `ask_cache.question_embedding`(pgvector) + redacted 질문 텍스트를
> 추가하고, τ는 고정 상수(LLM 아님)로 둔다(SC3).

---

## P3A.5 데이터 흐름 (`answer()` 진입점 hook)

```text
answer(question, scope, ...):
  │
  ├─ [캐시 lookup] (SC2 가능 조건 통과 시에만)
  │    if cacheable_request(scope, is_token, federation):
  │        key = build_partition_key(...) ; qk = question_key(question)
  │        hit = cache_lookup(key, qk)        # P3A.3.3 watermark+TTL 검증
  │        if hit: audit("ask.cache", result="hit"); return hit.answer   ← 재계산 skip
  │
  ├─ [정상 경로] decompose > agentic > legacy (§3 우선순위 그대로)
  │    routed = <기존 answer 본체>
  │
  └─ [캐시 store] (P3A.2 store 게이트 전부 통과 시에만)
       if cacheable_result(routed):
           cache_store(key, qk, routed, watermark, now)   # upsert
           audit("ask.cache", result="store")
       return routed
```

- lookup은 **decompose/agentic/legacy 분기보다 앞**에 둔다(어느 경로의 답이든 동일 키로 재생).
- store는 본체 계산 후, 결과가 store 게이트(P3A.2)를 통과할 때만. miss든 store-skip이든 정상 답 반환.
- 캐시는 `orthus/router/cache.py`에 독립 모듈로. `answer()`는 flag off면 두 hook 모두 no-op.

---

## P3A.6 스키마 / 테이블 (`ask_cache` — 신규 migration)

```text
ask_cache (
  id            UUID PK,
  owner_id      TEXT NOT NULL,        -- company 답 sentinel "company"
  scope         TEXT NOT NULL,        -- "company"
  project       TEXT NOT NULL,
  federation    BOOLEAN NOT NULL,     -- False
  node_id       TEXT NOT NULL,
  question_key  TEXT NOT NULL,        -- 정규화 질문 sha256 (MA.7a)
  question_embedding  VECTOR NULL,    -- MA.7b: sub-flag on일 때만 채움 (off=NULL)
  question_redacted   TEXT NULL,      -- MA.7b: redact_pii_text(normalize) 텍스트 (migration 0077, off=NULL)
  watermark     TEXT NOT NULL,        -- 저장 시점 partition 지식 버전
  answer_json   JSONB NOT NULL,       -- 직렬화된 RoutedAnswer (company grounded)
  created_at    TIMESTAMPTZ NOT NULL,
  UNIQUE (owner_id, scope, project, federation, node_id, question_key)  -- upsert 키 (partition-only 조회도 커버)
)
-- MA.7b (migration 0077): question_redacted 컬럼 + question_embedding ivfflat
--   인덱스(vector_cosine_ops, lists=100) 추가.
-- watermark 집계 가속용 covering index (migration 0076, wiki_pages):
--   CREATE INDEX idx_wiki_pages_company_watermark ON wiki_pages
--     (scope, project, updated_at) WHERE owner_id IS NULL;
-- → company partition의 COUNT+MAX(updated_at)을 index-only scan으로 (전체 heap scan 회피).
```

- `answer_json`은 company-scope grounded `RoutedAnswer`만 — owner/personal 콘텐츠 구조적 부재(P3A.2).
  저장 시 `stream_id`는 null로 비운다(request-scoped SSE id가 캐시 hit로 재생되지 않도록).
- 별도 row 모델은 두지 않는다 — `cache_lookup`이 `RoutedAnswer.model_validate(answer_json)`로 직접
  역직렬화하고, schema drift는 `ValidationError`만 잡아 miss로 강등한다.

---

## P3A.7 PII / redaction 경계

- **저장 콘텐츠:** company-scope grounded 답은 *이미 redaction 통과한 compiled wiki page*에
  그라운딩된 결과다(authoring 시 `redact_pii_text` 통과 — 불변식 6). 캐시는 새 raw 경로를 만들지
  않으므로 추가 PII 유입 없음. 그래도 `answer_json` 저장 전 기존 답변 직렬화 경로의 redaction 규약을
  그대로 따른다.
- **키:** `question_key`는 평문이 아닌 sha256 → 질문 PII가 행에 남지 않음(MA.7a). MA.7b가 redacted
  질문 텍스트/embedding을 저장할 때도 `redact_pii_text` 선통과.
- **audit:** `audit("ask.cache")` span은 hit/store/miss 결과·partition·`correlation_id`만 기록하고
  질문 원문/답 본문은 남기지 않는다(query_runs redaction 규약 준수).

---

## P3A.8 설정 + 플래그 (§19 확장)

| 환경변수 | settings.py 필드명 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED` | `ask_semantic_cache_enabled` | false | Phase 3-A 캐시 on/off (off=Phase 1/2 byte-identical) — **기존 필드 재사용** |
| `ORTHUS_ASK_CACHE_TTL_SEC` | `ask_cache_ttl_sec` | 86400 | TTL backstop (초). watermark가 1차, TTL은 2차 방어 |
| `ORTHUS_ASK_CACHE_SEMANTIC_MATCH_ENABLED` | `ask_cache_semantic_match_enabled` | false | MA.7b embedding 유사도 매칭 sub-flag (off=exact only) |
| `ORTHUS_ASK_CACHE_SIMILARITY_THRESHOLD` | `ask_cache_similarity_threshold` | 0.95 | MA.7b 고정 τ (LLM 아님) |

---

## P3A.9 감사

- 신규 span: `ask.cache` (meta: `result=hit|store|miss`, partition, `correlation_id`).
- 캐시 hit여도 `/ask` 응답 envelope은 동일(추가 필드 없이 동일 `RoutedAnswer`). hit/miss는 audit로만.

---

## P3A.10 구현 슬라이스 (MA.7)

| 슬라이스 | 내용 | 통과 게이트 |
| --- | --- | --- |
| **MA.7a** | `ask_cache` 테이블 + migration + `AskCacheEntry` 스키마 + `knowledge_watermark`(B′) + exact-normalized `question_key` + lookup/store hook + watermark/TTL 무효화. flag off byte-identical. | flag off `/ask` 바이트 동일, store 게이트(company+grounded+단일-mode only) 단위 테스트, personal/all/federated/token/decomposed/gap 답 **미캐시** 테스트, watermark 변경 시 miss 테스트, TTL 만료 miss, partition 누락 시 빌드 실패, 기존 5 router + Phase 1/2 회귀 PASS |
| **MA.7b 구현** | embedding 유사도 매칭 (sub-flag) + `question_embedding` 채움 + `question_redacted` 컬럼(migration 0077) + ivfflat 인덱스 + 고정 τ. exact 미스 시에만 동작, store는 sub-flag on일 때만 embedding/redacted 텍스트 기록(off=NULL, MA.7a byte-identical). 임베딩 입력 = `redact_pii_text(normalize_question(q))`. 조회는 partition btree(`uq_ask_cache_key`)로 partition 행을 정확히 distance 정렬(selective-partition 질의라 ivfflat 미사용 — 근사 아님; threshold post-filter는 안전망)해 최근접 K개를 id+distance로 랭크하고 반환 행만 `answer_json` 로드(`≤ 1−τ` 첫 valid hit). cold-path 가드: partition에 live embedded 행이 없으면 embedding 호출 자체를 skip(never-asked /ask가 provider round-trip 미지불). 임베딩 호출은 `audit("ask.cache.embed")` span(원칙 3). 임베딩 provider 실패는 store=exact-only row, lookup=miss로 degrade(`/ask` 불실패). sub-flag off로 re-store해도 기존 embedding/redacted 보존(NULL wipe 금지). audit는 exact/semantic stage 구분(`miss_semantic`/`miss_decode_semantic`/`miss_embed_error` + `exact=` meta). | sub-flag off=exact only 동일, τ 경계 hit/miss 결정론, redacted 질문/embedding 저장 PII 통과, cross-partition 격리(다른 owner/project/node 미히트), cold-partition embed skip·embed-error degrade·toggle-off embedding/redacted 보존·corrupt nearest skip·audit span/stage meta 방출. `tests/integration/test_ask_cache.py` MA.7b 17종 PASS |
| **MA.7c 구현** | `ask_cache` TTL/stale 행 GC. lazy on-read 무효화는 DELETE를 하지 않아(같은 키 재질의 시 upsert로만 덮어씀) 재질의 안 되는 행 + watermark 전진으로 orphan된 행이 누적된다. `orthus/router/cache.py::gc()`가 (1) TTL 만료 행(`created_at + TTL < now`, watermark 무관) + (2) watermark-stale 행(이 노드 company partition별 현재 watermark != 저장 watermark)을 삭제한다. live 행(현재 watermark + TTL 내)은 보존 → GC 전 HIT하던 lookup이 GC 후에도 HIT. KG outbox `trim` 선례를 따른 CLI(`python -m orthus.router.cache gc`) + `make ask-cache-gc`/`node-ask-cache-gc` 트리거(운영자/launchd 주기 실행), 런타임 무결합. `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED` 무관 실행(죽은 행 정리는 캐시 off여도 무해; lookup/store hook은 그대로 flag-gated). 결정론, LLM/embedding 호출 0. `audit("ask.cache.gc")` span(deleted_ttl/deleted_stale/remaining). | 누적 행 bound(watermark 전진 N회 후 live 1행으로 수렴), GC가 정상 hit 무영향, TTL/stale 삭제, project partition 격리, flag-무관 실행, audit span, CLI exit code — `tests/integration/test_ask_cache.py` MA.7c 8종 PASS |

> **활성화 게이트:** Phase 3-A는 MA.5 긍정으로 진행 가능하나, `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=true`
> **운영 적용**은 MA.7a 캐시 정확도(stale 무효화) + owner-scope 미캐시 경계 검증 후. cross-owner leak이
> 가장 큰 리스크(R7)이므로 partition 격리 테스트 통과 전 prod on 금지.

---

## P3A.11 수용 게이트 (§20 + 추가)

- **flag off byte-identical:** `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=false`에서 `/ask` 출력 바이트 동일.
- **store 게이트:** company + `should_federate=False` + 단일-mode + grounded + non-error + non-token만 store.
  personal/`all`/federated/token/decomposed/gap/error 답은 store 안 됨(각각 테스트).
- **watermark 무효화:** partition 지식 변경(wiki page upsert/삭제) 후 동일 질문 → miss + 재계산 + 덮어쓰기.
- **TTL backstop:** 만료 행 miss.
- **partition 키 필수:** `owner_id+scope+project+federation+node_id+watermark` 누락 시 캐시 빌드 실패(R7).
- **cross-owner/partition 격리:** 다른 owner/project/node/federation 키로 저장된 행이 현재 요청에 미히트.
- **결정론 hit/miss:** LLM 입력 경로 미포함. MA.7b τ는 고정 상수.
- **PII:** `question_key`는 해시(평문 아님), audit span에 질문/답 본문 미기록.
- **grounding 불변식:** 캐시 hit 답은 원래 compiled-wiki 그라운딩 답(raw-chunk RAG 신규 경로 없음).
- **사람 검토:** `orthus-operator-reviewer`로 SC2(company-only)·R7(cross-owner leak) 경계를 코드 구조로 입증.

---

## P3A.12 보존 불변식 (§21 확장 — 위반 시 PR 거부)

22. **캐시 = company-scope 전용.** personal/owner-scope/federated 답 `ask_cache` 저장 금지(§15B/§R7 하드룰).
23. **캐시 키 = partition 전부 + watermark + question_key.** `owner_id+scope+project+federation+node_id+watermark` 누락 시 빌드 실패.
24. **watermark 무효화.** 지식 변경 시 stale 캐시는 hit되지 않는다. 캐시는 grounding 우회 아님(이미 grounded 답 재생).
    watermark는 wiki_pages `(count, MAX(updated_at))`이며, canonical write 경로(`wiki/store.py::_persist`)가
    update 시 `updated_at`을 bump한다. updated_at을 bump하지 않는 비표준 in-place edit은 watermark가
    잡지 못하므로 TTL backstop이 1차 안전망이다(B′ 한계, 문서화된 trade-off).
25. **결정론 hit/miss.** LLM 입력 경로 없음. embedding 유사도는 고정 τ + 별도 sub-flag.
26. **wiki-mode grounded만 캐시.** `mode=="wiki"` + grounded + non-gap만. structured(notion_rows)·
    graph-success(KG)·transient graph-demote·decomposed·gap·error 답은 캐시 금지(substrate watermark 불일치/transient).
27. **flag off byte-identical.** `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED=false`면 lookup/store hook no-op.
28. **request-scoped 입력 미캐시.** `context_wiki_slug`/`history`가 있는 요청은 캐시 키에 그 차원이 없으므로
    캐시하지 않는다(cross-context/session 재생 방지). decompose leaf/fall-through는 `allow_cache=False`.

---

## Phase 3-B 계약 — 이벤트 트리거 오케스트레이션 + 전체 DAG

> **성격:** 본 문서 §2 로드맵의 Phase 3-B를 §3–§22(Phase 1)·§P2(Phase 2)·§P3A(Phase 3-A)와
> 동일한 강도로 확정한다. §2는 Phase 3-B를 「DAG 오케스트레이터(이벤트 트리거 + 다중 Personal Node
> + 전체 DAG)」로 묶었으나, 위험·아키텍처 결합도가 서로 달라 **단일 슬라이스로 구현하지 않는다.**
> 본 계약은 이를 분해해 **권장 진행 순서**(MA.8a → MA.8b)를 확정하고, **다중 Personal Node fan-out은
> owner 경계 위반으로 기각**한다(§P3B.4 — caller의 personal ∪ company 평면만 읽으며, 다른 owner의
> personal 노드는 어떤 sub-question도 건드리지 않는다).
>
> **disambiguation:** 본 섹션의 "Phase 3-B"는 *이 문서의 decompose 로드맵* 슬라이스이며,
> `CLAUDE.md`/`AGENTS.md`의 P-시리즈(P3 Autonomous Agent Work Loop / P8 Central Consolidation)와
> 다르다. 슬라이스 식별자는 Phase 1(MA.1–4)·MA.5(측정)·Phase 2(MA.6)·Phase 3-A(MA.7)를 이어 **MA.8x**.
>
> **전제:** Phase 1/2/3-A 무회귀가 1순위다. 신규 경로는 전부 전용 fail-closed flag(default false)
> 뒤로 머지하며, off면 `/ask`·mail ingest·decompose 출력이 도입 전과 바이트 동일하다.
> **운영 적용은 여전히 MA.5 측정 게이트 + 각 슬라이스 수용 게이트.**
>
> **기준일:** 2026-06-26.

---

## P3B.0 범위

| 안 | 밖 |
| --- | --- |
| **MA.8a** 이벤트 트리거: mail/connector ingest → 비동기 decompose 오케스트레이션 → AgentWork/draft sink | `/ask` 동기 요청 경로 변경(무회귀) |
| **MA.8b** 전체 DAG: `MAX_DEPTH` 상향(depth > 2) + width×depth 비용 가드 | 일반 ReAct / 오픈루프 / LLM-driven 루프 제어 |
| 이벤트→오케스트레이션 loop/storm 가드 | 다중 Personal Node fan-out (owner 경계 위반 — 기각, §P3B.4) |
| 비동기 결과 sink = 기존 AgentWork policy gate | 신규 RAG/raw-chunk 경로 (불변식 2 유지) |
| — | LLM-only 실행 (불변식 3 유지) |

### P3B.0.1 sub-capability 분해 + 권장 순서

| 슬라이스 | 기능 | 결합도/위험 | 기존 인프라 재사용 | 권장 |
| --- | --- | --- | --- | --- |
| **MA.8a** 이벤트 트리거 | mail/connector ingest 이벤트가 decompose 오케스트레이션을 **비동기** 기동, 결과는 AgentWork/draft | 낮음 — mail→AgentWork(P7.1) 인프라와 정렬, P8/하드룰 충돌 적음 | `mail/ingest.py:231` 트리거 지점(`_maybe_enqueue_event_orchestration`), `build_reply_candidate`, P3 policy gate, decompose 엔진 | **1순위** |
| **MA.8b** 전체 DAG | Phase 2 wave loop를 depth > 2로 확장(read→act→act 등) | 중 — LangGraph 하드룰(R1) 경계. 단 wave 머신은 이미 일반화됨(아래) | `_run_waves`(`decompose.py:861`)·`topo_levels`·`coordinate` 그대로, `MAX_DEPTH` cap만 상향 | **2순위** |
| ~~다중 노드 fan-out~~ | decompose 질의를 여러 personal collector 노드로 fan-out | — | (신규 inbound serve 필요 — owner 경계 위반) | **기각 (§P3B.4)** |

> **`_run_waves`는 이미 일반 depth-N 머신이다.** Phase 2(`decompose.py:861`)가 `topo_levels` →
> wave barrier loop를 구현했고, `len(levels) > max_depth`일 때만 flat 강등한다. 따라서 MA.8b의
> 실질 작업은 **(a) `MAX_DEPTH` cap 상향, (b) width×depth 비용/커넥션 가드, (c) context_from 체인
> redaction 누적 검증, (d) depth 3–4 회귀 + operator review**이지, 새 루프 프레임워크가 아니다.

핵심 사용 패턴:
- MA.8a: 회사 메일 수신 → "이 메일 관련 회사 지식 + 과거 회신 맥락 조회 + 답장 초안"을 사람이 `/ask`를
  치지 않아도 **백그라운드로** 미리 준비해 AgentWork/draft로 올림.
- MA.8b: "지난주 매출 조회 → 그 숫자로 보고서 초안 작성 → 김부장에게 메일 초안" 같은 **3+단계 read→act 체인**.

---

## P3B.1 핵심 원칙 (Phase 1 §1 / Phase 2 §P2.1 / Phase 3-A §P3A.1 유지 + 추가)

- **E1. 이벤트 트리거는 비동기, /ask 동기 경로 불변.** MA.8a는 `/ask` HTTP 핸들러를 건드리지 않는다.
  mail/connector ingest **후처리 훅**에서 기동하며, 동기 사용자 대기가 없으므로 §MA.5(d) 5초 inline
  예산이 적용되지 않는다(백그라운드 job). 결과는 inline SSE가 아니라 **AgentWork/draft sink**로 간다.
- **E2. 이벤트→오케스트레이션은 단발(one-shot), loop 금지.** 한 이벤트가 기동한 오케스트레이션의
  출력(AgentWork·draft·메일 초안)이 **새 트리거 이벤트를 다시 발생시키지 않는다**(storm/재귀 차단).
  트리거 깊이는 구조적으로 1(event → orchestration → sink), 그 이상 없음. → R16.
- **E3. 액션은 여전히 P3 policy gate 경유만.** 이벤트가 기동했어도 action-intake leaf는
  `draft_for_review` default. auto-send/auto-execute는 기존 P7.5/P3 게이트(opt-in bucket + kill
  switch) 통과 시에만. **이벤트 트리거가 액션 자동 승인을 의미하지 않는다**(불변식 7/20 유지).
- **E4. 전체 DAG는 여전히 결정론 bounded 위상 실행.** depth를 늘려도 (a) backward-only(acyclic-by-
  construction), (b) `depth ≤ MAX_DEPTH` bounded, (c) `coordinate()` 결정론은 유지(불변식 17/18).
  cap 상향이 LangGraph 전환이 아님을 operator review로 입증한다. → R1 재검토.
- **E5. 다중 Personal Node fan-out은 owner 경계 위반으로 기각.** 한 `/ask`/오케스트레이션이
  읽을 수 있는 평면은 **caller의 personal ∪ company뿐**이다 — 다른 owner의 personal 노드는 어떤
  sub-question도 건드리지 않는다(§11/불변식 5). caller 평면에 근거가 없으면 답하지 않고 gap으로 둔다.
  조직 전체 집계는 company promote 경유만 가능하다. → 다중 노드 inbound fan-out은 짓지 않는다(§P3B.4).
- **E6. flag off byte-identical.** 모든 MA.8x flag off면 mail ingest / decompose / `/ask` 출력이
  도입 전과 바이트 동일.

---

## P3B.2 MA.8a — 이벤트 트리거 오케스트레이션 계약

### P3B.2.1 트리거 소스 + 진입점

기존 단일-후보 훅(`mail/ingest.py:282` — `mail_reply_draft_enabled`일 때 `build_reply_candidate`
1개 생성)을 **일반화**한다. 이벤트가 *복합 후처리*(지식 조회 + 회신 맥락 + 초안)를 요할 때
decompose 오케스트레이션을 비동기 기동한다.

```text
mail/connector ingest (changed=True)
  │
  ├─ [기존] mail_reply_draft_enabled → build_reply_candidate (단일 후보) — 무변경
  │
  └─ [신규 MA.8a] ask_event_orchestration_enabled AND 트리거 조건 충족
       → enqueue_event_orchestration(event, owner_user_id, settings)
            │ (동기 ingest 트랜잭션을 막지 않는다 — 별도 비동기 job/worker)
            └─ answer_or_decompose(seed_question, scope="company",
                   allow_decompose=True, learn=False, record_gaps=False,
                   allow_cache=False, sink="agent_work")
```

- **트리거 조건은 결정론.** "이 이벤트가 오케스트레이션 대상인가"는 LLM이 아니라 결정론 규칙(예:
  inbound company mail + 본문에 질문/요청 신호). `seed_question` 합성도 결정론 템플릿(메일 제목/발신자
  기반) — LLM은 이후 leaf 내부에서만(기존 경로 그대로).
- **비동기 실행 모델 — 대안 (owner 결정 2026-06-26 = job 큐+worker).**

| 안 | 모델 | 장점 | 단점 |
| --- | --- | --- | --- |
| A | ingest 핸들러 내 inline 동기 실행 | 신규 인프라 0 | ingest 트랜잭션·pull 사이클 지연(오케스트레이션 수초+), 실패가 ingest에 전파 |
| B | `agent_work` family job으로 enqueue → 기존 worker/스케줄러가 소비 | sink과 동일 substrate | **소비할 agent_work job-runner가 현재 없음** — net-new |
| **C (구현 채택)** | 전용 `ask_event_jobs` 큐 + 전용 lifespan worker (K3 `kg_outbox` 패턴 재사용) | ingest 무영향(post-commit enqueue), claim/lease/dead-letter 검증된 패턴, flag-off byte-identical | 전용 테이블/worker 1개 |

> **채택 C 근거:** 안 B는 "기존 worker가 소비"를 전제하지만 agent_work에는 그런 job-runner가 없어
> 결국 net-new다. 그래서 **K3 `kg_outbox`의 큐+lifespan worker 패턴을 그대로 재사용**해 전용
> `ask_event_jobs` 큐 + `EventOrchestrationWorker`를 둔다(`FOR UPDATE SKIP LOCKED` + lease +
> 5회 dead-letter). enqueue는 mail ingest **post-commit**라 ingest 트랜잭션·P7.1 경로 무영향이고,
> flag off면 worker 스레드가 아예 안 뜬다(불변식 32). sink(지식 브리프)은 여전히 AgentWork이라
> 정책 게이트/audit/`/agent-work` 표면을 그대로 쓴다. CLI `python -m orthus.router.event_orchestration drain`.

### P3B.2.2 결과 sink (owner 결정 2026-06-26 = 지식 브리프만)

- **MVP sink = 지식 브리프 1건.** seed를 *지식 질문*으로 framing해(`build_mail_seed_question`)
  decompose가 회사 위키를 조회·합성한 `synthesized_body`를 `event_orchestration` AgentWork item
  (`source_kind`/`action_family` = `event_orchestration`)으로 적재한다. 정책 게이트가 **결정론
  `draft_for_review`**(외부 쓰기 없음, owner review)로 고정한다(`apply_policy` 신규 분기).
- **답장은 기존 P7.1 경로가 그대로 담당**(불변). MA.8a는 답장을 만들지 않는다 — 지식 브리프만 추가한다.
  지식→답장 연결(회신이 조회 결과를 반영)은 **MA.3 어댑터 + MA.3b leaf 배선 + Phase 2 `context_from`**
  이다. **MA.3/MA.3b 구현 완료**: MA.3 typed `create_reply_draft` 어댑터(build+persist) + MA.3b
  `/ask context_mail_id` 배선 + decompose action-intake leaf의 reply-draft 재사용(D안 —
  `find_reply_draft_for_mail`로 P7.1 초안 조회·링크, 없으면 `queue_agent_work` fallback). **단 MA.8a
  이벤트 트리거 자신은 여전히 답장을 만들지 않는다** — `knowledge_only_leaves`로 action-intake를 억제하고
  지식 브리프만 적재한다(double-draft 회피, 불변식 29 유지). 즉 사용자가 mail 컨텍스트로 연 `/ask`
  복합질문에서만 답장 초안이 재사용·링크되며, 오케스트레이션 sink는 불변이다.
- 단일 fall-through(`mode != "decomposed"` 또는 빈 합성)면 worker는 **빈 브리프를 적재하지 않고** job만
  done 처리한다(노이즈 억제).
- **이벤트 결과는 SSE를 쓰지 않는다**(동기 클라이언트 없음). 진행 표면은 `/agent-work` + `audit("router.event_orchestration")`.

### P3B.2.3 loop/storm 가드 (E2, R16)

- 유일한 트리거 소스는 `"mail"`이다. sink(`event_orchestration` AgentWork item)은 **트리거 소스가
  아니므로**(`ask_event_jobs.source_kind`에 들어오지 않음) 큐는 **구조적으로 비순환**이다(불변식 29).
  회귀 테스트 `test_worker_loop_impossible`로 고정.
- 동일 이벤트 idempotency: `ask_event_jobs (source_kind, source_ref)` UNIQUE + `ON CONFLICT DO NOTHING`
  으로 같은 메일 재pull 시 재enqueue를 차단한다(`source_ref = "mail:<canonical_id>"`).
- 폭주 상한: `ORTHUS_ASK_EVENT_ORCH_MAX_PER_CYCLE`(default 5) — pending job 수가 이 값 이상이면 새
  enqueue를 skip해 한 pull 사이클이 worker를 flood하지 못하게 한다(큐 bound).

---

## P3B.3 MA.8b — 전체 DAG (depth > 2) 계약

### P3B.3.1 변경점

Phase 2의 `_run_waves`(`decompose.py:861`)·`topo_levels`·`coordinate()`를 **그대로** 쓴다. 변경은:

- `MAX_DEPTH` cap 상향: `ORTHUS_ASK_DECOMPOSE_MAX_DEPTH` default 2 유지, **상한값을 별도 flag로** 허용
  (`ORTHUS_ASK_DECOMPOSE_FULL_DAG_ENABLED` on일 때만 depth > 2 허용; off면 기존 flat 강등 그대로).
- **width×depth 비용/커넥션 가드** (§16 부등식 확장): depth가 늘면 wave 누적 leaf 수가 증가 →
  `Σ(wave별 leaf 수) × leaf당 DB 커넥션 ≤ pool_size+max_overflow` 재계산 + 총 leaf 수 상한
  (`ORTHUS_ASK_DECOMPOSE_MAX_TOTAL_LEAVES`, depth×width 폭발 차단).
- **context_from 체인 redaction 누적 검증** (§P2.4 확장): depth N에서 upstream 맥락이 hop마다
  prepend되며 길어진다 → 각 hop `redact_pii_text` 통과 + 누적 길이 상한(token budget).

> **구현 (2026-06-26):** `orthus/settings.py`에 `ask_decompose_full_dag_enabled`(default false),
> `ask_decompose_max_total_leaves`(default 12) 추가. `answer_or_decompose()`에서
> `effective_max_depth = ask_decompose_max_depth if full_dag else min(ask_decompose_max_depth, 2)`로
> 게이팅하고(off면 depth > 2가 `_run_waves`의 기존 `len(levels) > max_depth` flat 강등으로 떨어진다),
> split 결과 `len(sub_questions) > max_total_leaves`이면 fan-out 대신 단일 `answer()`로 강등한다
> (`reason="total_leaves_exceeded"`; **full DAG flag on일 때만 적용** — off면 가드 비활성이라 Phase 1/2가
> 어떤 K 설정에서도 byte-identical, 불변식 32). `resolve_context()`는 hop별 `redact_pii_text` 뒤 누적 결과를
> `_MAX_CONTEXT_CHARS`(4000)로 절단한다. `tests/unit/test_ask_decompose_ma8b.py` 13종 PASS
> (depth 3–4 wave 실행, off→cap 2 flat 강등, total-leaves 강등, 누적 context 절단, hop redaction,
> depth-4 acyclic). 신규 마이그레이션·테이블 없음.
>
> **§16 DB 커넥션 부등식 재계산 (depth×width):** 동시 leaf 수는 depth와 무관하게 여전히
> `MAX_CONCURRENCY`(default 4) 세마포어가 상한이다 — wave 사이에는 `coordinate()` barrier가 있고,
> wave 내부 `fan_out`은 같은 세마포어를 공유한다. 따라서 depth를 늘려도 **peak 커넥션은 불변**이고
> (waves는 순차), 부등식은 Phase 2와 동일하다:
> `leaf당 커넥션(≤2) × MAX_CONCURRENCY(4) × 동시 decompose 요청 ≤ pool_size(10)+max_overflow(30)`
> → 요청당 ≤ 8, 약 5 동시 요청까지 안전. `MAX_TOTAL_LEAVES`는 peak가 아니라 **총 작업량(누적 leaf =
> 총 sub-question 수)** 상한이며, 초과 시 decompose를 포기(단일 answer 강등)해 비용 폭발만 막는다.

### P3B.3.2 LangGraph 하드룰(R1) 경계 입증

depth 상향이 오픈 ReAct가 아님을 **코드 구조로** 입증한다(`orthus-operator-reviewer` 필수):

- (a) backward-only 의존성 → cycle 구조적 불가(MA.6a 가드 그대로).
- (b) `depth ≤ MAX_DEPTH` bounded — cap이 상수 flag, LLM이 늘리지 못함.
- (c) `coordinate()` 결정론 loop-continue — LLM 입력 경로 없음(불변식 17/25).
- (d) wave 내부는 read-only 병렬, 액션은 P3 gate. → "bounded 위상 실행"이 정확한 명칭(불변식 16).

---

## P3B.4 다중 Personal Node fan-out — 기각 (owner 경계 위반)

decompose 질의를 여러 personal collector 노드로 fan-out하는 능력은 **기각한다.** 슬라이스로 짓지 않으며
재론 대상도 아니다.

**근거 (owner 평면 경계, 불변식 5/30, §11):** 한 사용자의 `/ask`(및 그 이벤트 오케스트레이션)가 읽을 수
있는 지식 평면은 **caller의 personal ∪ company뿐**이다. **개인은 다른 개인의 personal 노드에 들어갈 수
없다.** 따라서 "여러 personal 노드를 inbound로 부르는" fan-out은 정의상 이 경계를 깬다.

| 질문의 근거가 있는 곳 | 처리 |
| --- | --- |
| company 노드로 promote되어 있음 | company scope로 답 가능(누가 묻든) |
| caller 자신의 personal 노드에 있음 | personal-own scope로 답 가능 |
| **다른 owner의 personal 노드에만** 있고 company엔 없음 | **답하지 않고 gap.** 그 노드를 긁지 않는다 |

남는 합법적 형태는 두 가지뿐이고 둘 다 신규 코드가 필요 없다: (1) caller 자신의 personal + company =
이미 `scope="all"` federation/owner-scope가 처리, (2) 조직 전체 집계 = company promote 게이트 경유.

P8.8 cutover 이후 personal 데이터가 central에 owner-row 경계로 들어오면 "다중 노드"라는 축 자체가
사라지고 **central 내부 owner-scope 질의**(K7 재사용)로 수렴하므로, 별도 inbound fan-out 인프라는 어느
시점에도 필요하지 않다. → **이 기각 자체가 불변식 30.**

---

## P3B.5 설정 + 플래그 (§19 / §P2.8 / §P3A.8 확장 — 전부 default false)

| 환경변수 | settings.py 필드명 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `ORTHUS_ASK_EVENT_ORCH_ENABLED` | `ask_event_orch_enabled` | false | MA.8a 이벤트 트리거 오케스트레이션 on/off (off=mail ingest byte-identical) |
| `ORTHUS_ASK_EVENT_ORCH_MAX_PER_CYCLE` | `ask_event_orch_max_per_cycle` | 5 | ingest/pull 사이클당 기동 수 상한(storm 가드) |
| `ORTHUS_ASK_DECOMPOSE_FULL_DAG_ENABLED` | `ask_decompose_full_dag_enabled` | false | MA.8b depth > 2 허용 (off=기존 flat 강등) |
| `ORTHUS_ASK_DECOMPOSE_MAX_TOTAL_LEAVES` | `ask_decompose_max_total_leaves` | 12 | depth×width 누적 leaf 상한(비용 폭발 차단) |

`ORTHUS_ASK_DECOMPOSE_MAX_DEPTH`(기존, default 2)는 그대로 — full DAG flag on일 때만 이 값이 > 2로
설정될 수 있고, off면 2 초과 설정도 무시(flat 강등).

---

## P3B.6 SSE / 표면 (§12 / §P2.9 유지 + 구분)

- **MA.8a 이벤트 트리거:** 동기 클라이언트 없음 → **SSE 미사용**. 결과는 `/agent-work` 표면 + `audit`.
- **MA.8b 전체 DAG:** `/ask` 동기 경로에서 depth만 늘 뿐 SSE 이벤트 계약은 Phase 2(§P2.9)와 동일.
  추가 wave leaf는 자연히 늦게 `sub_answer_ready` 발행, FE는 index 추적만으로 충분(별도 이벤트 불필요).

---

## P3B.7 감사

- 신규 span: `router.event_orchestration`(MA.8a — 트리거 소스·결과 sink·`correlation_id`),
  기존 `router.decompose`/`router.decompose.synthesize`는 depth 무관 재사용.
- 이벤트 트리거 결과·질문 원문/본문은 span에 남기지 않는다(query_runs redaction 규약 준수).

---

## P3B.8 리스크 (§17 / §P2 / §P3A 확장)

| # | 리스크 | 완화 |
| --- | --- | --- |
| R16 | **이벤트→오케스트레이션 loop/storm** | 출력=비-이벤트(E2), source idempotency 중복 차단, 사이클당 상한 |
| R17 | **이벤트 트리거가 액션 자동 승인처럼 보임** | action leaf는 P3 gate `draft_for_review` default(E3). auto-send는 기존 P7.5 게이트만 |
| R18 | **depth×width 비용/커넥션 폭발** | `MAX_TOTAL_LEAVES` 상한 + §16 부등식 재계산(MA.8b 게이트) |
| R19 | **context_from 체인 PII 누적** | hop마다 `redact_pii_text` + 누적 길이 상한 |
| R20 | **full DAG가 LangGraph로 보임(R1 재발)** | backward-only + depth cap + 결정론 coordinate 입증(§P3B.3.2), operator review 필수 |
| R21 | **다중 노드 fan-out이 owner 경계 위반** | 다중 Personal Node fan-out 기각(§P3B.4). caller personal ∪ company 평면만, 타 owner personal 노드 미접근 |
| R22 | **이벤트 trigger가 ingest 트랜잭션 지연/실패 전파** | 비동기 enqueue(안 B), ingest 무영향 |

---

## P3B.9 구현 슬라이스 (MA.8)

| 슬라이스 | 내용 | 통과 게이트 |
| --- | --- | --- |
| **MA.8a 구현** | 이벤트 트리거 오케스트레이션: `mail/ingest.py` post-commit 훅 → `enqueue_mail_event_orchestration`(복합/실행 신호 결정론 트리거) → `ask_event_jobs` 큐(migration 0079) → `EventOrchestrationWorker`(lifespan, `kg_outbox` 패턴) → `answer_or_decompose`(scope=company, stream_id=None) → `persist_event_orchestration_brief`(게이트 `draft_for_review`). flag `ORTHUS_ASK_EVENT_ORCH_ENABLED`/`..._MAX_PER_CYCLE`. **`tests/integration/test_event_orchestration.py` 17종 PASS.** | flag off mail ingest 바이트 동일(PASS), 트리거 신호 결정론(PASS), loop 불가(sink≠트리거, PASS), source idempotency(PASS), gate `draft_for_review`(PASS), storm cap(PASS), dead-letter(PASS), seed/meta PII redaction(PASS), audit span `router.event_orchestration` |
| **MA.8b 구현** | 전체 DAG: `ask_decompose_full_dag_enabled` flag + `effective_max_depth` 게이팅(off→cap 2) + `ask_decompose_max_total_leaves` 가드(초과→단일 answer 강등) + `resolve_context` `_MAX_CONTEXT_CHARS` 누적 절단. `_run_waves`/`topo_levels`/`coordinate` 무변경. **`tests/unit/test_ask_decompose_ma8b.py` 13종 PASS.** 신규 마이그레이션·테이블 없음. | flag off Phase 2 byte-identical(depth>2 무시·flat 강등 유지, PASS), depth 3–4 위상 실행(PASS), 누적 leaf 상한 강등(PASS), §16 DB 커넥션 부등식 재첨부(§P3B.3.1 — peak=MAX_CONCURRENCY 불변), context hop redaction(PASS), backward-only/cycle 불가 재확인(PASS), `orthus-operator-reviewer` R20 입증(**코드구조 PASS, 2026-06-26** — §P3B.3.2 (a)(b)(c)(d)+불변식32+커넥션 부등식 6항목 전부 PASS, file:line 증거 첨부; `coord.next_wave` 미사용 dead field·flat-degrade 경고 미검증 2건은 non-blocking 관찰), browser QA(**PASS, 2026-06-27, 390×844** — full_dag on company node `/ask` read→act→act depth-3 `아틀라스 정책이 뭐야? 그 내용으로 보고서 초안 만들어주고 그 보고서를 김부장한테 메일 초안으로 써줘` → `mode=decomposed` 3-wave: wave0 structured grounded, wave1 action-intake `context_injected=true` + 검토 가능 `/agent-work/{id}` 딥링크(auto-send 아님), wave2 `upstream_unavailable` 안전 skip; DECOMPOSED 렌더 정상·fallback 미노출·가로 스크롤 없음(scrollW=clientW=390)·JS 에러 0. **참고:** ask.py 선분기 회피 위해 선두 info-query 필요; FE는 POST `/ask` 단일 경로라 per-wave SSE 미소비(SSE는 agentic 전용 — decompose SSE는 unit/integration 테스트가 커버)) |

> **flag on 게이트:** MA.8a/8b 모두 각 수용 게이트 + `orthus-operator-reviewer` 통과 전 운영 적용 금지.
> 다중 Personal Node fan-out은 owner 경계 위반으로 **기각**(§P3B.4) — 구현하지 않는다.

---

## P3B.10 수용 게이트 (§20 / §P2.11 / §P3A.11 + 추가)

- **flag off byte-identical:** 모든 MA.8x flag off에서 mail ingest / decompose / `/ask` 출력 바이트 동일.
- **이벤트 결정론:** 트리거 조건·seed_question 합성에 LLM 판단 입력 경로 없음.
- **loop 불가:** 오케스트레이션 출력이 트리거 소스에 포함되지 않음(구조적 비순환) + idempotency 중복 차단.
- **액션 경계:** 이벤트 기동 action leaf도 P3 policy gate `draft_for_review` default, auto-send 미발생.
- **depth bounded:** `len(levels) > MAX_DEPTH` flat 강등, full-DAG flag off면 depth > 2 무시.
- **비용 가드:** `MAX_TOTAL_LEAVES` 초과 강등 + DB 커넥션 부등식 재계산 첨부.
- **context 체인 PII:** 각 hop `redact_pii_text` 통과 + 누적 길이 상한.
- **사람 검토:** `orthus-operator-reviewer`로 R16(loop)·R17(액션 자동승인)·R20(LangGraph)·R21(owner 경계)을
  코드 구조로 입증. read→act→act 3–5 케이스 브라우저 QA(390×844 포함, MA.8b).

---

## P3B.11 보존 불변식 (§21 / §P2.12 / §P3A.12 확장 — 위반 시 PR 거부)

29. **이벤트 트리거는 비동기 + 단발.** `/ask` 동기 경로 불변, 결과는 AgentWork/draft sink, 출력이
    새 트리거 이벤트를 만들지 않는다(loop 금지). 액션은 P3 gate `draft_for_review` default.
30. **다중 노드 fan-out 기각 — owner 평면 경계.** 한 `/ask`/오케스트레이션은 caller의 personal ∪
    company 평면만 읽으며, 다른 owner의 personal 노드는 어떤 sub-question도 건드리지 않는다(§11/불변식 5).
    caller 평면에 근거가 없으면 답하지 않고 gap. 조직 전체 집계는 company promote 경유만. personal node
    inbound fan-out 구현 금지.
31. **full DAG도 bounded 결정론 위상 실행.** depth 상향은 backward-only + `depth ≤ MAX_DEPTH` +
    결정론 `coordinate` + 누적 leaf 상한 유지. LangGraph/오픈 ReAct 아님(불변식 16 재확인).
32. **flag off byte-identical.** 모든 MA.8x flag off면 mail ingest / decompose / `/ask` 출력이 도입 전과 동일.

---

## 23. 관련 문서

| 문서 | 내용 |
| --- | --- |
| 내부 문서(비공개) | 최종 계약·불변식·PR 판단 기준 |
| `docs/p3-autonomous-agent-loop.md` | Agent Work lifecycle, policy gate |
| 내부 문서(비공개) | `agent_task` 위임 sink 계약 |
| `docs/inline-agentic-ask.md` | inline agentic `/ask` 엔진 계약 |
| 내부 문서(비공개) | `/ask`·`/wiki`·`/agent-work` cross-link |
| `docs/kg-model.md` | K4b graph 분기·owner-scope graph |
| 내부 문서(비공개) | P7.1 `build_reply_candidate` 계약 |
| `docs/operations.md` | 플래그/공개 smoke/측정(MA.5) |

---

## 설계 결정 이력

| 결정 | 선택 | 근거 |
| --- | --- | --- |
| `classify_subpart` 알고리즘 | `detect_assistant_command_action` 우선 → `_rule_based_route` → `classify` LLM | 기존 게이트 100% 재사용. action-intake를 먼저 잡아채야 policy gate 오염 없음 |
| `split_question` 출력 포맷 | `{"sub_questions": [str, ...]}` JSON — 파싱 실패/len<2 → [] | 최소 구조. struct 없이도 스키마 전환 비용 없음 |
| `should_decompose` 두 단계 | False-prefilter(명령 동사 0개 AND 접속·열거 0개 → 즉시 False) → LLM enum. 나머지는 전부 LLM 판단 | `_COMMAND_VERBS` 재사용. 명백히 단순한 질문만 LLM 없이 걸러내고 실질 복합 판단은 LLM에게 위임 |
| agentic vs decompose 우선순위 | decompose 우선 — leaf 안에서 agentic 가능 | Phase 2 오케스트레이터 방향 일치 |
| Phase 1 스트리밍 | SSE Phase 1 포함 (기존 채널 재사용) | 10–20초 빈 화면 방지 |
| action-intake 연결 | Option B (P3 위임) | 코드 단순. P3 내부 조건 변경 시 decompose 무영향 |
| fan-out 병렬 실행 | `ThreadPoolExecutor + executor.submit(copy_context().run, leaf_fn)` | `run_in_executor`는 ContextVar 자동 복사 안 함. `answer()`가 sync blocking이므로 async 전환 없이 threadpool 유지. `asyncio.Semaphore` 대신 `threading.Semaphore` 사용 |
| **Phase 2** `depends_on` 도출 | split LLM이 `{text, depends_on}` 함께 출력 (index-based, backward-only) | LLM 호출 추가 0회. forward/self/out-of-range index drop으로 acyclic-by-construction 보장 → 결정론 위상 정렬 가능 |
| **Phase 2** 루프 구조 | bounded wave loop (`topo_levels` + 결정론 `coordinate`) | LangGraph/오픈 ReAct 아님. depth ≤ `MAX_DEPTH`(2) + backward-only + LLM-free coordinate로 하드룰(R1) 준수 |
| **Phase 2** context 전달 | upstream grounded 본문 텍스트를 redaction 후 downstream에 주입 (`context_from`) | read→act 실효 확보. scope 경계 부모 상속 + assert, PII redaction 필수(R15) |
| **Phase 2** 실패 전파 | upstream 실패/미그라운디드 → downstream skip(`upstream_unavailable`), 액션 미큐잉 | read 실패 시 blind 액션(잘못된 메일 등) 방지. 전체 fan-out 실패 아님 |
| **Phase 2** flag 분리 | `ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED` default false, off=Phase 1 byte-identical | Phase 1 무회귀 보장(불변식 8). 운영 적용은 MA.5 측정 게이트 |
| **MA.7c** GC 트리거 | CLI(`python -m orthus.router.cache gc`) + `make ask-cache-gc`/`node-ask-cache-gc`, 운영자/launchd 주기 실행 | KG outbox `trim` 선례 동일. 런타임(API) 무결합 → flag-off byte-identical(불변식 27) 위험 0. lifespan 워커/hot-store 결합 대비 단순 |
| **MA.7c** 삭제 기준 | TTL 만료(watermark 무관) + watermark-stale(노드 company partition별) | watermark 전진이 주 누적원(편집마다 partition 전 행 orphan) → stale 삭제가 실효. TTL은 2차 backstop. live 행 보존으로 정상 hit 무영향 |
| **MA.7c** flag 게이팅 | `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED` 무관 항상 실행 | 죽은 행 삭제는 캐시 off여도 무해. CLI 운영자 청소 용도와 자연스럽게 일치. lookup/store hook은 그대로 flag-gated |
| **Phase 3-B** 슬라이스 분해 | MA.8a 이벤트 트리거 → MA.8b 전체 DAG (다중 노드 fan-out은 기각) | 위험·결합도 상이. 이벤트 트리거가 기존 mail→AgentWork 인프라와 정렬돼 최저 위험, 전체 DAG는 wave 머신 재사용으로 경량, 다중 노드 fan-out은 owner 경계 위반으로 기각 |
| **MA.8a** 트리거 범위 (owner 2026-06-26) | 복합/실행 신호 inbound company mail만 (`mail_has_orchestration_signal` = should_decompose §4.1 prefilter 재사용) | 오케스트레이션이 실제 가치를 주는 메일에만 발화 → 단일 정보 메일에 decompose 비용 없음. P7.1 단일 답장 경로 불변 |
| **MA.8a** 비동기 실행 모델 (owner 2026-06-26) | 전용 `ask_event_jobs` 큐 + lifespan worker (`kg_outbox` 패턴) — 안 C | 안 B의 "기존 agent_work worker 소비"는 그런 runner가 없어 net-new. `kg_outbox` claim/lease/dead-letter 패턴을 재사용해 ingest 무영향(post-commit) + flag-off byte-identical 확보 |
| **MA.8a** sink (owner 2026-06-26) | 지식 브리프 1건(`event_orchestration` AgentWork, 결정론 `draft_for_review`) | 특정 메일 회신 생성은 `create_reply_draft` 미완성 + Phase 2 `context_from` 배선이 선결이라 MA.3 후속. 답장은 P7.1 유지, MA.8a는 지식 브리프만 추가(double-draft 회피) |
| **다중 노드 fan-out** | 기각 (owner 평면 경계 위반) | 개인은 다른 개인의 personal 노드에 접근 불가(불변식 5/30). caller personal ∪ company 평면만 읽으며, 합법적 형태(자기 personal + company)는 이미 owner-scope/federation이 처리, 조직 전체 집계는 company promote 경유 → 별도 inbound fan-out 인프라 불필요 |
| **MA.3** 구현 범위 (owner 2026-06-26) | 어댑터만 완성 (leaf 자동 라우팅 + `/ask` mail param 후속) | 문서가 명시한 MA.3 게이트(typed payload·request_more_data·audit)와 정확히 일치하는 최소·최저위험 범위. leaf 배선은 mail context 파라미터 + FE까지 필요한 별도 슬라이스 |
| **MA.3** 메일 해석 (owner 2026-06-26) | Option B — 호출자가 `MailIngestRequest`(reply_context) 전달, 어댑터는 저장소에서 복원하지 않음 | adapter-only 범위에선 live 호출자가 없어 markdown 파싱 복원(안 A)의 취약성만 떠안음. 이벤트 트리거/Phase 2 흐름은 payload를 이미 손에 쥐고 있어 자연스러움. 복원은 leaf/param이 실제 필요해지는 후속으로 이연 |
| **MA.3b** leaf 배선 방식 (owner 2026-06-26) | D안 — 복원 대신 P7.1 초안 재사용(`find_reply_draft_for_mail`), 없으면 `queue_agent_work` fallback | P7.1 persist가 canonical id에 멱등 → 답장 초안은 "메일에 한 번 붙는 속성". 재사용이 markdown 파싱(A)·raw JSON 저장(C)의 복원 취약성·마이그레이션을 전부 회피. A/C 복원은 P7.1-off 메일에 on-demand 생성이 필요하다는 증거(MA.5) 나올 때 leaf+C로 후속 |
| **MA.3b** id 주입 경로 (owner 2026-06-26) | `/ask context_mail_id` 파라미터 (`context_wiki_slug` 미러) | 자연어 텍스트 추출은 mail id가 안 실려 실사용 트리거가 거의 없음. FE 메일 상세에서 명시 전달이 견고. knowledge-token 미전달 + semantic cache 키 제외로 request-scoped 격리 |
