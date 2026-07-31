# 잔여 태스크 재측정 실행 계획 (Remaining Slots Scale-up Plan)

작성: 2026-07-29 · 상태: **실행 계획서 — owner 최종 결정 반영 완료(2026-07-29).
확정 파라미터는 §2.0, 미해결 리스크는 §5.**

> 범위: 자유생성 **5종(wiki_qa / synthesize / email_draft / gap_suggest /
> claim_headline)**, **n=1,000**. distill·followup_rewrite는 별도 트랙(§5).

## 0. 배경

arena/e2e 벤치마크가 `orthus/models/orchestration.py::ASSIGNMENTS`의 13개 슬롯 중
**6개(structured/routing/intent/decompose/graph_bind/delegation_extract)만**
n=1,750 규모로 재측정했다. 그 결과로 근거가 확인된 변경은 **2곳뿐**이다 —
**routing(t5, EXAONE→Solar)**와 **graph_bind(t9, A.X→Solar)**
(`docs/model-orchestration.md` §15, `analysis/e2e_report.md` §8.4/§8.5).
근거 수치는 t5(n=569) Solar 494 vs EXAONE 486, t9(n=232) Solar 232/232 vs A.X 223이고,
best-per-slot 조합(1466 / .838)이 현행 §15 diversified 배정(1449 / .828)을
McNemar p=7.6e-5로 유의하게 이긴다.

**intent(t6)는 변경 대상이 아니다** — n=139에서 Solar/EXAONE/A.X가 모두 102/139로
완전 동률이고 프론티어 모델을 포함해도 전 모델이 101~102라, 리포트가 "전 모델 포화 —
변별력이 낮은 슬롯"으로 명시한 구간이다(변경 근거 0).

> (정정 이력) 이 문서 초안은 여기에 `intent(Solar→A.X)`를 더해 "3곳"이라고 적었으나
> 이는 오기였다. graph_bind의 A.X→Solar 변경을 대칭 스왑으로 오독한 것으로 보이며,
> §7.3b 구표본 n=324 표의 "40 (solar·ax)" 동률 표기와
> `orthus/models/orchestration.py:121` 주석에 등장하는 "intent"라는 단어(실제로는 KG
> intent 필드를 가리킨다)가 혼동을 유발했을 가능성이 있다 — 2026-07-29 검증으로 정정.

나머지 **7개 태스크(wiki_qa, synthesize, email_draft, gap_suggest, claim_headline,
distill, followup_rewrite)는 여전히 원래의 소규모 홀드아웃(n=8~30)** 위에 배정이
얹혀 있다. 이 문서는 "이걸 6개 슬롯과 같은 신뢰도로 올리려면 뭘 해야 하는가"를
정리한 것이며, **owner 최종 결정(2026-07-29)으로 자유생성 5종 n=1,000이 확정돼
실행 계획서가 됐다**(§2.0). distill·followup_rewrite는 별도 트랙으로 분리됐다.

## 1. 현재 상태 (태스크별)

| 태스크 | 현재 golden 규모 | 채점 방식 | harness 존재 | 비고 |
|---|---|---|---|---|
| wiki_qa | `experiments/fugu-ko/golden/t2.json` 30 + `experiments/fugu-ko/golden/t2_holdout.json` 30 | LLM judge (근거 인용 판정) | 있음(`experiments/fugu-ko/t2_holdout_judge.py`) | 90% 무승부(Solar/EXAONE), 판정 난이도 자체가 높음 |
| synthesize | `experiments/fugu-ko/golden/t8.json` 8 | LLM judge | 있음(`experiments/fugu-ko/t8_synth.py`) | 표본이 극히 작음(n=8) |
| email_draft | `experiments/fugu-ko/t12_generation.py` 내부, n≈30 | **결정론**(형식 실패율 + `_invented` 환각 탐지) | 있음(9c 계열 실험에서 재사용) | Solar 5/30 vs EXAONE 4/30, p=1.000 — 애초에 무승부 |
| gap_suggest | `experiments/fugu-ko/t12_generation.py` 내부, n≈12 | **결정론**(형식/섹션 수 규격 + `_invented` 환각 탐지) | 있음 | 표본 최소 |
| claim_headline | `experiments/fugu-ko/t12_generation.py` 내부, 소규모 | **결정론**(120자 초과 등 형식 + `_invented` 환각 탐지) | 있음 | 표본 최소 |
| distill | 문서 25개(문항 아님) | 근거이탈률 등 결정론+judge 혼합 | 있음(`experiments/fugu-ko/t11_distill.py`/`t13_distill_cap.py`/`t14_distill_cap.py`) | **단위가 "문서"라 다른 6개와 척도가 다름** — 전체 코퍼스 2,480문서 |
| followup_rewrite | **없음** | **없음** | **없음** | prod flag(`ORTHUS_CHAT_FOLLOWUP_REWRITE_ENABLED`)로만 존재, 벤치마크 자산 자체가 0 |

> 경로 주의: 하네스/골든 자산(`harness_e2e.py`, `golden/`, `t2_holdout_judge.py`,
> `t8_synth.py`, `t11_distill.py`, `t13_distill_cap.py`, `t14_distill_cap.py`,
> `t12_generation.py`)은 전부 **`experiments/fugu-ko/` 직속**이다. `experiments/fugu-ko/e2e/`
> 하위에 있는 것은 이 계획 문서와 `AUGMENT_STATE.md`/`KOREAN_ADVANTAGE_PLAN.md`/
> `SLOT_SWAP_EXPERIMENT_RESULT.md` 같은 문서·매니페스트 자산이다(2026-07-29 실측 확인).

## 2. 태스크별로 해야 할 일

### 2.0 확정된 실행 파라미터 (owner 최종 결정 2026-07-29)

**이 절이 실행 계약이다. 아래 값은 확정이며, 변경하려면 owner 재승인이 필요하다.**

| 항목 | 확정값 |
|---|---|
| 규모 | **5종 전부 n=1,000** (시나리오 A) |
| 워커 로스터 | **7종** — 국내 3 + 프론티어 4 |
| 국내 워커 | `solar` / `exaone` / `ax` |
| 프론티어 워커 | **`claude-opus-4.8`**, **`gpt-5.6-sol`**, **`deepseek-v4-pro`**, **`glm-5-bedrock`** |
| judge | **Claude Sonnet 4.6(Bedrock) + 국내 판정자 1인** (총 2인) |
| judge 규약 | judge∉판정쌍 + A↔B 스왑 양방향(방향 불일치 = tie) |

**명시적으로 제외된 모델(워커 로스터에 넣지 않는다):** Claude Sonnet 4.6(**워커에서
제외 — judge로만 사용**), `gpt-5.3`, Claude Haiku 4.5, baseline `gpt-4o-mini`,
GPT-4o 풀버전, `deepseek-v3.2`.

> **judge∉워커가 깨끗하게 성립한다.** Claude Sonnet 4.6을 워커 로스터에서 뺐기 때문에
> judge 모델이 판정 대상 집합과 전혀 겹치지 않는다 — 자기채점 편향 회피 조건이
> 예외 없이 만족된다.

### 2.1 wiki_qa / synthesize / email_draft / gap_suggest / claim_headline (5종)

**⚠️ 이 5종이 전부 LLM judge라는 초안의 서술은 틀렸다.** 실제로는 채점 방식이 둘로
갈리고, 확장 비용도 그에 따라 크게 다르다(2026-07-29 코드 실측 정정).

#### (a) LLM judge 채점 2종 — wiki_qa(t2), synthesize(t8)

- 자유생성 pairwise 판정이라 **결정론 채점으로 못 바꾼다** — judge 유지. judge 모델은
  평가 대상 3사(Solar/EXAONE/A.X)와 겹치면 안 된다(자기채점 편향). **owner 결정
  2026-07-29: judge는 gpt-4o가 아니라 Claude Sonnet(Bedrock,
  `ORTHUS_LLM_BEDROCK_API_KEY` 경로)으로 교체한다** — 함의와 선행 조건은 §5 참조.
  judge∉판정쌍 + A↔B 스왑 양방향(방향 불일치는 tie) 규약은 유지한다.
- **골든 스키마 실측:** t2/t8 골든은 `{task, desc, note, items}`이고 items 원소는
  **`{id, q}` 단 두 필드**다. 정답도 grounding 소스도 골든에 인라인되지 않는다 —
  근거는 런타임에 실제 `retrieve`가 라이브 wiki-store에서 가져오고, 판정자용 원문은
  실행 산출물의 `sources`(slug)로 `$FUGU_WIKI_STORE`에서 되읽는다.
- **따라서 "grounding 소스(wiki page) 세트도 같이 준비해야 한다"는 초안 서술은
  부정확하다 — 질문만 저작하면 된다.** 라이브 회사 wiki-store
  (`~/.orthus/nodes/company/wiki-store/company/`)에 마크다운 31,617건
  (wiki 4,702 / claims 8,230 / sources 1,851 / tasks 142)이 이미 있다.
- 남는 조건: 문항이 **"위키에 실제 근거가 존재하는 주제"**여야 한다. 따라서 페이지
  본문에서 **역방향으로 질문을 생성한 뒤 `retrieve`가 실제로 히트하는지 검증하는
  파이프라인**이 필요하다.
- **synthesize(t8) 추가 제약:** grounded 리프가 정확히 1개면 LLM 없이 결정론
  passthrough라 **grounded ≥2일 때만 발화**하고, `t8_synth.py`가 `_DENIALS` 문자열로
  grounded 여부를 판정해 grounded≥2 문항만 judge 대상으로 필터한다. 즉 저작한 n의
  일부만 실제 판정에 들어가므로 **목표 n의 여유분을 잡아야 한다.** 합격선은 baseline
  대비 승률 ≥40%(tie 제외).

#### (b) 결정론 채점 3종 — email_draft / gap_suggest / claim_headline

- `experiments/fugu-ko/t12_generation.py`는 **judge를 전혀 쓰지 않는다.** 채점은 전부
  결정론 지표다 — 형식 실패율, 없는 고유명사·수치 삽입 탐지(`_invented`), 섹션 수
  규격, 120자 초과 등.
- **owner 결정 2026-07-29: 이 3종은 결정론 채점을 유지한다** — 기존
  `t12_generation.py` 지표를 그대로 쓰고 **n만 확대**한다. judge를 새로 붙이지 않는다.
- 결과적으로 **judge 호출 비용도 judge 검수 병목도 없어 확장이 훨씬 싸다.** 병목은
  문항 저작뿐이고, 채점은 추론 산출물만 있으면 즉시 계산된다.

#### (c) 공통 — 문항 저작

- golden n을 30 안팎 → **n=1,000**(§2.0 확정)으로 올리려면 **문항 자체를 새로
  만들어야** 한다. 제3벤더(Nova Pro/Llama 3.3 등, 기존 KO-PARITY 실험에서 쓴 생성기
  재사용 가능)로 생성 → 인간/역검증 표본 검수 → freeze.

#### (d) 태스크별 골든 원천 — 실측 + owner 결정 (전부 2026-07-29 실측)

##### claim_headline

- 원천 = `~/.orthus/nodes/company/wiki-store/company/claims/*.md` **8,230건** 중
  40~300자 필터 통과 **2,691건**(실측, `t12_generation.py::_claims()` 로직 그대로
  적용). **n=1,000 확보 가능.**
- 현재 코드는 `N_CLAIMS=20`으로 잘라 쓰고 `sorted()` 앞부분만 뽑는다 → **슬러그 편향을
  피할 샘플링 로직 추가가 필요하다.**

##### wiki_qa

- 위키 페이지 4,702개(슬러그 prefix 1,189종)지만 **front-matter 제외 본문 길이
  중앙값 159자**, 300자↑ 213개, 500자↑ 58개로 **얕다.**
- **→ owner 결정: claims(2,691건)를 grounding 원천으로 함께 사용해 질문을
  역생성한다.** `retrieve` 히트 검증 파이프라인((a) 참조)은 그대로 필요하다.

##### synthesize

- wiki_qa 문항을 **페어링**해 만든다. 단 grounded≥2 필터((a) 참조) 때문에 저작 n의
  일부만 판정 대상이 되므로 **여유분 확보 필요.**

##### gap_suggest

- 실데이터는 PG `data_gaps` **57행뿐**(전부 `status=open`, question 결측 0,
  distinct 57). reason 분포 실측 = `insufficient_grounding` 40 / `missing_link` 10 /
  `no_data` 5 / `weak_retrieval` 2.
- **→ owner 결정: 실데이터 57건을 시드로, 실제 reason 분포를 보존한 합성 확장.**
- **⚠️ 기존 골든 버그 기록:** 기존 골든의 reason 값(`no_hits` / `low_confidence`)이
  프로덕션 enum(`no_data` / `insufficient_grounding` / `weak_retrieval` /
  `missing_link`)과 **불일치했다.** 라이브 원천을 쓰면 이 불일치가 해소된다.

##### email_draft

- 현행 company DB에 **메일 본문 0건** — `email_send_log` / `mail_signatures` /
  `mail_tracking`이 전부 hash-only 스키마이고 0행이다. 0706 스냅샷 DB에 실수신 메일
  735건이 있으나 **전부 `scope=personal` PII**다.
- **→ owner 결정: 템플릿 합성만 사용한다**(수신자 유형 × 요청 의도 × ctx 유무 조합).
  **스냅샷 실메일은 쓰지 않는다.**
- 현재 `EMAIL_ITEMS`는 파이썬 리터럴 30건이므로 **golden JSON으로 외부화**하고
  `score()`의 grounding 재구성 로더도 함께 교체해야 한다.

### 2.2 distill
- 척도가 "문서" 단위라 다른 태스크와 같은 n=1,750 프레임에 억지로 맞추지 않는다.
  **전체 코퍼스(2,480문서) 표본 확대**가 맞는 방향.
- **A.X는 문서당 165초** — 전체 코퍼스를 A.X로 전량 재측정하면 약 5.5일 걸림. 이미
  A.X는 distill 후보가 아니므로(현재도 Solar 배정) A.X는 표본(예: 기존 25~50문서)
  수준만 유지하고, **Solar/EXAONE만 대규모 확대**하는 게 합리적.
- Solar는 문서당 7.3초 → 전체 2,480문서 약 5시간.

### 2.3 followup_rewrite
- **harness부터 새로 만들어야 함.** 골든셋 스키마, 채점 로직(원 질문+히스토리+후속질문
  → 재작성된 질문이 원 의도를 보존하는지 판정), 실행 스크립트 전부 부재.
- 지금 prod에 플래그로만 있고(`ORTHUS_CHAT_FOLLOWUP_REWRITE_ENABLED`) 벤치마크 자산이
  0이므로, 이 태스크는 "재측정"이 아니라 "신규 구축"이다 — 다른 6개와 별도 트랙으로
  다뤄야 함.

## 3. 하네스 선택 — `harness_e2e.py` 쓰지 않는다

기존 6개 슬롯을 쟀던 `experiments/fugu-ko/harness_e2e.py`는 **동시성도 재개(resume)도
없다**(`experiments/fugu-ko/e2e/AUGMENT_STATE.md:73`,
`experiments/fugu-ko/harness_e2e.py:922-946` 완전 순차). 중단되면 처음부터
다시 돌려야 하고, 병렬은 tmux 레인을 사람이 수동으로 띄우는 방식뿐이었다.

반면 이후 KO-PARITY 실험(`experiments/fugu-ko/e2e/KOREAN_ADVANTAGE_PLAN.md` §6.1)에서
이미 검증된 **`arena_run.py`**는:
- `ThreadPoolExecutor` 내장 동시성(기본 worker 6, 벤더별로 20까지 실측 검증됨)
- `load_done_pairs()`가 `(id, condition)` 단위로 진짜 완료 행만 스킵 → 중단 후 재개 가능
- 벤더별 출력 파일 분리(`{system}.jsonl`)라 프로세스 충돌 없음

**⚠️ 소재 주의: `arena_run.py`는 현재 main 브랜치 체크아웃에 존재하지 않는다.**
`.worktrees/ko-parity`, `.worktrees/ko-parity-genexp`, `.worktrees/arena-benchmark`
세 워크트리의 `experiments/fugu-ko/arena_run.py`에만 있다(위 기능 서술은 그 워크트리
코드에서 직접 확인한 것이라 내용 자체는 정확하다). `koparity_run.py`도 마찬가지로
`.worktrees/ko-parity`·`.worktrees/ko-parity-genexp`에만 있다.

**따라서 잔여 태스크를 재측정한다면 `harness_e2e.py`를 확장하지 말고,
워크트리의 `arena_run.py`/`koparity_run.py` 코드를 참고해 **신규 워크트리에서 러너를
새로 작성**한다.** 실측 처리량(§6.3, 벤더당 c=6~20 기준 시간당
5,000~11,000문항)을 그대로 재사용 가능.

## 4. 시간·비용 견적 (실측 기반)

> **⚠️ 주석 1 — 로스터 차이.** 아래 시나리오 A 총계(77,000콜 / $184 / 90~186분)는
> **워커 9종 기준**으로 산출한 값이다. §2.0에서 로스터가 **7종**으로 확정됐으므로
> 워커 콜 수·비용은 이보다 줄어든다. 지배 항목의 순위(비용=프론티어 워커 추론,
> 시간=Sonnet judge 단일 레인)는 바뀌지 않는다.
>
> **⚠️ 주석 2 — 실측 vs 가정.** 아래 표에서 "실측"으로 표기한 것만 계측값이다.
> **프론티어 단가는 전부 가정치**이므로(§5 참조) 비용 숫자는 `--cost-cap-usd`
> 상한 산정용으로만 읽어야 한다.

### 4.1 실측 근거

| 항목 | 값 | 출처 |
|---|---|---|
| wiki_qa 워커 1콜 토큰 | solar 495 in → 99 out / ax 476 → 27 / exaone 477 → 47 | **실측** `analysis/e5-results.md` §2 (n=30) |
| 문항당 LLM 콜 | **1.00 콜/문항** | **실측** 같은 출처 |
| 한국어 chars/token (벤더별) | solar 2.94 / ax 3.30 / exaone 3.70 / **Sonnet 1.00** / opus 1.01 / gpt 1.91 / deepseek 1.76 / glm 1.37 | **실측** |
| judge 프롬프트 길이 | 평균 **1,346자**(근거 566 + 두 답변 297 + 시스템 409), p90 1,596 | **실측** |
| judge 1콜 입력 | 약 **1,350토큰** ≈ **$0.0048/콜** | 실측 길이 × 가정 단가 |

> **⚠️ 토크나이저 차이가 judge 비용의 실질 지배 요인이다.** 한국어 chars/token이
> 벤더마다 3.7배까지 벌어져, **동일 프롬프트가 solar 246토큰 → Sonnet 611토큰으로
> 2.49배**가 된다. judge를 Sonnet으로 정한 이상 이 배수는 고정 비용이다.

### 4.2 시나리오 A 총계 (워커 9종 기준 — 주석 1 참조)

| 지표 | 값 | 지배 항목 |
|---|---|---|
| 총 콜 수 | **77,000콜** | — |
| 총 비용 | **$184** | **프론티어 워커 추론이 전체의 45%** |
| 총 소요 | **90~186분** | **Sonnet judge 단일 레인(12,000콜)** |

### 4.3 레버 분석 — 판정자 축소는 효과가 없다

- **판정자를 2인 → 1인으로 줄여도 비용·시간이 거의 안 줄어든다: $184 → $183, 바닥
  레인 불변.** 없애는 쪽이 값싼 국내 판정자이기 때문이다.
- 따라서 **실효 레버는 둘뿐이다: (1) judge n 축소(전수 판정 대신 서브샘플링),
  (2) 프론티어 워커 제외.** 판정자 수 조정은 레버가 아니다.
- 결정론 3종(email_draft/gap_suggest/claim_headline)은 judge 콜이 0이라 이 비용
  구조에서 완전히 자유롭다(§2.1b).

### 4.4 사람 작업 (병렬화로 안 줄어드는 부분)

| 단계 | 병목 종류 | 예상 소요 | 병렬화로 줄어드나 |
|---|---|---|---|
| golden 5종 저작 n=1,000 | 사람 검수 | 반나절~1일 | **거의 안 줄어듦** — 검수는 순차 판단 작업 |
| judge 일치율 파일럿(gpt-4o ↔ Sonnet, t2 30문항) | 설계+실행 | 실행 전 선행 필수(§5) | 소규모라 무관 |
| 결정론 채점 | 계산만 | 사실상 즉시 | 병목 아님 |
| (별도 트랙) followup_rewrite harness+golden 신규 구축 | 설계+구현 | 1일 이상 | 안 줄어듦 |
| (별도 트랙) distill golden 확대 | Solar만이면 계산량 적음 | Solar 전량 약 5시간 | 벤더 레인 병렬화로 단축 가능 |

## 5. 리스크 / 열린 질문

### 5.1 로스터 관련 리스크 (실행 전 처리 필요)

- [ ] **PREREG 사전등록 위반 — 정정문 기록 필요.** KO-PARITY 로스터는
  `golden/ko-parity/PREREG.md`에 **사전등록된 항목**이고, deepseek 슬롯은 원래
  **V3.2**로 등록돼 있었다. 이번에 `deepseek-v4-pro`로 바꾸는 것은 **사후 교체**이므로
  **정정문 기록이 필요하다.**
  - 다만 **되돌리는 것 자체는 안전하다** — 제외 사유가 성능·안정성 문제가 아니라
    "버전 사양 준수 + 효율"이었기 때문이다(카나리에서 v4-pro가 V3.2 대비
    **p50 3,093ms vs 1,472ms로 2.10배 느리고** completion 167.5tok 추론토큰 상수).
- [ ] **⚠️ deepseek 슬러그 default 함정.** **2026-07-24 15:59 UTC 이후
  `deepseek-chat`이 `deepseek-v4-flash`로 매핑된다**(`experiments/fugu-ko/e2e/PHASE6_MODEL_IDS.md`).
  새 실행에서 슬러그만 쓰면 **조용히 다른 모델이 잡힌다.**
  - **대응: `ORTHUS_LLM_DEEPSEEK_MODEL` 또는 `--system deepseek-v4-pro`로 모델 ID를
    명시 pin해야 한다.**
  - 기존 n=1,750 결과는 **그 매핑 이전 실행(2026-07-22 시작)이라 오염되지 않았다.**
  - v4-pro는 **직접 API(api.deepseek.com) 경로만 가능**하다 — Bedrock에 V4 계열은
    없다(`deepseek.v4-pro` Converse는 400).
- [ ] **GLM 경로 제약 — Bedrock만 허용.** z.ai 직접 API는 과거 **~$10 추론토큰 과금
  사고 + 16×429 좌초** 이력으로 PREREG §13/§12.3이 **명시 배제**했다. 승인된 경로는
  **Bedrock `zai.glm-5`(inference prefix 없음)** 뿐이다. 단가($1.4/$4.4)는 **코드
  주석상 가정치**다.
- [ ] **⚠️ 프론티어 단가 신뢰도 — 확정 단가가 하나도 없다.** 이번 로스터 프론티어
  4종의 단가는 전부 코드 주석상 **"공개가 미확인, 가정/추정"**이다 —
  opus-4.8($5/$25), gpt-5.6-sol($2.5/$15), deepseek-v4-pro($1.74/$3.48),
  glm-5($1.4/$4.4). **비용 견적은 `--cost-cap-usd` 상한 산정용으로만 읽어야 한다.**

### 5.2 결정 완료 / 미해결

- [x] **(owner 결정 2026-07-29) 범위 = 자유생성 5종만, 전부 n=1,000(시나리오 A).**
  이번 트랙은 wiki_qa/synthesize/email_draft/gap_suggest/claim_headline **5종만**
  진행한다(확정 파라미터 전체는 §2.0).
  **distill과 followup_rewrite는 별도 트랙으로 분리**한다 — distill은 단위가 "문서"라
  척도가 다르고(§2.2), followup_rewrite는 재측정이 아니라 신규 구축이다(§2.3).
- [x] **(owner 결정 2026-07-29) 작업 위치 = 신규 워크트리.** main 체크아웃이 아니라
  새 워크트리에서 진행한다(§3의 러너 신규 작성 방침과 동일 위치).
- [x] **(owner 결정 2026-07-29) 결정론 3종은 결정론 유지.** email_draft/gap_suggest/
  claim_headline은 기존 `t12_generation.py` 결정론 지표를 그대로 쓰고 **n만 확대**한다.
  judge를 새로 붙이지 않는다(§2.1b).
- [x] **(owner 결정 2026-07-29) judge = Claude Sonnet 4.6(Bedrock,
  `ORTHUS_LLM_BEDROCK_API_KEY` 경로) + 국내 판정자 1인 — 단 일치율 파일럿 선행 필요.**
  judge를 쓰는 건 wiki_qa(t2)/synthesize(t8) 2종뿐이며, gpt-4o가 아니라 Claude
  Sonnet 4.6으로 교체한다. **Sonnet 4.6은 워커 로스터에서 제외됐으므로(§2.0)
  judge∉워커가 깨끗하게 성립한다.** 함의 3건:
  - **(a) 선행 조건:** E4 PoLL 신뢰도 실측치는 **gpt-4o를 기준 judge로 삼아** 얻은
    것이라 Claude Sonnet judge에 그대로 적용되지 않는다. **본 실행 전, 기존 t2 골든
    30문항 규모로 gpt-4o judge와 Claude Sonnet judge의 판정 일치율/kappa를 재는
    소규모 파일럿이 선행되어야 한다.**
  - **(b) 비교 불가:** 과거 t2/t8 측정치는 gpt-4o judge 기준이므로 이번 결과와 **직접
    수치 비교는 성립하지 않는다.**
  - **(c) 편향 조건은 충족:** Claude Sonnet은 평가 대상 3사(Solar/EXAONE/A.X)와 겹치지
    않아 자기채점 편향 회피 조건을 만족한다. judge∉판정쌍 + A↔B 스왑 양방향(방향
    불일치는 tie) 규약은 유지한다.
- [x] **judge 리스크 서술 정정(2026-07-29).** 초안은 "제3벤더 judge가 한국어 판정에
  부적합했던 전례"라고 잘못 일반화했다. 실제 §2.3c 전례는 **Bedrock Llama 3.3에게
  한국어 어문규범 정오 판정(사물존칭 '품절이십니다' 등)을 시킨** 케이스이고, 대응도
  judge 교체가 아니라 **C군 gold를 결정론/큐레이션으로 전환하고 LLM 판정을 참고
  신호로 강등**한 것이었다. 교훈은 **"규범 정오 판정을 LLM에 위임하지 말라"**이지
  "자유생성 pairwise judge를 쓰지 말라"가 아니다.
  - 자유생성 pairwise judge에는 이미 검증 기록이 있다 —
    `experiments/fugu-ko/analysis/e4-results.md`(E4 PoLL, 2026-07-13, 360콜)에서
    gpt-4o 대비 일치율 **Solar 83% / kappa 0.73, A.X 73% / 0.54, EXAONE 72% / 0.53**으로
    사전 기준(일치율 ≥70%, kappa ≥0.4)을 **3종 모두 통과**했고 순위·수용선도 유지됐다.
  - 주의사항 2건: **EXAONE는 judge로 부적합**(변별력 없이 천장, F-E4d),
    **A.X는 장문 판정 입력에서 JSON 계약 7.5% 위반**(F-E4e).
- [ ] golden 저작에 제3벤더(Nova Pro/Llama 3.3 등) 생성기를 쓸 경우 API 비용 발생 —
  기존 KO-PARITY 실측 기준 $6~10 수준이었으나 5개 태스크 동시 저작이면 규모가 다를 수
  있음. **(미해결)**

별도 트랙으로 넘긴 항목(이번 범위 밖, 기록용):

- distill을 "문서 수"로 확장할 때 A.X를 표본 규모로만 유지하는 게 맞는지 —
  다른 6개 슬롯처럼 "동일 n"을 고집하면 A.X 하나 때문에 전체가 5일 이상 걸림.
- followup_rewrite는 지금 prod flag조차 실사용 데이터가 없어 golden 저작 시
  "무엇이 정답인가"를 정의하는 것 자체가 설계 작업 — 단순 확장이 아님.

## 6. 참고 문서

- `docs/model-orchestration.md` §15 — 현재 프로덕션 배정표(SoR)
- `experiments/fugu-ko/e2e/SLOT_SWAP_EXPERIMENT_RESULT.md` — 6개 슬롯 재측정의
  선행 negative result(소표본 시점) 및 이후 n=1,750 뒤집힘 경위
- `experiments/fugu-ko/e2e/KOREAN_ADVANTAGE_PLAN.md` §6 — `arena_run.py` 병렬화
  실측치, 벤더 레인 전략, 배치 API 검토 후 기각 근거
- `experiments/fugu-ko/e2e/AUGMENT_STATE.md` — `harness_e2e.py`의 동시성/재개 부재
  실측 근거
- `experiments/fugu-ko/e2e/PHASE6_MODEL_IDS.md` — 모델 ID/슬러그 매핑 SoR.
  **deepseek 슬러그 default 함정(2026-07-24 15:59 UTC 이후 `deepseek-chat` →
  `deepseek-v4-flash`)** 근거
- `golden/ko-parity/PREREG.md` — KO-PARITY 로스터 사전등록(§12.3/§13 z.ai 직접 API
  배제 근거 포함). deepseek V3.2 → v4-pro 사후 교체 정정문을 여기에 기록해야 함.
  **⚠️ `arena_run.py`와 마찬가지로 main 체크아웃에 없다** — 실측 경로는
  `.worktrees/ko-parity/experiments/fugu-ko/golden/ko-parity/PREREG.md`
  (2026-07-29 확인)
- `experiments/fugu-ko/analysis/e5-results.md` §2 — 워커 토큰/콜 실측(n=30)
- `experiments/fugu-ko/analysis/e4-results.md` — E4 PoLL(2026-07-13, 360콜) 자유생성
  pairwise judge 신뢰도 실측(gpt-4o 기준 일치율/kappa, F-E4d EXAONE judge 부적합,
  F-E4e A.X JSON 계약 위반)
- `docs/agent-chat-answer-quality.md` — wiki_qa 관련 실제 답변 품질 이슈(참고용,
  이 계획과 직접 연결되지는 않음)
