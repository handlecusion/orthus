# Inline agentic /ask — Solar function-calling 루프

> Status: fail-closed flag 뒤에 구현됨. `ORTHUS_AGENTIC_ASK_ENABLED=true` 전까지
> `/ask` 동작은 기존 라우터 래더와 동일하다.

## 무엇인가

`/ask`의 고정 라우터 래더(wiki | structured | graph 분기) 대신, **Solar의
네이티브 function calling**으로 도구를 다턴 오케스트레이션하는 인프로세스
루프다. 흐름:

```
질문 → OpenAIChat.run_tool_loop (orthus/models/adapters/openai_compat.py)
        ├─ 도구 광고: wiki_ask · structured · team_schedule
        │   (+ 게이트 통과 시: mail_search/compose · kg_relations ·
        │    entity_relations · inbox_summary · data_gaps ·
        │    team_members/board/projects · ask_user)
        ├─ Solar가 tool_calls 선택 → dispatch가 결정론 백엔드 실행
        └─ 최종 산문 답 → RoutedAnswer 봉투로 매핑 (orthus/router/agentic/loop.py)
```

- 엔진 슬롯은 `registry.get_agent_chat_model()` — 플래그 off거나 Solar 키가
  없으면 `None`을 돌려 레거시 래더로 폴백한다(fail-closed).
- 루프는 `max_turns` 캡 + **fail-open**: 어떤 예외도 요청 핸들러로 던지지 않고
  지금까지 누적된 텍스트를 반환한다. 정확성은 루프가 아니라 도구 백엔드가
  소유한다.

## 안전 계약 (설계 원칙 1·4 유지)

- **모델은 도구를 고를 뿐 실행하지 않는다.** `structured` 도구의 입력은 자연어
  `question` 하나 — SQL을 주입할 필드 자체가 없고, 실행은 sqlglot 검증
  게이트(SELECT-only + schema_ok + LIMIT + EXPLAIN)와 DB read-only 롤을 그대로
  통과해야 한다. 게이트 거부는 레거시와 동일한 422 봉투로 표면화된다
  (회귀: `tests/unit/test_agentic_ask.py`).
- KG 도구는 typed 템플릿 게이트로만 나간다(raw Cypher 입력 경로 없음), KG
  미가용 노드에서는 광고 자체가 빠진다.
- `mail_compose`는 발송이 아니라 **초안**이다 — 실제 발송은 사용자가 검토 후
  별도 승인 경로로만.
- recursion guard: 토큰 인증 caller(wiki_ask 경유)는 agentic에 진입하지 않는다
  (`allow_agentic=not is_token`).

## 설정

| env | 의미 |
|---|---|
| `ORTHUS_AGENTIC_ASK_ENABLED` | 루프 on/off (기본 off, fail-closed) |
| `ORTHUS_LLM_SOLAR_API_KEY` | 엔진 자격증명 (없으면 레거시 래더 폴백) |
| `ORTHUS_AGENTIC_ASK_MAX_CONCURRENCY` | 동시 루프 상한 (기본 3) |

## 주요 코드

- `orthus/models/adapters/openai_compat.py::OpenAIChat.run_tool_loop` — 루프 본체
- `orthus/router/agentic/loop.py` — 시스템 프롬프트·도구 광고 게이트·dispatch·
  RoutedAnswer 매핑, SSE 진행 프레임
- `orthus/router/agentic/tools.py` — 도구 스펙 + 결정론 백엔드 어댑터
- `orthus/models/registry.py::get_agent_chat_model` — 엔진 슬롯 (fail-closed)
