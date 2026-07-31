# BFCL v3 multi-turn 사전선언 (prereg)

> 작성: 2026-07-23, **어떤 모델 호출도 하기 전**에 등록.
> 외부 타당성 실험 — 내부 B2-C6(tool-call compliance) / B3(request_more_data 행동)의 외부 corroboration.

## 고정 사항

- **하네스**: gorilla `berkeley-function-call-leaderboard` (BFCL), 저장소 커밋
  **`6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`** (sparse clone,
  `external/.cache/bfcl/` — gitignored, 커밋 금지). 현재 패키지 버전 표기는
  v4이지만 multi-turn 카테고리(`BFCL_v4_multi_turn_*`)는 v3에서 도입된 동일
  아이템셋이다(카테고리당 200개).
- **채점**: BFCL 내장 `multi_turn_checker` — 턴별 시뮬레이터 상태 비교
  (`state_checker`) + ground-truth 호출 subset 매칭(`response_checker`).
  판정자(LLM judge) 없음, 전부 프로그램 채점.
- **primary 카테고리**: `multi_turn_base` (200) + `multi_turn_miss_param` (200).
  `miss_func`/`long_context`는 optional(시간/쿼터 남으면).
- **temperature**: BFCL 기본 0.001. 단일 런(반복 없음 — non-claims 참조).

## 대상 모델

| registry | 실제 모델 | 경로 | 역할 |
|---|---|---|---|
| `solar-fc` | solar-pro (Upstage) | OpenAI-compat FC (`api.upstage.ai/v1`) | primary |
| `exaone-fc` | EXAONE (Friendli dedicated) | OpenAI-compat FC | primary |
| `sonnet-bedrock-fc` | `bedrock:us.anthropic.claude-sonnet-4-6` | Bedrock (bearer key, Anthropic SDK 또는 Converse) | frontier 기준선 |
| `ax-fc` | A.X-K1 (SKT) | OpenAI-compat FC | **20문항 probe 전용** — 내부 B2에서 tool-call 0% floor 문서화됨. 기록만 하고 순위 매기지 않음 |

gpt-5.3은 쿼터 preflight 통과 시에만 optional (기본 미포함).

## 등록 예측 (실행 전 고정)

내부 결과 (B2-C6 strict tool-call compliance): **Sonnet 95 / solar 90 / exaone 82.5 / ax 0**.
내부 B3/M7: request_more_data(부족 정보에서 되묻기) 행동은 frontier가 국내 모델보다 앞선다.

1. **P1 (base, compliance 순서)**: `multi_turn_base` 정확도 순위가
   **Sonnet ≥ solar > exaone >> ax(≈0)** 로 나온다. 판정 기준은 절대 수치가
   아니라 **부호(순위) 일치**다.
2. **P2 (miss_param, asking 행동)**: `multi_turn_miss_param`에서 frontier(Sonnet)와
   국내 모델의 격차가 base 대비 **더 벌어진다**
   (Δ(Sonnet−solar)_miss_param > Δ(Sonnet−solar)_base). 근거: B3/M7에서 국내
   모델은 부족한 인자를 되묻는 대신 환각 인자로 호출하는 경향.
3. **P3 (ax probe)**: ax는 하네스 strict 파싱에서 0 또는 그 근방. 이는 모델
   무능이 아니라 포맷 비준수(내부 B2 strict-vs-lenient 소견과 동형)일 수
   있으므로, 하네스 점수와 별개로 raw 로그에서 "tool call 시도는 있었는가"를
   구분 보고한다.

## 판정 규약

- **성공 기준 = 부호 일치(sign agreement)**: 외부(BFCL) 순위가 내부(B2-C6/B3)
  순위와 일치하는가. 절대 수치 비교는 주장하지 않는다.
- miss_param은 가능하면 per-turn 로그에서 **asking vs hallucinating** split을
  추출해 보조 보고한다(BFCL 채점과 별개 축).
- 0점 캐너리: 카테고리×모델당 5문항 캐너리를 먼저 돌리고, 0점이 나오면
  하네스 비호환 vs 모델 실패를 구분 조사한 뒤에만 스케일한다. 하네스가 모델
  native 포맷을 파싱 못해 0이 되는 경우 strict 점수와 (저렴하면)
  format-tolerant rescore를 **둘 다** 보고한다.

## Non-claims (주장하지 않는 것)

- 단일 런이다 — 분산/유의성 주장 없음.
- 시뮬레이터 8종 API는 우리 도메인(위키/메일/보드)이 아니다 — 도메인 전이
  주장 없음.
- 공개 벤치마크라 **오염 가능성 있음** — trajectory(다중 턴 상태) 채점이
  암기 효과를 상당히 희석하지만 제거하지는 않는다. 기각 근거로 쓰지 않되
  명시한다.
- Friendli dedicated EXAONE 엔드포인트/AX 게이트웨이는 공식 리더보드 제출
  구성과 다를 수 있다 — 리더보드 수치와의 비교 주장 없음.

## 운영 제약

- Bedrock 동시성 ≤4, 429 백오프(다른 에이전트가 같은 키로 WorkBench 병행 중).
- `.env` 수정 금지, 키는 in-process 로드(파일로 안 씀), 클론은 gitignored 유지,
  커밋 없음.
