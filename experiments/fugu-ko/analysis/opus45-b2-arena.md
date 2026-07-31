# Opus 4.6(1차)/4.5(2차) — B2 계약 이행 + Assistant Arena 편입 (2026-07-23)

> 지시: Opus 4.5를 B2/Arena에 추가 → 실행 중 코디네이터 개정으로 **4.6이 1차, 4.5는 완료분 보너스 행**.
> 데이터: `analysis/raw/b2_summary_8model.json` · `b2_rows_8model.jsonl` ·
> `raw/b2_claude-opus-4-{5,6}.jsonl` · `raw/arena_{t2,email}_claude-opus-4-{5,6}.jsonl` ·
> `raw/arena_judge_opus4{5,6}.jsonl`. 기존 6/7모델 산출물은 덮지 않고 별도 파일로 남김. 커밋 없음.

## 0. 모델 ID (Bedrock Converse)

| slug | model_id | 비고 |
|---|---|---|
| claude-opus-4-5 | `anthropic.claude-opus-4-5-20251101-v1:0` | 표준형 — `_normalize_model_id`가 `us.` prefix 부여 |
| claude-opus-4-6 | `us.anthropic.claude-opus-4-6-v1` | **비정형**: `-v1` 접미사, date/`:0` 없음(`:0` 붙이면 400), `us.` 포함이라 prefix 통과. 3문항 카나리아 후 전량 실행 |

## 1. B2 계약 이행 — 280문항 전량, 에러 0 (양쪽)

| model | strict | lenient | recovery | fence |
|---|---|---|---|---|
| claude-sonnet-4-6 (기존) | 94.6% | 94.6% | +0.0p | 0% |
| **claude-opus-4-5** | **93.9%** | **95.4%** | +1.4p | 1.4% |
| solar (기존) | 94.3% | 94.3% | +0.0p | 0% |
| gpt-4o-mini (기존) | 93.6% | 93.6% | +0.0p | 0% |
| **claude-opus-4-6 (1차)** | **93.2%** | **93.9%** | +0.7p | 0.0% |

두 Opus 모두 Sonnet 4.6(94.6%)·solar(94.3%)와 **같은 상위 클러스터**다. 4.5↔4.6의 strict 차이는
280문항 중 2문항(0.7%p) — 순위 주장 불가 수준의 노이즈로 본다. lenient에서는 4.5(95.4%)가 전 모델 최고.

### per-type (strict/lenient)

| type | opus-4-6 | opus-4-5 | sonnet-4-6 | solar | exaone | gpt-4o-mini |
|---|---|---|---|---|---|---|
| C1 (펜스) | 100/100 | 100/100 | 100/100 | 97.5/97.5 | 100/100 | 97.5/97.5 |
| C2 | 100/100 | 100/100 | 100/100 | 95.0/95.0 | 100/100 | 100/100 |
| C3 | **92.5/92.5** | 100/100 | 100/100 | 100/100 | 100/100 | 97.5/97.5 |
| C4 | 100/100 | **90.0/100** | 100/100 | 100/100 | 0.0/0.0† | 100/100 |
| C5 | 95.0/100 | 100/100 | 100/100 | 100/100 | 100/100 | 100/100 |
| C6 (tool-call) | 95.0/95.0 | 95.0/95.0 | 95.0/95.0 | 90.0/90.0 | 82.5/82.5 | 90.0/90.0 |
| C7 | 70.0/70.0 | 72.5/72.5 | 67.5/67.5 | 77.5/77.5 | 72.5/72.5 | 70.0/70.0 |

† exaone C4=0%는 기존 문서화된 러너 아티팩트(b2-results §4.1) — 재인용.

실패 내역(정직 보고):
- **opus-4-5 C4 90→100**: strict 실패 4건 전부 `email_draft_payload`에서 JSON을 ```펜스로 감싼 것
  (c4-11/16/35/40). 펜스 스트립(lenient) 한 줄로 전량 복구 — Haiku C1 현상의 미니어처.
- **opus-4-6 C3 92.5%**: `graph_bind` 3건(c3-29/30/31)이 `subjects: []` 빈 배열 — 바인딩 거부
  성향(형식은 유효 JSON, 내용 미충족). 4.5·Sonnet은 같은 문항에서 subjects를 채웠다.
- **opus-4-6 C5 2건**: `structured_compile`에서 "(no tables)" 산문 거부 — 골든이 카탈로그를 런타임
  조회 못 해 빈 카탈로그로 렌더되는 **기존 문서화된 러너 아티팩트**(b2-results §4.2, Haiku와 동류).
  순수 모델 실패로 보지 않는다.
- C7은 전 모델 67.5~77.5%로 눌리는 hard type — Opus도 예외 아님(11~12건).

### 주판정(분산비) — 변화 없음

두 Opus 모두 **145문항 Layer-2 정확도가 없어** variance-ratio 페어링에서 제외했다(Haiku와 동일
사유 — 분모 불일치 방지). `analysis/raw/b2_accuracy.json` 무수정. 따라서 주판정은 기존 5모델
그대로: ratio 13.875 (점추정), bootstrap CI [0.045, 77.0] → verdict `comparable`(주장 철회 유지).

## 2. Assistant Arena — 생성 단계 (각 62콜, 에러 0)

| task | model | 특이 | 답변/본문 길이(평균) | p50 지연 |
|---|---|---|---|---|
| t2 | claude-opus-4-6 | gap telemetry 23/30 | 383자 | 7,926ms |
| t2 | claude-opus-4-5 | gap telemetry 24/30 | 324자 | 6,302ms |
| t2 | claude-sonnet-4-6 (기존) | gap telemetry 22/30 | 303자 | 6,421ms |
| email | claude-opus-4-6 | 폴백 0 | 373자 | 8,937ms |
| email | claude-opus-4-5 | **템플릿 폴백 1 (e19)** | 306자 | 7,207ms |
| email | claude-sonnet-4-6 (기존) | 폴백 0 | 451자 | 9,698ms |

- t2 retrieval sanity: exaone vs opus-4-5 / opus-4-6 모두 **source slug mismatch 0/30** — grounded
  판정 근거 중립성 유지.
- opus-4-5 e19 폴백은 `_generate_command_email` 파싱 실패로 결정론 템플릿이 나간 것 — 그 산출물이
  그대로 opus arm으로 판정에 들어갔다(생성 계약 실패로 정직 계상).

## 3. 배틀 결과 (국내 관점 W/T/L, **대체 2인 패널** 만장일치 collapse)

판정자: **gpt-4o 불가** — 판정 직전 3회 + 4.6 판정 직전 1회 + preflight 1회, 전부 429
`insufficient_quota`(총 5콜, 상세 §5). Sonnet 보고서 §8과 동일한 대체 패널(haiku + 판정 쌍에 없는
국내 모델)로 판정하고 **"대체 판정자 기준"으로만 서술**한다. 파싱 실패: 4.5 판정에서 exaone 3표
(→tie), 그 외 0.

### 1차 — vs **claude-opus-4-6** (판정자: haiku + off-pair 국내)

| dom | task | n | W/T/L | 승률(decided) | tie율 | 정확 이항 p(양측) |
|---|---|---|---|---|---|---|
| exaone (주) | t2 | 30 | 1/18/11 | 8.3% | 60% | **0.006** |
| exaone (주) | email | 30 | 3/8/19 | 13.6% | 27% | **0.001** |
| solar (보조) | t2 | 30 | 0/24/6 | 0.0% | 80% | **0.031** |
| solar (보조) | email | 30 | 4/16/10 | 28.6% | 53% | 0.180 (n.s.) |

### 2차(보너스) — vs **claude-opus-4-5**

| dom | task | n | W/T/L | 승률(decided) | tie율 | p |
|---|---|---|---|---|---|---|
| exaone (주) | t2 | 30 | 0/16/14 | 0.0% | 53% | **1.2e-4** |
| exaone (주) | email | 30 | 5/10/15 | 25.0% | 33% | **0.041** |
| solar (보조) | t2 | 30 | 0/22/8 | 0.0% | 73% | **0.008** |
| solar (보조) | email | 30 | **11/14/5** | 68.8% | 47% | 0.210 (n.s.) |

### Sonnet 4.6 대비 (기존 결과 재인용)

| 비교 | exaone t2 | exaone email | solar t2 | solar email |
|---|---|---|---|---|
| vs Sonnet — **검증 gpt-4o**(1차 판정) | 2/17/11 p=0.022 | 3/9/18 p=0.0015 | 3/19/8 n.s. | 6/11/13 n.s. |
| vs Sonnet — 대체 패널 | 0/18/12 p≈5e-4 | 1/7/22 p≈6e-6 | 0/27/3 n.s. | 4/13/13 p=0.049 |
| vs Opus 4.6 — 대체 패널 | 1/18/11 p=0.006 | 3/8/19 p=0.001 | 0/24/6 p=0.031 | 4/16/10 n.s. |
| vs Opus 4.5 — 대체 패널 | 0/16/14 p≈1e-4 | 5/10/15 p=0.041 | 0/22/8 p=0.008 | **11/14/5** n.s. |

읽기 (like-for-like는 패널↔패널 행만):
- **exaone(조립 1차)의 생성 열세는 Opus에서도 재현** — t2/email 모두 유의(4.6: p=0.006/0.001).
  Sonnet 배틀과 같은 방향, 같은 자릿수.
- **solar는 email에서 Opus에 통계 동률** — 4.6 상대 4W10L(n.s.), 4.5 상대는 오히려 11W5L로 앞섰다
  (n.s., p=0.210). Sonnet 패널 배틀의 4W13L(p=0.049 경계 열세)보다 우호적. 단 보조 배틀은 prereg §3상
  승부 주장 없음 — 기술 서술만.
- solar t2는 Sonnet 대전(tie 90%)과 달리 Opus 상대로 유의 열세가 떴다(4.6 p=0.031, 4.5 p=0.008) —
  tie율이 73~80%로 여전히 높아 decided 표본은 6~8건에 불과하다. 약한 신호로 취급.
- 4.5 vs 4.6 간 역전(예: solar email 11W→4W)은 판정 표본·tie율 변동 범위 안 — 두 Opus의 우열 주장에
  쓰지 않는다.

## 4. 정직 caveats

1. **판정자 비교 가능성**: Opus 배틀은 전부 **대체 2인 패널**이다. Sonnet의 1차 판정(검증 gpt-4o,
   B4-X1 사람-일치 κ 검증)은 quota 사망으로 Opus에 재현 불가. 위 표의 gpt-4o 행과 패널 행은
   **판정자가 다르므로 직접 비교 금지** — Sonnet 사례에서 두 판정자가 같은 방향이었다는 것이
   유일한 다리다. quota 복구 시 `arena_judge.py --judge gpt-4o --frontier claude-opus-4-6
   --out-suffix _opus46`(4.5는 `_opus45`) 한 명령으로 저장 생성물 위에 재판정 가능하게 준비돼 있다.
2. **패널 구성 편향**: haiku는 Opus와 같은 계열(같은 벤더 — 어느 방향으로든 편향 가능), 국내 판정자는
   판정 쌍 제외로 뽑았지만 exaone/solar 상호 판정이라 완전 독립이 아니다. Sonnet 보고서와 동일 구조라
   **상대 비교(Sonnet행 vs Opus행)는 편향이 상쇄**되는 편.
3. **max_tokens 비대칭**(bedrock 4096 vs 국내 1024) caveat는 Sonnet 보고서 그대로 승계 — email 열세
   일부는 길이 효과일 수 있다.
4. **B2 두 Opus의 145-accuracy 부재** → 분산비 페어링 제외(§1). 8모델 표에서도 주판정 수치는 기존
   5모델과 동일하다.
5. **opus-4-6 C5 2건은 러너 아티팩트 혼입**(빈 카탈로그), C3 3건은 진짜 바인딩 거부 성향. strict
   93.2%를 인용할 때 이 5건의 성격을 병기할 것.
6. 4.5는 코디네이터 개정 전 완주한 **보너스 행**이다 — 1차 인용은 4.6.
7. Bedrock는 동시 사용자(BFCL sonnet + Layer2/glue opus)와 키를 공유 — 전 실행 worker ≤4 유지,
   429/스로틀 어댑터 내 재시도 외 별도 백오프 불요(관측된 스로틀 0).

## 5. 정확 호출 수

| 단계 | 모델 | 호출 | 비고 |
|---|---|---|---|
| B2 opus-4-5 | Bedrock | **284** | 스모크(preflight 1+2) + 본실행(preflight 1+280), 에러 0 |
| B2 opus-4-6 | Bedrock | **285** | 카나리아(preflight 1+3, workers 1) + 본실행(preflight 1+280), 에러 0 |
| Arena 생성 | opus-4-5 / opus-4-6 | **62 + 62** | 각: 연결 preflight 1 + t2 preflight 1 + t2 30 + email 30 |
| 판정 (4.5 배틀) | claude-haiku-4-5 | **241** | 240표 + preflight 1 (Bedrock) |
| 판정 (4.5 배틀) | solar / exaone | 121 / 121 | 파싱실패 exaone 3 → tie |
| 판정 (4.6 배틀) | claude-haiku-4-5 | **241** | 파싱실패 0 (Bedrock) |
| 판정 (4.6 배틀) | solar / exaone | 121 / 121 | 파싱실패 0 |
| 진단 | openai gpt-4o | 5 (전부 429) | 판정 전 quota 확인 1+3회 + 4.6 판정 전 1회 |

**Bedrock 총합 = 1,175** (284 + 285 + 124 + 482). 전 구간 동시성 ≤4 유지.

## 6. 코드 변경 (미커밋)

- `b2_run.py`: `_BEDROCK_MODEL_IDS`에 opus 4.5/4.6 두 줄 추가(+ 4.6 비정형 ID 주석).
- `arena_judge.py`: `--frontier`/`--out-suffix` 인자 추가 — 기존 Sonnet verdict 파일
  (`arena_judge.jsonl`/`arena_judge_gpt4o.jsonl`)을 덮지 않기 위한 최소 변경. 기본값은 종전 동작.
- `ruff check` PASS(양 파일). `ruff format --check`는 arena_judge.py의 **기존 코드**(내 변경 전부터)
  가 드리프트라 미적용 — stash 검증으로 선재성 확인, 무관 줄 재포맷은 하지 않았다.
- 기존 산출물 무손상: 6/7모델 summary·rows, Sonnet arena 파일 전부 그대로. `.env` 무수정. 커밋 없음.
