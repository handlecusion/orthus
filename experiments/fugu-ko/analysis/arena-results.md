# Metric 05 — Assistant Arena 결과 (2026-07-22)

> prereg: `analysis/arena-prereg.md` (측정 전 고정 + 개정 3건 — §2 개정 1·§8 개정 2는 판정
> 시작 전, §9 개정 3은 하네스 파서 결함 수정으로 규칙 불변).
> 원자료: `raw/arena_t2_*.jsonl`, `raw/arena_email_*.jsonl`, `raw/arena_judge.jsonl`
> (run 1 원본은 `raw/arena_judge_run1_fencebug.jsonl` 보존). 커밋하지 않음.
> 실행: `arena_gen.py` → `arena_judge.py`. `ruff` clean.

## 0. 한 줄

**대체 판정자 패널 기준, 국내 조립(EXAONE)은 sonnet-4-6에 생성 품질 쌍대 배틀에서 열세다** —
t2 wiki QA는 60%가 판정 불가(tie)지만 갈린 12문항이 0승 12패(p≈5e-4), email은 1승 22패
(p≈6e-6). 판정자 사람-일치 검증(B4-X1)은 gpt-4o에만 있으므로 이 결론은 "사람-일치 검증이
없는 대체 판정자 기준 열세"까지다. 프론티어 표본은 sonnet 하나뿐이다(gpt-5.3 quota로 취소).

## 1. 무엇을 쟀나

정답이 하나가 아닌 **생성 작업**(t2 wiki QA 30 + email draft 30)에서 국내 조립(§15 슬롯:
`wiki_qa→exaone`, `email_draft→exaone` — `orthus/models/orchestration.py::ASSIGNMENTS` 확인)
vs 프론티어의 쌍대 배틀. 생성은 전부 **프로덕션 코드 경로**:

- t2: `orthus.wiki.qa.ask(scope="company", learn=False, record_gaps=False, chat_model=주입)`,
  grounding = 로컬 `orthus_r2`(25,412 wiki_pages, Solar 임베딩). DSN/임베딩 슬롯은 프로세스
  내 env로만 설정(`.env` 무수정), audit no-op(순수 read). 항목은 `e2e/tier_a.jsonl`의
  frozen t2 30문항.
- email: `agentwork.service._generate_command_email(to, inst, ctx)` — b2_run의
  `get_chat_model_for` 캡처 패치로 모델 주입, 반환 (subject, body, llm_drafted)를 그대로 배틀.
  JSON 실패 시 결정론 템플릿 폴백도 "프로덕션 출력"으로 그대로 배틀(발생: exaone 2/30 —
  e13·e27; solar/sonnet 0). 항목은 `t12_generation.py::EMAIL_ITEMS` 30문항.
- t8(synthesize)은 frozen sub-answer가 없어 격리 실행 불가 → **제외**(n=8 qualitative only).

## 2. 인프라 제약 + 판정자 교체 (결과 해석 전 필독)

1. **gpt-5.3 대전 취소** — 이 환경의 유일한 OpenAI 키가 `insufficient_quota`(429, 재시도
   무효; `gpt-4o`/`gpt-4o-mini`/`gpt-5.3-chat-latest` 전부 동일 계정 quota). 프론티어 대전은
   **claude-sonnet-4-6(Bedrock, 전체 모델 id `anthropic.claude-sonnet-4-6`) 단독.**
2. **판정자 gpt-4o 불가 → 대체 2인 패널**(prereg §8, 판정 시작 전 개정). 프로토콜은
   `judge/pairwise.py`/`t2_holdout_judge.py` verbatim(익명 A/B, 양방향 스왑, 방향 일치 시만
   판정자 verdict, t2는 [근거 — 위키 원문] 제시 grounded 판정) + 판정 모델만 교체:
   - `claude-haiku-4-5` — 프론티어(sonnet) 계열 → self-preference 방향은 국내에 불리(보수적).
   - 판정 쌍에 없는 국내 모델(exaone 배틀=solar 판정, solar 배틀=exaone 판정) — 편향 방향이
     반대라 상쇄 구조. ax는 미사용(Layer-1 점유 규칙).
   - 패널 collapse: 두 판정자 verdict가 **같은 승자**일 때만 win/loss, 그 외 전부 tie.
   - ⚠️ B4-X1(judge–human κ 0.457 > human 천장 0.404, 비율 CI 하한 0.92, PASS)은 **gpt-4o
     판정자 검증**이다. 이 패널에 승계되지 않는다 — 아래 모든 승부 서술은 "사람-일치 검증이
     없는 대체 판정자 기준".
3. **원 판정자(gpt-4o) 복귀 시도 — 차단(prereg §10)**: 판정 완료 후 "quota 복구" 통보를 받아
   원 프로토콜(B4-X1 검증 gpt-4o) 재판정을 시도했으나, **직접 카나리아 재검증에서 `gpt-4o`·
   `gpt-5.3-chat-latest` 모두 여전히 429 `insufficient_quota`**(같은 키, 본 세션 실측 —
   `arena_judge.py --judge gpt-4o`도 preflight에서 동일 429로 중단). 검증 판정자 표는 만들 수
   없었고 만들지 않았다. quota가 실제 복구되면 `arena_judge.py --judge gpt-4o` 한 명령으로
   저장된 생성물 위에 240표를 재판정해(`raw/arena_judge_gpt4o.jsonl`) validated-judge 표를
   1차, 아래 대체 패널을 2차 확증으로 병기할 수 있다.
4. **판정 run 1 폐기(파서 결함, prereg §9)** — haiku가 JSON을 ```json 코드펜스로 감싸(b2에
   문서화된 haiku 특성, 라이브 재현 확인) 240표 전부 `json.loads` 실패 → 조용히 tie. run 1의
   haiku 표에는 유효 판정 0건. 펜스 관용 파서 + 파싱실패 카운터를 넣고 **전량 재판정**(run 2).
   run 2 파싱실패 = 3개 판정자 모두 **0**. run 1 원자료는 보존.

## 3. 생성 단계 요약 (에러 0)

| task | model | n | 특이 | 답변 길이(평균) | p50 지연 |
|---|---|---|---|---|---|
| t2 | exaone (조립) | 30 | gap telemetry 13/30 | 207자 | 2,290ms |
| t2 | solar | 30 | gap telemetry 19/30 | 337자 | 2,098ms |
| t2 | claude-sonnet-4-6 | 30 | gap telemetry 22/30 | 303자 | 6,421ms |
| email | exaone (조립) | 30 | 템플릿 폴백 2 (e13,e27) | 307자 | 4,096ms |
| email | solar | 30 | 폴백 0 | 336자 | 1,568ms |
| email | claude-sonnet-4-6 | 30 | 폴백 0 | 451자 | 9,698ms |

- t2 사전점검(모델별 1문항, answerable): 3모델 모두 sources 5개 + 실질 답변. gap 값은
  `_looks_insufficient`가 답변의 유보 표현에 반응하는 phrasing 함수임이 확인돼 telemetry로만
  기록(prereg §2 개정 1). 품질 지표 아님.
- t2 retrieval sanity: exaone vs sonnet **source slug 집합 mismatch 0/30** — 두 arm의 근거가
  동일하므로 grounded 판정 근거(조립 arm excerpt 상위 3, 각 400자)는 중립.

## 4. 배틀 결과 (국내 관점 W/T/L, 2인 패널 만장일치 collapse)

### 주 배틀 — 조립 exaone vs claude-sonnet-4-6 (판정자: haiku + solar)

| task | n | W | T | L | 승률(decided) | tie율 | 정확 이항 p(양측) |
|---|---|---|---|---|---|---|---|
| t2 wiki QA | 30 | 0 | 18 | 12 | 0.0% (0/12) | 60% | ≈0.0005 |
| email draft | 30 | 1 | 7 | 22 | 4.3% (1/23) | 23% | ≈6e-6 |

판정자별 원 verdict(collapse 전, 60표=2task×30):
haiku W2 T14 L44 · solar W6 T16 L38 — **국내 판정자(solar)도 같은 방향**이라 패널 상쇄
구조에서 계열 편향으로 설명되지 않는다.

### 보조 — solar vs claude-sonnet-4-6 (report-only, 승부 주장 없음)

| task | n | W | T | L | 승률(decided) | tie율 | p |
|---|---|---|---|---|---|---|---|
| t2 wiki QA | 30 | 0 | 27 | 3 | 0.0% (0/3) | 90% | 0.25 |
| email draft | 30 | 4 | 13 | 13 | 23.5% (4/17) | 43% | 0.049 |

판정자별 원 verdict: haiku W7 T12 L41 · exaone W6 T35 L19.

Elo는 계산하지 않음(prereg §4 — deliverable은 승률; tie 다수라 Elo가 정보를 더하지 않음).

## 5. prereg §5 문구 적용 (사전 고정 기준 그대로)

- **t2 (주 배틀)**: tie율 60% ≥ 50% → 1차 서술은 "판정자가 가르지 못한 비율"이다: **30문항 중
  18문항은 패널이 승부를 가리지 못했다.** 갈린 12문항은 decided ≥ 10, p≈0.0005 < 0.05 →
  prereg §5에 따라 승부 주장 대상이며, §8.3 강등 적용: **"대체 판정자 기준 열세"**.
- **email (주 배틀)**: decided 23 ≥ 10, p≈6e-6 < 0.05 → **"대체 판정자 기준 열세"**. 같은
  grounding 없이 요청만 보고 쓰는 작업이라 tie율이 낮고(23%) 차이가 가장 뚜렷했다.
- **solar (보조)**: 승부 주장 없음(prereg §3). 기술적 서술만: t2는 tie 90%로 거의 갈리지
  않았고, email은 4승 13패(p=0.049, 경계).
- 이 결과로 §15 슬롯 배정 변경을 주장하지 않는다(측정 보고서일 뿐, prereg §6).

## 6. 정확 호출 수

| 단계 | 모델 | .complete() 호출 | 비고 |
|---|---|---|---|
| 생성 | exaone | 62 | 연결 preflight 1 + t2 preflight 1 + t2 30 + email 30 |
| 생성 | solar | 62 | 동일 구성 |
| 생성 | claude-sonnet-4-6 | **62 (Bedrock)** | 동일 구성, 동시성 4 |
| 생성 | gpt-5.3 | 0 성공 (실패 시도 2) | preflight 429 insufficient_quota ×2회 실행 |
| 판정 run 1 (폐기) | claude-haiku-4-5 | **241 (Bedrock)** | 240표 + preflight 1 — 전량 파싱실패 |
| 판정 run 1 (폐기) | solar / exaone | 121 / 121 | 각 120표 + preflight 1 |
| 판정 run 2 (유효) | claude-haiku-4-5 | **241 (Bedrock)** | 파싱실패 0 |
| 판정 run 2 (유효) | solar / exaone | 121 / 121 | 파싱실패 0 (exaone은 최종 0) |
| 진단 | openai 직접 호출 | 3 (전부 429) | gpt-5.3/gpt-4o/gpt-4o-mini quota 확인용 |
| 진단(§10 재시도) | openai 직접 호출 | 3 (전부 429) | 카나리아 gpt-4o·gpt-5.3 ×2 + `--judge gpt-4o` preflight ×1 |

- **Bedrock 총 invocation = 62 + 241 + 241 = 544** (어댑터 `.complete()` 프록시/카운터 기준;
  어댑터 내부 transient 429 재시도 re-POST는 별도 카운트하지 않음 — b2_run과 동일 규약).
- 국내 총: exaone 62+121+121=304, solar 62+121+121=304. OpenAI 성공 호출 0.

## 7. 정직한 caveat

- **판정자는 LLM이고 사람-일치 검증(B4-X1)은 gpt-4o에만 있다.** 본 패널(프론티어 계열 1 +
  비참여 국내 1, 만장일치만 승패)은 편향을 상쇄·보수화하는 구조이지 검증의 대체가 아니다.
  다만 두 판정자(계열 편향 방향이 서로 반대)가 같은 방향을 가리킨 점, collapse가 만장일치
  요구로 보수적인 점은 방향의 신뢰를 높인다.
- 프론티어 표본이 sonnet-4-6 하나(gpt-5.3 quota) → "프론티어 일반" 주장 불가.
- 단일턴, 사내 도메인 한국어 한정. 멀티턴/대화 품질 아님. t8 합성 제외.
- max_tokens 비대칭(국내 1024 vs sonnet 4096). judge 프롬프트가 장황함을 명시 감점하지만
  완전 통제가 아니고, sonnet email 본문이 평균 451자로 가장 길다 — email 열세 일부가 분량
  효과일 가능성을 배제 못 한다. 지연은 반대 방향이다(sonnet p50 6.4–9.7s vs 국내 1.6–4.1s).
  균등화 비용은 작다(exaone 재생성 60콜 + endpoint별 max_tokens override 한 줄)지만 "기존
  하네스 배선 그대로"라는 생성 규약을 깨므로 재생성하지 않았다(prereg §10.3).
- t2 판정 근거는 검색 chunk excerpt(각 400자 절단)이지 페이지 전문이 아니다 — 근거 밖 사실로
  잘잘못을 가릴 수 없는 판정 구조이며, excerpt 절단만큼 판정력이 약해진다.
- exaone email 2문항(e13·e27)은 JSON 실패로 결정론 템플릿이 배틀에 나갔다 — 프로덕션 출력
  그대로라는 원칙(prereg §2)에 따른 것이지만, 그 2패는 "모델 산문 품질"이 아니라 "형식 실패"의
  대가다.
- 검증 판정자(gpt-4o) 재판정은 시도했으나 quota로 불가(§2-3) — 본 보고서의 판정은 전부
  대체 패널 기준이며, validated-judge 표는 존재하지 않는다. "복구됐다"는 외부 통보는 본
  세션의 직접 카나리아로 반증됐다(429 insufficient_quota 지속).
- 판정 run 1 폐기·재실행(§2-4)은 결과값을 본 뒤의 수정이지만, 바꾼 것은 파서뿐이고 run 1의
  버려진 표는 전부 파싱 실패였다(유효 표 0건, 뒤집힌 표 0건). 원자료 보존.
- solar 결과는 report-only. ax는 어디에도 사용하지 않음.

---

## §최종 — 검증 판정자(gpt-4o) 재판정 완료 (2026-07-22 22:02, amendment 5)

쿼터 창이 열린 순간 코디네이터가 준비된 `arena_judge.py --judge gpt-4o`를 직접 실행 — preflight 통과,
120 판정행(exaone/solar × t2/email, 위치 스왑 양방향 collapse), 파싱 실패 0. **이것이 1차 판정이다**
(B4-X1 사람-일치 검증 이전 성립). 대체 패널은 2차 보강으로 강등.

| dom | task | W/T/L | decided 승률 | p (exact binom) | 판정 |
|---|---|---|---|---|---|
| **exaone (조립 1차)** | t2 | 2/17/11 | 15% | **0.022** | **유의 열세** |
| **exaone (조립 1차)** | email | 3/9/18 | 14% | **0.0015** | **유의 열세** |
| solar | t2 | 3/19/8 | 27% | 0.227 | 동률 (n.s.) |
| solar | email | 6/11/13 | 32% | 0.167 | 동률 (n.s.) |

- **3개 판정자(gpt-4o 검증·haiku·국내 solar)가 전부 같은 방향** — exaone의 생성 열세는 판정자
  아티팩트가 아니다. 대체 패널의 유의성이 검증 판정자에서도 유지됐다(exaone 한정).
- **실용 인사이트(슬롯 재배정 근거):** 생성 작업에서 solar는 Sonnet과 통계 동률(높은 tie율 63~37%),
  exaone은 유의 열세. §15 배정표는 wiki_qa/email_draft→exaone인데, **생성 품질 기준으로는 solar가
  옳은 배정**이었다 — exact-채점(t10 위임 등)에서 exaone이 이겼던 것과 반대 방향. 작업 성격
  (판별 vs 생성)에 따라 국내 1차가 갈린다는 새로운 슬롯 설계 신호.
- max_tokens 비대칭(1024 vs 4096) caveat 유지 — email 결과 일부는 길이 효과일 수 있음.
- gpt-5.3 대전은 생성 시점 쿼터 사망으로 여전히 미실행(정직 보고 유지).
