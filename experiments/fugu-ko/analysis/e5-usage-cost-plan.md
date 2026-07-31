# E5 실험 계획서 — 토큰 usage 계측 + 원가 모델

> E-시리즈 우선순위 3/5 · 작성 2026-07-13 · 검토 반영 2026-07-14 · **상태: 실행 완료 (2026-07-14)**
> 실행 위치: `.worktrees/fugu-ko-e5` (브랜치 `feat/fugu-ko-e5-usage-cost`)
> **결과: `analysis/e5-results.md`** — 게이트 3종 PASS, usage 결측 0/316콜, 질문당 원가 **1.03배(동등)**
> → **국산화 동기 중 "비용" 축 기각**(Solar와 gpt-4o-mini는 공시 단가 동일 + 토큰 상쇄). 보고서 §6.7/§1.1 반영 완료.

## 1. 목표 / 문제 정의

보고서 §6.7이 정직하게 명기한 공백: *"하네스가 토큰 사용량을 계측하지 않아(응답 `usage` 미기록)
실측 토큰 기반 원가를 낼 수 없다 → 금액 비교는 후속 과제."* 국산화 동기 3개 중 하나가
**비용/환율**(§1.1)인데 금액 표가 비어 있다 — 배포 결정의 마지막 입력을 채운다.

- **산출물(가설 검정 아님 — 계측 실험):** 작업별 실측 입력/출력 토큰 × 벤더 공시 단가 →
  (a) 현행 gpt-4o-mini / (b) 오케스트레이션 배정 / 단일 국내 3종의 **1,000질문당 원가 표**.
- 이미 실측된 두 입력(호출 수 불변 · Solar 출력 3.6배)에 단가만 곱하면 되는 상태를 완성한다.

## 2. 기존 코드 접점 (실측 심볼)

| 접점 | 위치 | 현재 상태 |
|---|---|---|
| 워커 호출 | `pool.py::WorkerChat.complete()` — `_post_json()` 응답 `data`에서 `choices`만 읽고 **`data["usage"]` 폐기** | 계측 지점 ① |
| baseline 호출 | `pool.py::build_pool()`의 `"baseline"` → `orthus.models.registry.get_chat_model()` (프로덕션 `OpenAIChat` — usage 미노출) | 계측 지점 ② (§4.1에서 해소) |
| 하네스 기록 | `harness.py` 각 runner — `ms`만 기록, jsonl에 usage 필드 없음 | 기록 지점 |
| 재실행 대상 골든 | `golden/t3.json`(28) · `t5.json`(21) · `t2.json`(30) | 1회 재실행 (§4.2) |
| 단가 소스 | 벤더 공시: Upstage(solar-pro) · SKT adot(A.X-K1) · **Friendli dedicated(EXAONE — 토큰과금 아님, §7)** · OpenAI(gpt-4o-mini) | 수집 필요 |

## 3. 스코프

**In:** `WorkerChat` usage 계측, baseline의 usage-계측 가능화(실험 측), 골든 3태스크 × 4모델
1회 재실행, 작업별·구성별 원가 표, 보고서 §6.7 금액 절 채움.

**Out:** 프로덕션 `OpenAIChat`/`registry` 수정(실험 격리 — 프로덕션 원가 계측은 별도 제품
관심사), judge 콜 원가(평가 인프라 비용은 운영 원가 아님 — 각주로만), 지연 재측정(기존 수치
유지 — 같은 재실행에서 공짜로 재확인만), 임베딩 원가(국내화 스코프 밖 — 변인 통제 유지).

## 4. 방법

### 4.1 계측 설계

1. `WorkerChat.complete()`가 응답의 `usage`(`prompt_tokens`/`completion_tokens`)를 **누적
   리스트 `usage_events`에 append + `reset_usage()`** 제공 — 단일 `last_usage` 필드는 항목당
   2콜(json 재시도, compile 2-pass 등)이 되는 순간 조용히 과소계상하므로 채택하지 않는다.
   Protocol 시그니처(`complete(system, user, *, json_only) -> str`) 불변이라 orthus 주입 경로 무영향.
2. **baseline도 `WorkerSpec`으로 등록**해 동일 계측 경로로 통일: env
   (`ORTHUS_LLM_BASE_URL`/`ORTHUS_LLM_API_KEY`/모델명)에서 스펙을 만들어 `SPECS` 밖 특수 분기
   `build_pool(["baseline"])`를 `WorkerChat` 기반으로 교체. 프로덕션 `get_chat_model()` 의존 제거
   → 프로덕션 diff 0줄 유지. **행동 동등 검증됨(2026-07-14):** `OpenAIChat.complete()`와
   `WorkerChat.complete()`의 요청 body는 완전 동일(messages 구조·`temperature:0`·
   `json_only`→`response_format`·같은 `_post_json`) — `timeout=60`(OpenAIChat default)만 맞추고
   `min_interval=0`.
3. `harness.py` 각 runner가 항목 실행 전 `reset_usage()` → 실행 후 합산해 결과 dict에
   `in_tok`/`out_tok`/**`n_llm_calls`** 병기 → 기존 jsonl 스키마에 필드 추가(하위호환 — 기존
   분석 스크립트는 미사용 필드 무시). `n_llm_calls`는 보고서의 "호출 수 불변" 주장을 실측으로
   재확인하는 부수 증거를 공짜로 준다. **fast-path 문항은 `n_llm_calls=0`이 정상이다** —
   t5 `classify()`는 `_rule_based_route` 매치 시 LLM을 부르지 않으므로(route.py) usage가
   정의상 없고, 원가 0으로 기록한다(0원 문항이 평균에 들어가는 것이 정직한 문항당 원가이며,
   fast-path 비율 자체가 배정안의 원가 우위 데이터다).

### 4.2 실행 + 집계

- 골든 3태스크(t3/t5/t2) × 4모델(solar/ax/exaone/baseline) 1회 재실행 = 79문항 × 4 ≈ **316콜**.
  (t6/t7 제외 근거: **본 시나리오는 `/ask` 파이프라인 한정** — orchestrate 계층(intent/decompose)은
  fast-path 지배 + LLM 도달분 소수라 잎 3태스크로 원가 구조가 결정. 시나리오 정의에 명기.)
- 집계: 작업별 평균 in/out 토큰(fast-path 0원 문항 포함) → 단가 곱 → 문항당 원가 →
  구성별(단일 4종 + (b) 배정) 표.
- **질문당 원가 합성식(사전 고정):** 실제 `/ask` 질문 1개 = routing 1콜 + leaf 1콜의 파이프라인이므로
  `cost/question = c_routing + p·c_structured + (1−p)·c_wiki`. 믹스 `p`는 §4.3에서 환율과 함께
  사전 고정(1안: 골든 구성비 `p = 28/(28+30)`; 감도 확인용 50/50 병기). (a) 현행 baseline도
  **동일 합성식**으로 계산해야 "현행 대비 배율"이 성립한다. (b) 배정 원가 = 같은 합성식에
  작업별 배정 모델(`static_map.TASK_MODEL`)의 작업별 원가를 대입.
- 부수 검증: 재실행 정오가 기존 raw와 다른 문항 수 기록(모델 드리프트 신호 — 있으면 그 자체 발견).
  **적용 범위는 t3 gate 통과 여부 + t5 route 정오로 한정** — t2는 정오 개념이 없고(D3 judge
  쌍대비교), t3의 `executed`/rows는 DB 상태 변화로도 흔들리므로 모델 드리프트 신호로 세지 않는다.
- 기대 부수 발견: 동일 프롬프트라도 벤더별 tokenizer가 달라 **입력 토큰 수 자체가 모델별로
  다르게 청구**된다(한국어 토크나이저 효율 차이). 버그가 아니라 §6 C안(문자수 환산) 기각이
  옳았다는 실증 — 보고서에 기록.

### 4.3 단가 수집 (실행 전 선행)

벤더 공시 단가를 날짜와 URL과 함께 `analysis/e5-price-sources.md`에 고정(재현성).
환율 가정 1개(실험일 매매기준율)와 **질문 믹스 `p`(§4.2 합성식)** 를 같은 문서에 사전 고정.

## 5. 판정 기준 (사전 고정 — 계측 실험이므로 "완료 정의")

- 4모델 × 3태스크 **LLM 도달 콜 기준** usage 결측 0 — fast-path 문항(t5 룰 매치 등)은
  `n_llm_calls=0`으로 기록되면 정상(결측이 아니라 원가 0 실측). 결측 벤더 발견 시 §7 처리.
- 원가 표에 최소: 문항당 원가(작업별), §4.2 합성식 기반 질문당 원가 + 1,000질문/월 시나리오
  원가, 현행 대비 배율.
- 재실행 정오 드리프트 ≤ 2문항 — **t3 gate + t5 route 기준만**(§4.2). 초과 시 원가 표와 함께
  드리프트 절 별도 기록(수치 무효 아님).
- 보고서 §6.7 "금액으로 제시하지 않는 이유" 단락을 실측 표로 대체.

## 6. 대안 비교

| 옵션 | 장점 | 단점 | 판단 |
|---|---|---|---|
| A. 응답 `usage` 필드 계측 (본안) | 벤더 공식 집계, 구현 최소 | 벤더별 필드 누락 가능성 | 채택 |
| B. tokenizer 로컬 재계산 | 벤더 무의존 | 모델별 tokenizer 상이(EXAONE/A.X 비공개 가능) — 오차 원천 | 결측 벤더 한정 폴백 |
| C. 기존 raw에서 문자수→토큰 환산 추정 | 재실행 0콜 | 환산비 가정이 원가 표의 신뢰를 깎음 — "실측" 주장 불가 | 기각 (316콜은 싸다) |

## 7. 리스크 / 불확실성

- **EXAONE = Friendli dedicated endpoint는 토큰 단가가 아니라 GPU 시간 과금** — 원가 모델이
  구조적으로 다르다. 처리: EXAONE 행은 "토큰 환산 불가 — 시간당 endpoint 비용 ÷ 시간당 처리량
  (실측 지연 기반)"으로 별도 산식 + 명시적 각주. **이용률 가정도 표에 박는다:** serial ~36s/call
  실측 기반 처리량은 "이용률 100% 가정의 하한 원가"다 — 실트래픽(1,000질문/월)에서는 idle 시간도
  과금되므로 실효 원가가 몇 배 커질 수 있음을 같은 각주에 명기. 이 비대칭 자체가 정직한
  발견(관리형 국내 모델의 과금 모델 다양성)으로 보고서에 남긴다. Friendli serverless 토큰 단가가
  공시돼 있으면 참고 병기.
- **A.X 단가 공시 부재 가능성**(adot 게이트웨이가 B2B 견적형일 수 있음) → 공시 없으면 "단가 미공시"
  로 표기하고 토큰량만 기록(추정 단가 임의 대입 금지 — 정직성 원칙).
- **usage 필드 벤더별 스키마 차이/누락** → D0-스타일 스모크(모델당 1콜)로 필드 존재를 먼저 확인
  후 본실행(§8 G-스모크).
- 크레딧 사용이라 "실지출 0원" 각주는 유지 — 원가 표는 공시 단가 기준 **추정 운영 원가**로 명명.

## 8. 게이트

1. **G-단가:** 단가 소스 문서(`e5-price-sources.md`) 작성 완료 — 공시 확인 안 되는 벤더의
   처리 방침(§7) 확정 → 계측 구현 착수.
2. **G-스모크:** 4모델 × 1콜 usage 필드 존재 확인 → 본실행(316콜).
3. **G-보고서:** §5 완료 정의 충족 → `competition-report.md` §6.7 개정 + `experiment-log.md` 기록.

## 9. 비용 추정

구현(계측 + baseline 스펙화) 2~3시간 + 재실행 316콜(크레딧, EXAONE 지연으로 벽시계 ~1h) +
단가 수집/집계 2시간. 총 반나절~1일.
