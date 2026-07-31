# WB — WorkBench 한국어 포트 결과 (2026-07-23)

사전선언 `analysis/wb-prereg.md` (실행 전 고정). 하네스/채점 = WorkBench 자체
(clone `external/.cache/workbench/`, 2026 Revisited 코드, evaluator 무수정).
포트/러너/채점 스크립트 = `external/wb_port_ko.py` / `wb_run_ko.py` / `wb_score.py`.
동결 목록 manifest = `external/wb_frozen_slice.json` (email 90 + calendar 110,
src sha256 be57642f…/0a2e3163…, ko sha256 77e49e9b…/26537bab…).

## 1. 셋업 (실측 그대로)

- 슬라이스: email 90 + calendar 110 = **200 task**, ground truth v2 전량. 지시문만
  한국어화(개체명 라틴 유지, 인용 본문 원문 유지 — prereg §4), outcome 무변경.
- 설정: 2026 Revisited 공개 런과 동일 — `structured_outputs=True`(native FC),
  `tool_selection=all`, `act_without_confirmation=True`, temperature 0.
- 모델: solar-pro(Upstage), EXAONE(Friendli dedicated, enable_thinking=false),
  claude-sonnet-4-6(Bedrock Converse, FULL prefix, workers 4).
  **gpt-5.3은 미실행** — preflight에서 `gpt-5.3` 404 → `gpt-5.3-chat-latest`로
  재시도 시 429 insufficient_quota. prereg대로 생략.
- 캐너리 5task/모델 통과 후 확대. 0점 조사 결과(solar/exaone calendar 0/2):
  국내 모델이 한국어 문자열("12월 13일")을 `query`로 넘겨 영어 이벤트명과
  텍스트 매칭 실패 — sonnet은 query 생략 + time bound 검색으로 성공. 하네스
  결함 아님(교차언어 도구사용 능력 그 자체)으로 판정하고 진행.
- 정확 호출 수(call log + preflight ping): **solar 680 · exaone 974 · bedrock 723**
  (bedrock = 캐너리 15 + ko 613 + en대조 92 + preflight 3; 동시성 ≤4 준수).

## 2. 헤드라인 — ko 200 task, 3-way

| 모델 | success | harmless fail | harmful side-effect |
|---|---|---|---|
| **claude-sonnet-4-6** | **190/200 (95.0%)** | 3 (1.5%) | **7 (3.5%)** |
| **exaone** | 86/200 (43.0%) | 41 (20.5%) | 73 (36.5%) |
| **solar** | 70/200 (35.0%) | 54 (27.0%) | 76 (38.0%) |

도메인 분리:

| 모델 | email (90) | calendar (110) | harmful email/cal |
|---|---|---|---|
| sonnet | 98.9% | 91.8% | 1.1% / 5.5% |
| exaone | 68.9% | 21.8% | 22.2% / 48.2% |
| solar | 32.2% | 37.3% | 35.6% / 40.0% |

- 국내 2종의 harmful 비율(36–38%)은 2024년 GPT-4(26%)보다도 높다 — 잘못된
  id로 delete/send를 그대로 실행하는 패턴. exaone은 email에선 준수(68.9%)하나
  calendar 다단 검색(빈 슬롯 찾기, 시간 경계)에서 무너진다(21.8%).
- sonnet 실패 10건 중 9건이 calendar 다단 추론 템플릿(조건부 catch-up 스케줄,
  요일+시각 경계 취소)이다.

## 3. 한국어 페널티 — en 대조 30 task (동일 task 쌍대)

같은 30개(email 14 + calendar 16, 템플릿 층화 30종)를 영어 원문으로 재실행:

| 모델 | ko 성공 | en 성공 | en만 성공 | ko만 성공 | 판정 |
|---|---|---|---|---|---|
| sonnet | 30/30 | 29/30 | 0 | 1 | 페널티 없음 |
| solar | 8/30 (26.7%) | 14/30 (46.7%) | 10 | 4 | 한국어 페널티 방향(유의 아님, McNemar 양측 p≈0.18) |
| exaone | 10/30 (33.3%) | 7/30 (23.3%) | 2 | 5 | 오히려 한국어 우위 방향 |

- **sonnet의 ko 격차는 사실상 0**: ko 전량 95.0% vs 저장소에 커밋된 공개
  claude-sonnet-4.6 영어 전량 96.0%(email 98.9/calendar 93.6, harmful 2.5%) —
  같은 config, 다른 API 경로(Bedrock vs Anthropic)임에도 −1.0%p.
- 국내 모델의 낮은 점수는 **한국어 탓이 아니다**: 영어 원문으로도 solar 46.7%,
  exaone 23.3%에 그친다. 병목은 언어가 아니라 자유 FC 루프에서의 도구
  선택·다단 검색·id 그라운딩이다.

## 4. Metric 06 / M7 대비

- WorkBench는 예상대로(prereg §5) **짧은 지평 상태-완료형**이지만, 우리 Metric 06과
  달리 typed-handler 스캐폴딩 없이 26개 도구 자유 FC 루프다. 결과는 Metric 06의
  "국내 ≈ frontier"가 **스캐폴딩이 있는 파이프라인에 한정된 결론**임을 보여준다:
  공개 사무 벤치마크의 자유 루프에서는 국내 35–43% vs frontier 95%로 벌어졌고,
  이는 M7(장기 자율 루프에서 frontier 우위)의 방향과 일치한다.
- prereg 예측 채점: P1 부분 적중(sonnet ≥85 ✓, exaone 43 범위 내 ✓, solar 35는
  하한 40 미달 ✗). P2 부분 오답(solar는 ko−en −20%p 방향의 페널티 — 다만 n=30
  비유의; exaone·sonnet은 예측대로 격차 없음/역방향). P3 적중(harmful 순위 =
  성능 역순).

## 5. 정직 캐비앗

- 번역 = 작성자 1인, 템플릿 59종 단위. 역번역 스팟체크 10건 통과(의미 보존)했지만
  독립 검수 없음. 국내 모델 ko−en 차이 일부는 번역 문체 탓일 수 있다.
- 단일 런, temperature 0이어도 API 비결정성 존재. 쌍대 30개는 검정력이 낮다
  (solar의 10:4도 비유의).
- 도구명/인자/샌드박스 데이터는 영어 그대로 — 이는 "한국어 지시 + 영어 도구"
  교차언어 측정이지 완전 현지화 벤치마크가 아니다. 국내 모델이 한국어 검색어를
  영어 DB에 던져 실패하는 사례가 실제로 관측됐다(캐너리 §1).
- 오염: WorkBench는 2024 공개 + 2026 개정 공개. 최종상태 채점이라 문자열 암기
  이득은 제한적이나 0 아님. 모델별 노출 확률 차이 미상.
- invalid-kwarg(스키마 밖 인자)는 WorkBench 고유 처리대로 해당 task error=실패
  처리(solar email 3건, exaone calendar 4건) — 하네스 수정 없음.
- gpt-5.3 미실행(quota) — frontier 표본은 sonnet 1종.
- 결과 원본(CSV/trace/meta)은 gitignored clone 내부(`data/results/*_ko*`,
  `wb_scores.json`, `wb_paired.json`, `wb_call_log.jsonl`)에만 있다. 커밋 안 함.

## 6. opus 추가 실측 (2026-07-23) — **opus-4-6 primary** / opus-4-5 partial

본 섹션은 §1–§5 본 런 이후의 추가 실측이다(사전선언 밖 exploratory 추가 —
prereg §3의 모델 목록에 없던 모델). 설정은 본 런과 완전 동일: 같은 동결
슬라이스(ko 200 + en 대조 30), 같은 하네스/evaluator 무수정, workers 4,
temperature 0, Bedrock Converse.

- **claude-opus-4-6 (primary):** runtime ID `us.anthropic.claude-opus-4-6-v1` —
  비정형 형식(date/`:0` 없음; `:0`을 붙이면 400). 캐너리 5/5 통과 후 전량.
- **claude-opus-4-5 (partial, bonus):** `us.anthropic.claude-opus-4-5-20251101-v1:0`
  (bare `anthropic.` ID는 on-demand 미지원 400 → `us.` inference profile 필수).
  캐너리 5/5 + **email_ko 90 완주** 후, 운영 결정(4.6 우선)으로 calendar_ko
  67/110 시점에 중단. 중단분은 채점 제외
  (`data/results/calendar_ko/partial_45_aborted/`).

### 6.1 헤드라인 — opus-4-6, ko 200 3-way

| 모델 | success | harmless fail | harmful side-effect |
|---|---|---|---|
| **claude-opus-4-6** | **194/200 (97.0%)** | 0 (0.0%) | **6 (3.0%)** |
| claude-sonnet-4-6 (§2) | 190/200 (95.0%) | 3 (1.5%) | 7 (3.5%) |
| exaone (§2) | 86/200 (43.0%) | 41 (20.5%) | 73 (36.5%) |
| solar (§2) | 70/200 (35.0%) | 54 (27.0%) | 76 (38.0%) |

도메인 분리: email 88/90 (97.8%, harmful 2) · calendar 106/110 (96.4%, harmful 4).

- opus-4-6은 sonnet 대비 +2.0%p (194 vs 190), harmful 3.5%→3.0%. frontier 간
  차이는 작고(단일 런, n=200), **국내 35–43% vs frontier 95–97% 격차**라는 §4
  결론은 그대로 강화된다.
- 실패 6건은 **전부 harmful side-effect**(harmless 0) — 모두 대량
  삭제/취소형 템플릿("지난 N일 X 이메일 전부 삭제", "수요일 N시 이후 회의
  전부 취소", "X와의 앞으로의 회의 전부 취소")에서 잘못된 대상 포함/누락.
  sonnet 실패 클러스터(calendar 다단 추론 9건)와 겹치는 계열이다.

### 6.2 한국어 페널티 — en 대조 30 task (동일 task 쌍대)

| 모델 | ko 성공 | en 성공 | en만 성공 | ko만 성공 | 판정 |
|---|---|---|---|---|---|
| opus-4-6 | 30/30 | 29/30 | 0 | 1 | 페널티 없음 (sonnet §3과 동일 패턴) |

en 대조 자체 성적: email_enctl 13/14 + calendar_enctl 16/16 = 29/30, harmful 0.
불일치 1건(ko만 성공)뿐이라 McNemar 무의미(p=1.0) — ko−en 격차 사실상 0.

### 6.3 opus-4-5 partial (bonus, email_ko 90만)

| 모델 | email_ko success | harmless | harmful |
|---|---|---|---|
| claude-opus-4-5 | 84/90 (93.3%) | 5 | 1 (1.1%) |
| claude-opus-4-6 | 88/90 (97.8%) | 0 | 2 (2.2%) |
| claude-sonnet-4-6 (§2) | 89/90 (98.9%) | 1 | 1 (1.1%) |

- 4.5 harmless 5건이 전부 "지난주 X의 'Subject' 이메일 전부 Y에게 전달"
  템플릿 — 4.6/sonnet은 통과하는 다건 전달 검색에서 일부 누락. calendar
  미완주라 ko 200 합산·en 대조·쌍대 비교는 **불가**(email 단면만 참고).

### 6.4 호출 수/비용 (정확 계수)

call log(`wb_call_log.jsonl`) + preflight/probe 수기 합산:

- **opus-4-6: 737** = ID probe 1 + 캐너리(preflight 1 + 15) + 본 런(preflight 1
  + email_ko 273 + calendar_ko 356 + email_enctl 41 + calendar_enctl 49 = 719).
  sonnet 723(§1)과 동급 — 도메인당 task당 ~3.1 call. 동시성 ≤4 준수, 스로틀
  429 재시도 0회 관측.
- **opus-4-5: ≈532** = ID probe 2(bare 400 포함) + 실패 preflight 1 + 캐너리 16
  + 본 런 preflight 1 + email_ko 291 + 중단된 calendar_ko ≈221(trace steps
  합산 — call log 미기록이라 근사). 합계 **≈1,269 Bedrock 호출**.

### 6.5 정직 캐비앗 (opus 한정 추가)

- §5의 전 항목(번역 1인, 단일 런, 교차언어 설계, 오염 가능성) 동일 적용.
- opus 추가는 **사전선언 밖**이다 — prereg 예측(P1–P3)에 opus는 없다.
- opus-4-5는 partial(위 §6.3)이며 어떤 합산 지표에도 포함하지 않는다.
- opus-4-6 결과 원본도 gitignored clone 내부에만 있다(커밋 안 함).
