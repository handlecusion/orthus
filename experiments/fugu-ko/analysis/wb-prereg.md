# WB — WorkBench 한국어 포트 사전선언 (prereg)

작성: 2026-07-23, 실행 전 고정. 대상 = Metric 06(flow completion)의 외부 공개 벤치마크 검증.

## 1. 대상 벤치마크

- **WorkBench** (olly-styles/WorkBench, COLM 2024, MIT). 로컬 clone
  `external/.cache/workbench/` (gitignored), HEAD 기준 2026 "WorkBench Revisited" 코드.
- 채점 = WorkBench 자체 결정론 evaluator(**무수정**): 예측 action 리스트를 fresh 샌드박스에
  실행 → 최종 상태를 ground-truth 실행 상태와 행 단위 비교. 3-way 결과:
  success(correct) / harmless fail(incorrect, side effect 없음) / harmful
  side-effect(incorrect + 상태 변경).
- 채점 join 키는 **task 문자열** (`metrics.compute_metrics`가 `merge(on="task")`) —
  한국어 포트는 task 문자열을 한국어로 바꾼 **병렬 ground-truth CSV**를 만들면 되고
  evaluator 코드는 그대로다. `outcome`(ground-truth action 리스트)은 바이트 단위로 무변경.

## 2. 슬라이스 (동결)

- **email 90 + calendar 110 = 200 tasks** — 두 도메인의 v2 ground truth 전량
  (`data/processed/tasks_and_outcomes/{email,calendar}_tasks_and_outcomes.csv`).
  하위 샘플링 없음 → "frozen list" = 두 파일 전체. sha256은 결과 문서에 기록.
- 영어 대조군: 같은 200개 중 **30개**(email 14 / calendar 16, 템플릿 다양성 유지하며
  결정론 seed=20260723 층화 추출, 목록은 포트 파일에 `en_control=True`로 고정)를
  원문 영어 그대로 동일 설정으로 실행.

## 3. 실행 설정 (2026 Revisited 공개 런과 동일 맞춤)

공개 2026 런의 `_meta.json` 전수 확인 결과: `structured_outputs=True`,
`tool_selection=all`, `act_without_confirmation=True`. 우리도 동일:

- **structured_outputs=True** (native function calling — ReAct 텍스트 파싱 아님),
  `tool_selection=all`(26개 도구 + company_directory 전부 노출),
  `act_without_confirmation=True`, temperature 0(지원 모델), max_iterations 20 기본.
- 이 맞춤 덕에 sonnet-4.6의 우리 ko 결과를 저장소에 커밋된 공개 en 결과와 직접 대조 가능.

### 참가 모델

| slug | 경로 | 비고 |
|---|---|---|
| solar (solar-pro) | api.upstage.ai/v1, OpenAI-compat FC | `.env` `ORTHUS_LLM_SOLAR_*` |
| exaone | api.friendli.ai/dedicated/v1, OpenAI-compat FC | `chat_template_kwargs.enable_thinking=false` 필수(B2 규칙), model=엔드포인트ID |
| claude-sonnet-4-6 | Bedrock Converse, FULL prefix `anthropic.claude-sonnet-4-6` | `_call_llm_structured` 자리에 Converse 어댑터 주입, workers ≤4 |
| gpt-5.3 (옵션) | api.openai.com | quota preflight 통과 시에만; temperature/max_tokens 미전송 |

A.X는 불참(모델 오케스트레이션 분석 §11: RPS 3 하드캡 + tool-call 무시 전력 —
200 task × 다회 호출이 비현실적).

### 절차

1. 캐너리: 모델당 5 task(email 3 + calendar 2, 포트 파일 선두에서 고정) → **0점이
   하나라도 나오면 원인 규명 전 확대 금지**(이 프로젝트 silent-zero 함정 5회 전례).
2. 본런 ko 200 → 3. en 대조 30 → 4. 채점/보고(`analysis/wb-results.md`).
   Bedrock 총 호출 수를 정확히 집계해 보고(동시성 ≤4).

## 4. 한국어 포트 규칙

- 번역 대상 = **task 지시문만**. 도구 스키마·도구명·인자·샌드박스 데이터·채점 코드 무변경.
- 의미 보존 직접 번역(작성자 1인). `instruction_en`/`instruction_ko` 병기 컬럼 유지.
- **개체명은 라틴 표기 유지**: 사람 이름(nadia, sofia …), 이벤트/제목/본문 인용구('{subject}',
  '{body}'), 프로젝트명은 원문 그대로 — 샌드박스 검색 키가 영어 CSV라 음차하면
  풀 수 없는 문제가 되고, 한국 사무 환경에서도 영문 이름 혼용이 자연스럽다.
  날짜/시간 표현("December 13" → "12월 13일")과 문장 골격만 한국어화.
- 번역 시 해당 task의 `outcome`(정답 action)은 보지 않는다(지시문 자체에 담긴 정보 외
  ground truth 미참조). 완료 후 10개 역번역 스팟체크를 기록.
- 템플릿 단위 번역(email 26종 + calendar 33종) 후 슬롯 치환 — 같은 영어 템플릿은
  같은 한국어 골격을 갖는다(문체 변형 원문의 격식 차이는 한국어 존대 변형으로 보존).

## 5. 예측 (정직 등록)

WorkBench의 체제 판정: **짧은 지평 상태-완료형**(단일 지시, ground-truth 쓰기 액션
1–3개 + 검색 수 스텝)이다 — M7의 장기 자율 루프보다 Metric 06(flow completion)에 가깝다.
따라서:

- **P1 (주)**: 국내(solar/exaone)는 sonnet에 뒤지지만 붕괴하지는 않는다 —
  ko success 기준 sonnet ≥85%, solar/exaone 40–70% 범위로 예상. Metric 06에서
  국내≈frontier였지만 그것은 우리 typed-handler 파이프라인(짧고 스키마 강제) 기준이고,
  WorkBench는 20-스텝 자유 FC 루프 + 26개 도구 선택이라 M7 쪽 부하(도구 선택·다단 검색)가
  일부 섞여 있다. 순수 동률 예측은 하지 않는다.
- **P2**: ko−en 격차(같은 모델, 대조 30개)는 국내 모델이 frontier보다 크지 않다
  (국내 모델은 한국어 강점이 있어 오히려 격차가 작거나 0일 수 있다). frontier의
  ko 격차도 ≤10%p로 예상(지시문만 한국어, 도구는 영어).
- **P3**: harmful side-effect 비율은 성능 역순(sonnet 최저) — Revisited의
  "capability와 safety 동방향" 재현.

## 6. 주장하지 않을 것

- 번역 아티팩트 배제 — 번역은 작성자 1인, 역번역 스팟체크 10개뿐. ko−en 격차의
  일부는 번역 품질일 수 있다.
- 단일 런: run-to-run 분산 미측정(temperature 0이지만 API 비결정성 존재).
- "완전한 한국어 벤치마크" — 도구명/인자/샌드박스 데이터는 영어 그대로다. 이는
  cross-lingual 지시-따르기 측정이지 완전 현지화가 아니다.
- 오염 무결성: WorkBench는 2024 공개 + 2026 개정 공개라 사전학습 노출 가능.
  trajectory형(최종 상태 채점)이라 정답 문자열 암기의 이득은 제한적이지만 0은 아니다.
  국내 모델 vs frontier의 노출 확률 차이도 미상.
- 통계적 유의성 단정: 200 task 단일 런의 쌍대 비교는 보고하되, 경계 사례에서
  과잉 해석하지 않는다.
