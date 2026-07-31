# [핸드오프] 국내 LLM 조립모델 대규모 재측정 — 정량적 우수성 소스자료

작성일 2026-07-30.

---

## 0. 이 문서의 용도와 주의사항

이 문서는 **순수 소스자료**다. 보고서 문장을 어떻게 고치라거나 기존 초안이 맞다/틀리다를 평가하는
내용은 담지 않는다. 대신 (1) 이번 대규모 재측정이 왜 필요했는지, (2) 그 측정을 만든 아키텍처와
방법론, (3) 실험 규모, (4) 측정에서 나온 핵심 인사이트, (5) 그 결과로 나온 "13개 슬롯 최종 배정(안)"
표, (6) 정량적 우수성 서술에 바로 붙여 쓸 수 있는 수치 뭉치, (7) 그 수치들이 반드시 함께 전달해야
할 한계, (8) 아직 결정되지 않은 사항을 순서대로 정리했다.

**가장 중요한 전제 세 가지를 먼저 밝힌다.**

1. **`orthus/models/orchestration.py::ASSIGNMENTS`는 이 핸드오프 시점까지 코드에서 바뀌지 않았다.**
   아래 5장의 "최종 배정표"는 owner 승인을 가정한 **권고안**이지, 이미 merge된 상태가 아니다.
   보고서에 "구현 완료"로 서술하면 사실과 어긋난다.
2. **모든 수치는 출처 파일을 갖고 있다.** 아래에서 숫자 뒤 괄호 안 약어는 파일 경로를 가리키며,
   0장 끝의 "출처 파일 약어표"에서 전체 경로를 확인할 수 있다. 숫자를 다른 문서에 옮길 때 이
   출처를 함께 들고 가라 — 특히 7장의 한계는 숫자 자체만큼 중요하다.
3. **다섯 개의 서로 다른 "조립모델" 벤치마크가 존재하고, 서로 합산하면 안 된다.** 표본 크기와
   태스크 구성이 벤치마크마다 다르며(6작업 vs 13작업, n=145~2,899), 결론도 벤치마크에 따라
   "조립이 프론티어와 동률"에서 "조립이 16개 중 13~14위"까지 갈린다. 2장 A0에서 이 다섯을
   먼저 정리한다.

이 문서가 하지 않는 일: 기존 보고서 초안(`dump.txt`)의 문장을 고치거나 평가하지 않는다. 이 문서는
그 초안을 쓴 사람이 참고할 **원재료**이지, 그 초안에 대한 리뷰가 아니다.

### 출처 파일 약어표

| 약어 | 절대 경로 |
|---|---|
| E2E | `<repo>/experiments/fugu-ko/analysis/e2e_report.md` |
| RAW1750 | `.worktrees/golden-expand/experiments/fugu-ko/analysis/raw/phase6_verified_stats_expanded.json` |
| SUM1750 | `.worktrees/golden-expand/experiments/fugu-ko/analysis/raw/phase6_expanded_summary.json` |
| REM | `.worktrees/remaining-slots/experiments/fugu-ko/analysis/remaining_report.md` |
| REMJ | `.worktrees/remaining-slots/experiments/fugu-ko/analysis/remaining_summary.json` |
| MO-main / MO-kp | `docs/model-orchestration.md`(main) / 동일 파일의 `.worktrees/ko-parity` 버전(최신) |
| EMB | `experiments/fugu-ko/embedding/README.md` |
| PDD / INS | `experiments/fugu-ko/analysis/pipeline-deep-dive.md` / `INSIGHTS.md` |
| B1L2 / D9 / D10 / M7 / F4 / PROMO / E5 | `experiments/fugu-ko/analysis/{b1-layer2-orchestration,d9-results,d10-results,m7-results,f4-results,promo-metrics,e5-results}.md` |
| ARENA / ASP | `.worktrees/arena-benchmark/experiments/fugu-ko/analysis/{arena-p6a-verdict,arena-scoring-parity}.md` |
| H2H / PA / RFH / DCH / PSV | `experiments/fugu-ko/analysis/{harness-headtohead-handoff,harness-rematch-premise-audit,report-fix-handoff,diversification-cost-handoff,prod-slots-verify-results}.md` |
| PLAN / COST / JP / GT12 / GWQ / GSN | `experiments/fugu-ko/e2e/REMAINING_SLOTS_SCALEUP_PLAN.md`(main) / `.worktrees/remaining-slots/.../e2e/{COST_ESTIMATION_LESSON,JUDGE_PILOT_RESULT,GOLDEN_T12_NOTES,GOLDEN_WIKI_QA_PILOT,GOLDEN_SYNTHESIZE_NOTES}.md` |
| WB / BFCL / E4 / B4 / B4x | `experiments/fugu-ko/analysis/{wb-results,bfcl-results,e4-results,b4-results,b4-x2x3-results}.md` |
| CODE | `orthus/models/orchestration.py` |
| b3-results / e1-results / d0-findings | `experiments/fugu-ko/analysis/{b3-results,e1-results,d0-findings}.md` |

경로가 `.worktrees/`로 시작하는 파일은 main 브랜치가 아니라 해당 워크트리에만 존재한다(예:
`golden-expand`, `remaining-slots`, `arena-benchmark`, `ko-parity`). 보고서에 인용할 때는 이
사실도 함께 유의해야 한다 — 실험 산출물이 아직 여러 워크트리에 흩어져 있고, main으로 완전히
merge되지는 않았다.

---

## 1. 실험 배경과 목적

`docs/model-orchestration.md`의 §1~§3(초판)은 "국내 LLM 3사(Solar·EXAONE·A.X)를 13개 작업
슬롯에 나눠 배정하면 단일 모델보다 +7.7%p 낫다"는 결론을 냈다. 이 결론에는 두 가지 결함이
있었다(§11.1, MO-kp). 첫째, 배정 규칙을 **골든셋에서 뽑아 놓고 같은 골든셋에서 채점**하는
in-sample 낙관 편향이었다. 둘째, 실험 하네스가 main보다 52커밋 뒤처진 코드를 임포트하고
있었다 — 즉 프로덕션이 아닌 코드를 재고 있었다(예: 이 옛 코드로는 EXAONE structured가
58.5%였는데, main 코드로 다시 재니 75.6%였다).

이 두 결함을 걷어내기 위해 2026-07-14, 규칙 도출에 전혀 쓰이지 않은 **신규 홀드아웃**을
만들어 재측정했다(structured 41 · routing 28 · intent 20 · decompose 16 · graph_bind 32 ·
delegation 22, MO-kp §11). 재측정 결과 "작업별 분할 배정이 유리하다"는 초기 결론은 **기각**됐다
— 국내 3모델 간 어떤 쌍대 비교도 p>0.05로 유의하지 않았다(MO-kp §11.3). 여기에 그치지 않고
현행 gpt-4o-mini를 표에 되돌려 넣자 더 근본적인 사실이 드러났다 — **새 문항에서 국내 모델이
현행(gpt-4o-mini)을 유의하게 이긴 작업이 하나도 없었다**(MO-kp §11.3b). 즉 배정의 진짜 근거는
성능 우위가 아니라 ① GPT 벤더 금지(owner 결정, 2026-07-14), ② 지연(Solar 698ms vs 현행
~1,100ms), ③ 안전성(delegation_extract 오탐 EXAONE 0 · Solar 1 · A.X 4) 셋뿐이었다(MO-kp
§11.3b). 이 재측정 이후 §12에서는 객관 채점이 안 되는 생성형 태스크(wiki_qa·email_draft·
gap_suggest·claim_headline·distill)도 마저 측정해, 결국 "**단일 Solar 1차 + 안전 예외
2건**"(delegation_extract→EXAONE, 2차 검증→A.X)으로 정리됐다(MO-kp §11.4/§12.4).

2026-07-20에는 owner가 **동점 구간 안에서** 의도적으로 배정을 다양화하기로 결정했다(§15,
MO-kp) — 대회 E2E 벤치마크가 "작업별 멀티모델 오케스트레이션" 자체를 증명해야 하는데, 유의차가
없다는 이유로 전 작업을 한 모델에 몰아넣으면 그 사실이 코드에서 보이지 않기 때문이다. 이건
새로운 성능 근거가 아니라 **동점 구간 안에서의 선택**이라는 점이 §15에 명시돼 있다.

**이번(2026-07-29~30) 대규모 재측정이 다시 필요했던 이유**는, §11/§12/§15가 딛고 있던 홀드아웃이
n=16~41(객관 채점 6작업)과 n=30(생성 5작업)이라는 **작은 표본**이었기 때문이다. n이 작으면
"동점"이라는 결론 자체가 "차이가 없다"의 증거가 아니라 "차이를 검출할 검정력이 없다"의 증거일
수 있다 — 이 구분은 4.1에서 실측 수치로 확인한다. 그래서 6개 객관 채점 작업을 **n=1,750**(E2E
Phase 8, 2026-07-22 측정)으로, 나머지 5개 생성/판정 작업을 **n=1,000(email_draft·gap_suggest·
claim_headline·wiki_qa)** 및 **n=747(synthesize)**로(REM, 2026-07-29 측정) 대규모 재측정했다.
이 문서가 다루는 "최종 13-슬롯 배정표"(5장)는 이 두 대규모 재측정의 결과다.

---

## 2. 실험 아키텍처

### 2.1 대상 시스템: 13개 태스크 슬롯 조립

제품이 실제로 호출하는 LLM 작업은 13개 슬롯으로 나뉘어 있고(`CODE` L32-44), 각 슬롯은 코드
상수 딕셔너리(`ASSIGNMENTS`)로 모델에 고정 배정된다 — 런타임 추론이나 신뢰도 라우팅이 아니다.

- **객관 채점 가능 6종**(정답이 하나로 정해짐): `structured`(t3, NL→SQL), `routing`(t5, 질문
  분기), `intent`(t6), `decompose`(t7, 복합질문 분해), `graph_bind`(t9, KG 엔티티 바인딩),
  `delegation_extract`(t10, 위임 의도 추출).
- **생성/판정 필요 5종**: `wiki_qa`(t2), `synthesize`(t8), `email_draft`, `gap_suggest`,
  `claim_headline`.
- **자유 텍스트 요약 1종**: `distill`(문서→claim 추출).
- **골든/하네스가 존재하지 않는 1종**: `followup_rewrite`(PLAN §1 L46; `CODE` L127-130).

13개 전부가 Solar·EXAONE·A.X 3사 중 하나에 배정돼 있고, 임베딩 슬롯(`ORTHUS_EMBEDDING`)은
Chat 슬롯(`ORTHUS_LLM`)과 코드에서 완전히 분리돼 별도로 Solar `embedding-passage`에 배정됐다
(§14, MO-kp).

### 2.2 골든셋 저작 (5종, 방법론 차이)

같은 "골든셋"이라는 말 안에 실제로는 다섯 가지 다른 저작 방법론이 섞여 있다.

- **wiki_qa (n=1,000)**: 실 wiki claim/page에서 역생성했다. 팩트형 600(사전선언 전 55 +
  사전선언 후 545, 경계는 타임스탬프 컷, 격차 727.0초) + synthetic_broad 400. `retrieve` 결과
  순위로 채택 여부를 검증했고(1위 145 · 2위 315 · 3위 50 · 4위 70 · 5위 20), 형식 부적합
  175건(opaque_id 60 · answer_leak 41 · yes_no 20 · too_long 19 · deictic 18 · slug_leak 8 ·
  contact_pii 8)을 걸러냈다(GWQ §7.1).
- **synthesize (n=747, 900건 중 채택)**: 실 지식 2건을 조합한 복합질문을 만들고, 두 조각 모두
  실질적으로 답변 가능(grounded≥2)한 문항만 통과시켰다 — 900건 중 83.0%인 747건이 통과했다
  (GSN §5). retrieve 통과율은 사전선언 이전 89%에서 처방(prescription) 이후 67%로 떨어졌는데
  (I18), 이는 더 엄격한 채택 기준이 통과율을 낮춘 결과다.
- **email_draft / gap_suggest / claim_headline (각 n=1,000)**: 실 데이터를 씨앗으로 결정론
  템플릿 확장을 했다. email_draft는 실 30건 + 합성 970건(51개 수신자 유형 × 9개 클래스 ×
  61개 의도 × 맥락 유무), gap_suggest는 실 `data_gaps` 57건(insufficient_grounding 40 ·
  missing_link 10 · no_data 5 · weak_retrieval 2) + 비례 결정론 합성 943건(LLM 호출 0회),
  claim_headline은 실 클레임 8,230건을 40-300자 필터→텍스트 중복제거(2,691)→해시 정렬로
  1,000건을 뽑았다(GT12).
- **하이브리드 특징**: 이 다섯 중 셋(email/gap/headline)은 **결정론 채점**(성공/실패를 코드
  규칙으로 판정)이 가능해 판정자 편향 문제가 없다. 나머지 둘(wiki_qa/synthesize)은 자유 텍스트
  생성이라 **LLM 판정자**(주판정자 Claude Sonnet 4.6, 쌍대 비교 + 양방향 스왑)가 필요하다 —
  이 차이가 4.4의 "판정자 신뢰도" 논의로 이어진다.

### 2.3 실행 하네스

REM 실행은 7개 워커(국내 3사 solar/exaone/ax + 프론티어 claude-opus-4.8/gpt-5.6-sol/
deepseek-v4-pro/glm-5-bedrock) × 최대 4,747개 항목/모델으로, 총 33,229콜을 돌렸다(COST L3,
L22). email_draft/gap_suggest/claim_headline 세 태스크는 `t12_generation.py` 결정론
지표를 그대로 재사용했고, wiki_qa/synthesize는 쌍대 judge를 새로 붙였다. 예산 상한
때문에 claude-opus-4.8이 4,747건 중 3,591건에서 멈췄고, 처리 순서가 `(task, id)`라
빠진 1,156건이 **wiki_qa 전량 + synthesize 156건**에 몰리는 계통적 결손이 생겼다(I41) —
나중에 owner 승인으로 top-up했다.

E2E Phase 8(n=1,750)은 11개 모델(국내 3사 + 프론티어 8종) × 1,750개 공통 채점 항목을
7개 벤더 레인으로 병렬 실행했고(exaone은 3-way shard), 에러 행 0건·`model.fallback` span
0건("CLEAN")이었다(RAW1750 `error_triage: {}`).

### 2.4 판정 방법론

결정론 3종(email/gap/headline)은 "성공 = ok ∧ 규칙 위반 없음"으로 정의한 이진 지표를 쓴다.
판정이 필요한 wiki_qa/synthesize는 주판정자(Claude Sonnet 4.6)가 두 응답을 놓고 승자를
고르되, **양방향 스왑**(A×B, B×A 둘 다 실행)으로 위치 편향을 확인하고, 두 방향의 판정이
갈리면 **tie로 처리**한다(REM §2 header). 판정자 자기일관성도 별도로 쟀다 — 예를 들어
solar 판정자는 tie율 68.8%(688/1,000), Sonnet은 39.2%(1,864/4,750)로 판정자마다 tie
성향이 크게 다르다(REM L292-295).

### 2.5 통계 검정

McNemar exact(양측), Fisher exact, Wilcoxon signed-rank, Mann-Whitney, 부트스트랩
paired-diff CI95(`n_resamples=10000, seed=1234`), Holm 단계적 하강(step-down), BH FDR,
Cohen's kappa를 문서별로 다르게 적용한다(E2E §7 L571-572; REM §4; ARENA §4; H2H L9; EMB
§8.5). **REM은 명시적으로 "p(Holm)이 유의성의 정본이다"라고 선언한다**(REM §4 L455) — 63개
쌍(3작업 × 21쌍)의 결정론 지표와 wiki_qa/synthesize 각 17쌍의 judge 지표 모두 Holm 보정값을
나란히 병기했다. 반면 E2E n=1,750의 55쌍 43건 유의는 **명목(nominal) p<0.05로, 다중비교
보정을 적용하지 않은 값**이다(E2E §8.3 L899) — 이 구분은 7장에서 다시 다룬다.

---

## 3. 규모

- **E2E Phase 8**: 11개 모델 × 공통 채점 **1,750**항목(모델별 전량 1,750, 에러 행 0). tier-A
  골든이 851→1,884건(+1,033)으로 커졌고, 그중 신규 1,033건은 결정론(DB) 474 / Bedrock Nova
  Pro 500 / 실로그(query_runs) 82 / Bedrock Llama 3.3-70B 118로 구성되며 **국내 3사·Bedrock
  Claude 생성기는 의도적으로 제외**했다(E2E §8.1 L848-851). 태스크별 n: t3 343 · t5 569 ·
  t6 139 · t7 290 · t9 232 · t10 177.
- **REM(잔여 5슬롯)**: 33,229콜(7워커 × 최대 4,747항목), judge 판정단위 wiki_qa
  **10,250** + synthesize **8,226**(COST L3, L22; REM L154, L318). 골든 크기는
  email_draft/gap_suggest/claim_headline/wiki_qa 각 1,000, synthesize 747(900건 중
  83.0% 채택).
- **Arena P6a**: 14개 실측 시스템 + 조립 2종 = 16 시스템 × **2,899**항목/시스템, 13개
  시스템 100% 완주(gpt-5.6-sol은 2,898/2,899, 영구 `400 cyber_policy` 1건 결측), 에러 0
  (opus-4.8/gpt-5.6-luna 기준). 이 벤치마크가 딛고 선 wiki-store 코퍼스는 마크다운
  31,617개(wiki 4,702 · claims 8,230 · sources 1,851 · tasks 142)다(ARENA §0; PLAN §2.1a).
- **B1L2**: 6(후 7)개 모델 × 241 dispatch, 145 채점("오류/429 = 0"). **D9**: 12 arm ×
  55 신규 항목(기존 145 byte-identical 재사용)→n=200. **D10**: 10 arm × 84 신규→n=168.
  **F4a**: 9모델, 36쌍 × 30항목 × 위치스왑 = **2,160표**. **M7**: 20과제, LLM콜
  sonnet 67/solar 69/exaone 95/ax 5.
- **비교 대상 모델 수**: E2E 11개, Arena 16개 시스템, D9 12개 arm, REM 7개 워커.
- **에러/실패**: E2E n=1,750 에러 행 0, fallback 0(RAW1750); REM judge pilot API 실패
  0/JSON 파싱 실패 0; REM 형식 실패는 워커별 편차가 컸다 — A.X 248건(그중 247건이 끝
  여분 `}` 하나), GLM 79건(전부 마크다운 펜스), EXAONE 9건(복구 불가)(REM §0-3).

---

## 4. 핵심 발견 (인사이트)

### 4.1 "동점"은 모델의 사실이 아니라 검정력 부족이었다

E2E의 국내-대-프론티어 쌍대비교 유의 건수는 표본이 커질수록 늘었다: **n=145에서 36쌍 중
8건 유의**(E2E §6.0 L414) → **n=324에서 36쌍 중 11건 유의**(9모델 기준, E2E §6.0 L428) →
**n=1,750에서 55쌍 중 43건 유의**(명목 p, 보정 없음, E2E §8.3 L899). 같은 방향의 증거가
개별 슬롯에서도 나온다 — §15가 "동점이라 다양화했다"고 선언한 슬롯 중 최소 두 곳은
2026-07-28 재감사에서 애초에 동점이 아니었던 것으로 드러났다: **routing은 Solar가 87승
1패(McNemar p=5.75e-25)**, **wiki_qa는 34승 0패(p=1.2e-10)**였다(MO-kp §15 각주
`[^ko-parity-revert]`). 그리고 n=324에서 "동률"로 읽혔던 EXAONE의 지위 자체도 n=1,750에서는
성립하지 않는다 — EXAONE은 4개 프론티어 모델 전부에 유의하게 뒤진다(p<1e-4, GLM 대비
p=0.0002)(E2E §8.0 L809-812). 즉 "차이가 없다"는 이전 결론들의 상당수는 실은 "그 표본
크기로는 차이를 볼 수 없었다"였다.

### 4.2 차이가 없는 건 태스크가 포화된 슬롯뿐이다

반대 방향의 증거도 명확하다 — 진짜로 변별력이 없는 슬롯이 존재한다. `intent`(t6, n=139)는
Solar·EXAONE·A.X **셋 다 정확히 102/139**로 완전히 동일했다(E2E §8.4 L927). `decompose`(t7,
n=290)에서도 Solar와 EXAONE이 **154/290으로 동일**해 McNemar가 불일치 0으로 p=1.000을
반환했다(C2 REM L323급 패턴, C1 표). MO의 홀드아웃(n=6, LLM이 실제로 판단을 내린 문항만)
분석은 이 포화 현상의 이유를 짚는다 — intent 배정의 전체 근거가 사실은 **n=6**짜리
문항이었다(MO-kp §11.2 L636-639). 이런 슬롯에서는 "동점"이 검정력 부족이 아니라
**진짜 천장 효과**다 — 4.1의 발견과 반대 극단이며, 두 현상을 구분하는 것이 이 재측정의
핵심 기여다.

### 4.3 A.X의 JSON 취약성은 상당 부분 파서 엄격함의 문제였다

REM은 A.X의 결정론 3종 형식 실패 248건을 직접 열어봤다. **247건(99.6%)이 본문 끝의 여분
`}` 하나**였고, GLM의 형식 실패 79건은 **전부 마크다운 코드펜스**였다(REM §0-3 L17-19).
judge 채점에서도 비슷한 일이 있었다 — A.X 위반율이 한때 61~66%로 나와 "판정자 자격 미달"로
판단했지만, 실제 출력은 `{"winner": "A", "reason": 따옴표 없는 한국어}` 형태였고 **집계에
쓰는 `winner` 키는 항상 온전**했다. 불필요한 `reason` 필드 파싱 실패 때문에 멀쩡한 판정을
버리고 있었던 것이다. `winner` 키만 회수하는 규칙 두 줄을 적용하자 **위반율이 61~66%에서
0.0%로** 떨어졌고, bad_json 1,882건 전수 중 **100%가 API 재호출 없이 복구**됐다(REM §0-3
L18). 이 발견은 실행 가능성이 가장 큰 항목으로 명시돼 있다 — "**E4에서 잰 A.X의 7.5% JSON
위반치도 같은 의심을 받아야 한다**"(REM §0-3 L19, I19).

### 4.4 판정자 간 일치도가 낮아 생성형 태스크는 단일 판정자로 못 바꾼다

wiki_qa/synthesize에서 국내 보조 판정자(A.X, EXAONE)와 주판정자(Sonnet)의 Cohen's kappa는
**0.033~0.240** 범위로, 대부분 "사실상 우연 수준"이다 — wiki_qa: ax↔exaone 0.240 · ax↔sonnet
0.148 · exaone↔sonnet 0.112 · solar↔sonnet 0.521; synthesize: ax↔exaone 0.126 · ax↔sonnet
0.102 · **exaone↔sonnet 0.033** · solar↔sonnet 0.317(REM L301-304, L399-402). 판정자 파일럿
단계에서도 이미 같은 패턴이 나왔다 — gpt-4o↔Sonnet 불일치 9건이 **예외 없이 전부 gpt-4o가
solar를 후하게 보는 방향**이었고, 그 결과 gpt-4o 기준 "solar가 ax를 16-5로 이긴다"가 Sonnet
기준으로는 "11-10 무승부"로 바뀌었다(REM §0-2 L13; JP L65-76). 이 때문에 wiki_qa/synthesize의
배정 권고는 "주판정자 단독 + 결정론 부가지표(인용마커 위반율 등) 정합"에만 근거하며, REM
스스로 "**단일 판정자에 슬롯을 걸 수는 없다**"고 명시한다(REM §3 L451, I9).

### 4.5 다양화 자체에 실측 비용이 있었다

§15의 "동점 구간 안에서 다양화" 결정을 n=1,750으로 다시 확인하니, 실제로 최선-슬롯별-배정
(best-per-slot, 모든 슬롯을 각자 최고 성능 모델로 채움: 1,466/1,750=**83.77~83.8%**)과 §15
다양화 배정(1,449/1,750=**82.8%**) 사이에 **McNemar p=7.6e-5**로 유의한 차이가 있었다
(E2E §8.5 L950; SUM1750 `diversification_cost.true_domestic_best_vs_current_production_p`).
차이는 17개 항목 = routing 8개 + graph_bind 9개이고, structured/intent/decompose/
delegation_extract는 두 표가 같은 모델을 고르므로 기여가 0이다(RFH §2.1 O4 L104-105). 즉
"보여주기 위한 다양화"는 공짜가 아니라 약 1pp의 정확도를 대가로 치른다 — 그 자체가 하나의
실측 결과다.

### 4.6 지연 순위도 표본 크기에 따라 뒤집혔다

n=145(B1L2)에서는 국내 워커가 프론티어보다 **1.7~4.6배** 빠르다고 측정됐다(B1L2 L114;
solar 514ms · exaone 417ms · gpt-4o 858ms · Sonnet 4.6 1,938ms). n=324(E2E §7.4)에서는
순위가 조금 바뀌어 EXAONE 295ms · A.X 663ms · Solar 681ms · GLM-5.2 3,591ms/p95
18,319ms였다. n=1,750(RAW1750, 어떤 .md에도 발표되지 않은 원자료)에서는 다시 exaone
p50 331.0ms(p95 1,485.1ms) · solar 466.0ms(**p95 1,035.0ms — p95는 오히려 solar가
가장 안정적**) · ax 615.0ms(p95 2,000.55ms)로, p50 순위와 p95(꼬리) 순위가 서로 다른
모델을 가리켰다. 이는 "국내 3사가 항상 417~746ms대"라는 단일 밴드 서술이 표본에 따라
정밀도가 달라질 수 있음을 보여준다 — 지연 수치를 인용할 때는 어느 n에서 잰 것인지를
반드시 함께 밝혀야 한다.

### 4.7 Arena P6a는 나머지 다섯 벤치마크와 정면으로 모순된다

2장 A0에서 정리한 다섯 조립 벤치마크 중 넷(A1~A4, A6, A7)은 조립이 프론티어와 동률이거나
우세하다고 결론짓지만, **13개 태스크·n=2,899로 가장 크고 가장 최신인 Arena P6a는 정반대로
조립이 16개 시스템 중 13~14위**라고 말한다(65.45%/64.95%, ARENA §3 L176-177). 특히
`contract`와 `decompose` 두 태스크에서는 **프론티어 6종 전부에게 6/6으로 완패**한다(ARENA
§4.1 L262-265). Arena 문서 스스로 이 모순을 인정하고, "이 결과로 ASSIGNMENTS를 뒤집지
않는다"고 명시적으로 선을 긋는다(I2, I4) — 이유는 (a) 13개 태스크 중 6개만 나머지
벤치마크와 겹치고 나머지 7개는 다른 채점 체계이며, (b) Arena의 채점 자체가 프로덕션
파서보다 관대해서(4.3과 같은 종류의 파서 이슈) production-parity로 재점수를 매기자
18개 셀이 최대 95.0pp까지 이동했기 때문이다(ASP §8.1-§8.2, I3). 이 모순을 감추지 않고
그대로 남겨 두는 것 자체가 이 실험군의 태도를 보여준다.

### 4.8 §15의 "동점 전제"조차 일부는 사후 검증에서 무너졌다

4.1과 같은 계열의 발견이지만 별도로 짚을 가치가 있다 — §15가 애초에 "동점 구간"으로
분류해 다양화를 정당화했던 두 슬롯(routing, wiki_qa)이, 2026-07-28 별도 재감사에서
사실은 처음부터 유의한 차이(routing p=5.75e-25, wiki_qa p=1.2e-10)였던 것으로
드러났다(MO-kp §15 각주). 즉 "동점이라고 믿고 임의로 고른 선택"이 결과적으로는 옳았지만,
"그 근거였던 동점 판정 자체가 틀렸다"는 점은 별개로 기록해 둘 필요가 있다 — 결론(EXAONE
채택)은 우연히 맞았지만 그걸 지지했던 통계적 근거는 사후에 반박됐다.

### 4.9 사전선언·자기감사 절차가 두 개의 자체 초기 결론을 스스로 철회시켰다

이 실험군 전체의 방법론적 특징은 결과가 나온 다음에도 계속 재검증했다는 점이다. "작업별
분할 배정이 +7.7%p 우위"라는 최초 결론(1장에서 다룬 그 결론)과 "학습 선택기가 규칙표보다
우세하다"는 두 개의 초기 결론이 모두 사전선언·블라인드 저작·불변식 게이트 절차 안에서
스스로 기각됐다(3.3.2 INS L289-290급 서술과 동일 계열; PDD 부록D). 그 외에도 "decompose는
gpt-4o-mini가 국내 3모델을 전부 이긴다"(n=16, 93.8% vs 81.2%)는 판단이 DF 시리즈의 n=160
재측정(p=0.80)에서 사라졌고(MO-kp §11.3b [^df]), "구조화 질의 골든 +16%p"는 홀드아웃에서
+2.4%p로 줄었다(MO-kp §11.3b). 이런 항목들은 do-not-cite 목록에 명시적으로 등재돼 있다
(PDD 부록D L794-807, I27, I28) — 즉 이 실험군은 결론을 내는 절차만큼 결론을 철회하는
절차도 문서화해 두었다.

---

## 5. 최종 배정표 (승인 가정)

아래 표는 이번(2026-07-29~30) 대규모 재측정의 결과이며, **owner 승인을 가정한 권고안**이다.
`orthus/models/orchestration.py::ASSIGNMENTS`는 이 핸드오프 시점까지 이 표와 일치하도록
변경되지 않았다 — 현재 코드는 여전히 §15(2026-07-20)의 배정을 담고 있다(MO-kp §15).

| 슬롯 | 현행 | → | 최종(승인 가정) | 근거 지표 | 최종 점수 | 차순위 | p (Holm) | n | 확신도 |
|---|---|---|---|---|---|---|---|---|---|
| t3 structured | Solar | 유지 | **Solar** | L1 exact-match | 330/343=96.21% | EXAONE 284/343=82.80% | p=7.672e-10→Holm 1.534e-09 생존 | 343 | 확정 |
| t5 routing | EXAONE | 변경 | **Solar** | L1 exact-match | 494/569=86.82% | EXAONE 486/569=85.41% (현행) | p=0.02148→Holm 0.06445 미생존 | 569 | 약함(명목만) |
| t6 intent | Solar | 유지 | **Solar** | L1 exact-match | 102/139=73.38% | EXAONE·A.X 동일 102/139 | p=1.000 (불일치 0) | 139 | 동점 |
| t7 decompose | EXAONE | 유지 | **EXAONE** | L1 exact-match | 154/290=53.10% | Solar 동일 154/290 | p=1.000 (불일치 5v5) | 290 | 동점(현행 공동1위) |
| t9 graph_bind | A.X | 변경 | **Solar** | L1 exact-match | 232/232=100.00% | A.X 223/232=96.12%(현행) | p=0.003906→Holm 0.01172 생존 | 232 | 확정 |
| t10 delegation_extract | EXAONE | 유지 | **EXAONE** | 정확도+오탐안전성 | 154/177=87.01%, 오탐 in-sample 0/22·함정 2/24 | A.X 138/177=77.97% | p=0.005223→Holm 0.005223 생존 | 177(+22/24) | 확정(안전지표) |
| wiki_qa | Solar | 변경 | **EXAONE** | 쌍대판정 승률(판정 Sonnet4.6) | EXAONE승 570-130-300무=81.4% | Solar 패배 | A.X-vs-Solar p=7.27e-64→Holm 3.63e-63 생존 | 1,000쌍 | 조건부(판정자의존) |
| synthesize | Solar | 변경 | **EXAONE** | 쌍대판정 승률 | EXAONE승 333-131-283무=71.8% | Solar 패배 | A.X-vs-Solar p=3.82e-18→Holm 7.65e-18 생존 | 747쌍 | 조건부(판정자의존) |
| email_draft | Solar | 변경 | **EXAONE** | 결정론 성공률 | 893/1000=89.3% | Solar 759/1000=75.9%(현행) | p=2.16e-18→Holm 2.8e-17 생존 | 1,000 | 확정 |
| gap_suggest | Solar | 유지 | **Solar** | 결정론 성공률 | 980/1000=98.0% | EXAONE 924/1000=92.4% | p=3.10e-09→Holm 1.55e-08 생존 | 1,000 | 확정 |
| claim_headline | Solar | 변경 | **A.X** | 결정론 성공률 | 993/1000=99.3% | Solar 921/1000=92.1%(현행) | p=4.54e-17→Holm 7.27e-16 생존 | 1,000 | 확정(지연대가 A.X p50 1,452ms) |
| distill | Solar | 유지 | **Solar** | 클레임/문서@정밀도 | 8.4클레임/문서@정밀도100%@9.3초 | gpt-4o-mini 참고 7.2클레임 | 대표본 검정 없음 | 25문서(T14) | 미측정 |
| followup_rewrite | Solar | 유지 | **Solar** | — | — | — | — | 0 | 미측정(관례상속) |

**요약**: 6개 변경 / 5개 유지 / 2개 동점. 벤더 구성은 현행 **Solar 9 · EXAONE 3 · A.X 1**에서
최종안 **Solar 7 · EXAONE 5 · A.X 1**로 바뀐다(RFH §2.1 O6 L119-123이 현행 구성의 근거).

**표 안 인용 관련 주의(7장에서 다시 다룸)**: wiki_qa·synthesize 두 행의 "최종 점수"와
"차순위"는 solar×EXAONE 쌍의 승/패/무 값을 쓰고 있지만, 같은 행의 "p (Holm)" 값은
solar×A.X 쌍의 검정 결과를 인용하고 있다 — 즉 한 행 안에 서로 다른 두 쌍대비교의 숫자가
섞여 있다. 결론(둘 다 유의)에는 영향이 없지만(solar×EXAONE 쌍 자체의 정확한 Holm p는
wiki_qa 9.24e-66, synthesize 7.19e-21이며 인용된 값보다 더 극단적이다), 이 표를 그대로
보고서에 옮기기 전에 어느 쪽 p값을 표기할지는 결정해야 한다.

---

## 6. 정량적 우수성에 바로 쓸 수 있는 수치 모음

### 6.1 어셈블리 총점

- 6작업·n=1,750(E2E Phase 8, 2026-07-22) 공통 채점 세트에서 §15 다양화 조립 1449/1750=**82.8%**
  대 최선-슬롯별-배정 1466/1750=**83.8%**(원자료 0.8377)(E2E §8.5). 단일모델 대비: Solar
  1420/1750=**81.14%**, EXAONE 1406/1750=**80.34%**, A.X 1333/1750=**76.17%**, 현행
  gpt-4o-mini 1348/1750=**77.03%**(E2E §8.3).
- 조립 대 Claude Sonnet 4.6 p=0.2664(동률), 대 DeepSeek p=1.000(동률), 대 GLM-5.2
  p=1.000(동률), 대 gpt-5.3 p=0.9111(동률) — **조립이 유의하게 능가한 arm 0종**(E2E §8.5
  L945-948).
- 조립 대 Solar 단일 p=0.0019로 **조립이 유의 우위 +1.66%p**(E2E §8.5 L949; SUM1750
  `composite_ahead_pp: 1.66`).
- n=145(B1L2)에서 국내 조립 133/145=**91.7%**, gpt-5.3 132/145=91.0%(p=1.000 동률),
  gpt-4o 125/145=86.2%(p=0.021, 조립 유의 우위), EXAONE 단일 131/145=90.3%(B1L2 L63-69).
- n=200(D9)에서 조립 183/200=**91.5%**, gpt-5.3 183/200=91.5%(p=1.000), Solar 단일
  175/200=87.5%(p=0.008, 조립 유의 우위)(D9 L9-15).
- ※ Arena P6a(n=2,899, 13작업)만은 정반대다 — 조립이 16개 시스템 중 **13~14위**,
  64.95~65.45%로 6개 프론티어 전부와 Solar·A.X 단일에도 패배한다(ARENA §3 L176-177,
  §4 L251). 4.7 참조.

### 6.2 프론티어 대비

- 벤치마크별 조립 순위: E2E n=1,750에서 프론티어 top-4와 동률(유의 열세 arm 0)(E2E §8.5
  L953-954); n=324에서 10개 중 3위(V2)/6위(V1)(E2E §7.3 L648-660); B1L2 n=145에서
  7개 중 1위(B1L2 L118-121); D9 n=200(9프론티어 기준)에서 수치상 2위(Opus 4.8이
  187/200=93.5%로 앞섬)이나 통계적 동률("유의 우세 arm 0")(PDD §7f L692-694); Flow
  D10 n=168에서 1위, 2위 대비 +12항목(D10 L7-9); F4a 생성 아레나(2,160표)에서 1위
  78.3점(2위 Opus 4.8 70.4점)(F4 L72-77); **Arena P6a n=2,899에서만 13~14위**(ARENA
  §3 L176-177); M7 자율 루프 n=20에서는 조립 arm 자체가 없고 국내 단일 최고가
  14/20=70%, 프론티어는 20/20=100%(M7 L11-14).
- 외부 공개 벤치마크 교차검증(조립이 아닌 국내 단일 모델 대 프론티어 단일 모델):
  WorkBench-KO(200항목/도메인)에서 claude-opus-4-6 194/200=**97.0%** · sonnet-4.6
  190/200=**95.0%** · exaone 86/200=**43.0%** · solar 70/200=**35.0%**(WB L30-32,
  L113-116); BFCL v3 멀티턴(200항목/카테고리)에서 Opus 4.6 **75.5%** > Sonnet 4.6
  **73.0%** > EXAONE **49.5%** > Solar Pro **39.0%**(BFCL L13-16).

### 6.3 슬롯별 표

13개 슬롯 중 n=1,750으로 잰 6개(t3/t5/t6/t7/t9/t10)의 Solar/EXAONE/A.X 정확도와 최고
프론티어는 5장 표를 참조. n=1,000/747로 잰 5개(email_draft/gap_suggest/claim_headline/
wiki_qa/synthesize)의 결정론 성공률·판정 승률도 5장 표에 정리돼 있다. distill은 25문서
기준 Solar 클레임/문서 **4.8개**·정밀도 40/40·**7.3초**/문서, EXAONE 39/40·6.5개·28.3초,
**A.X 3/5 실패**·3.0개·**165초**/문서, gpt-4o-mini 40/40·7.1개·13.0초(MO-kp §12.3
L836-839). 프롬프트 상한 해제(T14) 이후 재측정한 Solar는 **8.4클레임/문서·정밀도
100%·9.3초**로, cap이 걸려 있던 이전 측정(4.8클레임)보다 실제로는 더 많은 정보를 더
정확하게 뽑아낸다(MO-kp §13 L940-943).

### 6.4 지연/처리량

- n=1,750(RAW1750, 미발표 원자료) scored-common p50: **exaone 331.0ms**(p95 1,485.1ms) ·
  **solar 466.0ms**(p95 **1,035.0ms**) · ax 615.0ms(p95 2,000.55ms) · gpt-4o 727.5ms ·
  baseline(gpt-4o-mini) 767.5ms · deepseek 898.0ms · gpt-5.3 1,688.5ms · claude-sonnet-4-6
  1,928.0ms · deepseek-v4-pro 2,585.0ms · **glm-5.2 3,229.0ms**(p95 **13,115.1ms**).
- n=324(E2E §7.4, 발표됨) p50: EXAONE **295ms** · A.X 663ms · Solar 681ms · baseline
  780ms · GPT-4o 853ms · gpt-5.3 1,855ms · Claude Sonnet 4.6 1,983ms · GLM-5.2 **3,591ms**
  (p95 18,319ms).
- 헤드라인 수치: "**3.2~4.6배** 빠름(국내 417~746ms vs 프론티어 1,612~2,061ms, 동일 문항
  실측)"(PDD §7f L696-697). Opus 4.8 p50 1,612ms(조립 대비 ~2.7배)(INS L245-246).
- MO 홀드아웃 지연: Solar **698ms** vs 현행 gpt-4o-mini **~1,100ms**; A.X 876ms; EXAONE
  722ms(MO-kp §11.3 L682).
- A.X 처리량 제약: **팀당 초당 3회(RPS 3)** 하드캡, throttle `min_interval 0.4s`, 429는
  즉시 국내 폴백(MO-kp L327, L368, L742; `CODE` L180).
- 임베딩 질의 p50: OpenAI **241ms**(p99 706ms) → Solar **69ms**(p99 89ms) — **−71% 단축,
  꼬리 8배 개선**(EMB §6.1 L317-323).
- ※ 조립 전체의 end-to-end 파이프라인 지연은 어느 문서에도 계산돼 있지 않다 — 슬롯별
  p50/p95 밴드만 존재한다(E2E §7.4 L737-739).

### 6.5 원가

- 질문당 원가(E5): baseline gpt-4o-mini **0.319원** · single solar **0.328원(1.03배)** ·
  single exaone† **2.59원(8.13배)** · single ax **미공시**(E5 §3 L47-50).
- 헤드라인: "**17~20배 저렴**(Solar 0.33원/질문 vs gpt-4o ~5.6원 · Sonnet ~6.6원)"(PDD §7f
  L699; PROMO L36-45). 단가: solar-pro $0.15/$0.60, gpt-4o $2.50/$10.00, Sonnet 4.6
  $3.00/$15.00(per 1M tok)(PROMO L40-42).
- ※ 위 17~20배는 **프론티어(gpt-4o/Sonnet) 대비**이며, **현행 경량 baseline(gpt-4o-mini)
  대비로는 1.03배로 사실상 동일**하다(E5 §3 L48) — "현행 대비 절감"으로 주장하지 않는다.
- 임베딩 원가: 전량 재임베딩(33,570청크) OpenAI $0.052 vs Solar **$0.287(5.5배)**; `/ask`
  100만 콜 기준 OpenAI $0.224 vs Solar **$4.136(18.5배)**; 단, 연 환산으로는 **"연
  $4"**로 절대액이 작아 "원가는 이 결정의 변수가 아니다"(EMB §7.4 L408-411).
- 실측 지출(예산 상한 대비): REM 재측정 33,229콜 = **$77.31**(사전 견적 $58.41 대비
  1.32배)(COST L3, L22). Arena P6a 16시스템 러닝 코스트 **약 $92.39**(ARENA §13 L520).
- 사내 질의 100만 건 기준: 해외 프론티어 **560만원** vs 국내 조립 **33만원**(AGENTS.md
  3.3.1 서술과 동일 계열 수치, PROMO L36-45 기반).

### 6.6 안전성(delegation false-positive)

- **적대적 함정 홀드아웃**(`t10_holdout2.json`, 함정 24 + 실위임 8 = 32항목): EXAONE(현행
  배정) 함정 오탐 **2/24(8%)**, 프리필터 적용 후 **1건**, 미탐 0건, 전체 정확도
  30/32(94%). Solar 6/24(25%), gpt-4o-mini(현행 baseline) **7/24(29%)**, A.X
  **11/24(46%)**(PSV §2; RFH §2.2). **실위임 미탐은 두 골든 전체에서 0/18**로
  회귀 고정돼 있다(RFH §2.2 L152).
- EXAONE의 함정 2건은 실제 팀원 이름을 assignee로 뽑은 것이었다 — 회의록 액션아이템 자기
  배정("h-09"→최수민) 및 자기 계획 오독("h-15"→오세훈)(PSV §2 table).
- 자기일관성 k=3 다수결로 오탐이 7건에서 4건으로(**−43%**) 줄었고 미탐 0건은 유지됐다
  (INS L219; PDD L840).
- ⚠️ 위 결과는 **in-sample 경고가 붙어 있다** — `t10_delegation.json`은 EXAONE을 그
  슬롯에 뽑은 바로 그 데이터다. 독립 검증은 적대적 홀드아웃(위 문단)이며, 거기서도
  EXAONE이 최저 오탐이라는 서열은 유지된다(PSV §2 L47-49).
- 더 큰 표본(정상 110 · 함정 120, b3-results.md): auto-dispatch 정책 게이트 하에서
  false-auto-dispatch **0/120**; 결정론 프리필터는 이 골든에서 함정을 **0건** 잡았다
  (b3-results L12, L190) — "안전을 완전자동화와 맞바꾼다"는 트레이드오프가 명시돼 있다.

### 6.7 통계적 엄격성

- 사용 검정: McNemar exact(양측), 부트스트랩 CI95(resample 10,000), Fisher exact,
  Wilcoxon signed-rank, Mann-Whitney, Holm 단계적 하강, BH FDR, Cohen's kappa(E2E §7;
  REM §4; ARENA §4; H2H L9; EMB §8.5).
- E2E n=1,750: 55쌍 중 **43건 유의(명목 p<0.05, 다중비교 미보정)**(E2E §8.3 L899).
- REM 결정론 3종: 3작업 × 21쌍 = 63쌍, **Holm 보정값을 원값과 나란히 병기**(REM §1;
  §4 L455).
- REM judge: wiki_qa 17쌍 + synthesize 17쌍, judge family 내 Holm 적용(REM §2).
- Arena Holm family: task-internal m=12(ARENA §4 header). Arena H3: **18쌍 중 14건 유의
  — "국내 3모델 동률"을 자체적으로 기각**하지만, "이걸로 ASSIGNMENTS를 뒤집지 않는다"고
  명시(ARENA §8 L364-381, I4).
- 판정자 검증(B4): judge-human kappa **0.4570** > human-human 상한 **0.4039**(B4
  L10-20). 판정 파일럿(JP): gpt-4o vs Sonnet 4.6 일치율 **70.0%(63/90)**, kappa
  **0.535**(JP L22-23, L34-35).
- **Holm/kappa가 `docs/model-orchestration.md` §11-§15 어느 사본에도 등장하지 않는다**(F표
  마지막 행) — 이번 REM/E2E 재측정에서 처음 도입된 절차다.

---

## 7. 이 수치들의 한계 (반드시 함께 전달해야 할 것)

이 절은 보고서를 비판하려는 것이 아니라, 어떤 정직한 정량적 주장이든 함께 들고 가야 하는
조건들이다. 숫자만 옮기고 이 조건을 떼어내면 그 숫자는 원래 의미를 잃는다.

1. **조립은 통합 실행이 검증된 적이 없다.** 슬롯별 배정 모델을 실제로 한 요청 안에서
   라우팅해 돌리는 "통합 실행"이 아니라, 슬롯별 개별 측정을 사후에 합산한 **가상 점수**다
   (E2E §7.0 L598-601; §8.7 L1012). 슬롯 간 fallback·retry·컨텍스트 상호작용은
   반영되지 않는다.
2. **Arena P6a가 나머지 넷과 정면 모순된다**(4.7 참조). 이 모순은 벤치마크 구성(13작업 vs
   6작업, n=2,899 vs n≤1,750)과 채점 관대함의 차이(4.3, I3) 둘 다에서 기인하며, 어느
   쪽이 "맞다"고 단정할 근거는 없다.
3. **명목 p와 Holm 보정 p는 구분해야 한다.** E2E n=1,750의 43/55 유의는 다중비교 보정이
   없는 값이다. REM은 Holm을 정본으로 삼고, Arena는 m=12로, H2H는 BH FDR로 각각 다른
   보정을 적용한다 — 셋은 서로 바꿔 읽을 수 없다(I13).
4. **판정자 kappa가 낮다.** wiki_qa/synthesize의 국내 보조 판정자-Sonnet kappa는
   0.033~0.240으로 상당수가 우연 수준이다. 이 두 슬롯의 배정 권고는 사실상 "**단일
   판정자**(Sonnet) + 결정론 부가지표"에 의존한다(I9, 4.4).
5. **5장 표 안의 인용 불일치.** wiki_qa·synthesize 행에서 최종 점수/차순위는 solar×EXAONE
   쌍의 값이고 p(Holm)은 solar×A.X 쌍의 값이다 — 결론에는 영향이 없지만(정확한
   solar×EXAONE Holm p는 wiki_qa 9.24e-66, synthesize 7.19e-21로 더 극단적이다) 표기
   자체는 정정이 필요하다.
6. **표본 비대칭(REM tier-B).** solar×claude-opus-4.8만 전량(wiki_qa 1,000/synthesize
   747)이고 나머지 3개 프론티어 스포크는 250건 서브샘플이다. 또한 tier-B에는 설계상
   2인이어야 할 판정자가 3인(sonnet·ax·exaone) 붙어 판정단위가 늘었다 — "그만큼 CI가
   좁아진 것이지 워커가 더 잘한 게 아니다"(REM L152, L457-458, I15). **프론티어끼리는
   직접 비교되지 않았다** — solar를 경유한 이행적 읽기만 허용된다.
7. **소표본 뒤집힘 사례가 실측으로 확인됐다.** email_draft는 n=30에서 "Solar 5/30 vs
   EXAONE 4/30, p=1.000(완전 동점)"이었지만 n=1,000에서는 "EXAONE이 Holm p=2.8e-17로
   유의 우세"였다(I20). "그때 반대로 골랐어도 우리는 몰랐을 것"이라는 문구가 REM
   §0-1에 그대로 남아 있다 — 이는 5장 표의 나머지 항목에도 동일한 위험이 잠재함을
   시사한다(아직 n=1,000/1,750으로 검증되지 않은 슬롯일수록 더 그렇다).
8. **과거 t2/t8 수치와 이 리포트의 수치는 같은 표에 놓을 수 없다.** 판정자가
   gpt-4o에서 Claude Sonnet 4.6으로 바뀌었고, 옛 `t2.json`은 반말/새 세트는 존댓말이라는
   차이도 있다(REM §0-2 L13; I17).
9. **A.X의 결격 사유(decompose 실패율, RPS 3 등)는 4.3의 파서 완화로 사라지지 않는다.**
   파서 완화가 되돌린 것은 "JSON 형식 위반으로 인한 손실"이지, decompose 복합질문 실패율
   56.2%나 RPS 3 하드캡, tool-call 무시 같은 구조적 한계가 아니다 — 이 둘을 섞어서
   "A.X도 사실 괜찮다"로 일반화하면 안 된다.
10. **원가·지연 수치는 실제 청구액이 아니다.** 벤더 크레딧으로 수행했고("실지출 0원"),
    표는 공시 단가 기준 추정치다. A.X는 토큰 단가를 공시하지 않아 원가 비교에서
    구조적으로 빠진다(E5 §6; I38). gpt-5.3 단가도 미공시라 17~20배 수치 계산에서
    제외됐다.
11. **자율 에이전트 루프는 조립 arm 자체가 존재하지 않는다.** M7(n=20)은 국내 단일
    최고 70%(14/20) vs 프론티어 100%(20/20)이며, 이 루프는 단일모델 end-to-end라서
    "조립"이라는 개념 자체가 적용되지 않는다(I42) — 자율 루프 수치를 조립 성능과
    나란히 놓으면 범주 오류다.
12. **한국어 성적 향상(6작업 언어전환 조립 +2.00%p)의 원인은 확정되지 않았다.** 라우터
    규칙 사전이 한국어 키워드만 갖고 있어 한국어 질의가 결정론 경로로 더 많이 빠지는
    비대칭이 원인일 수 있다는 가설이 있고, 대칭 라우팅 가정 반사실 재평가에서는 상승폭이
    줄거나 부호가 뒤집힌다 — "국내 모델이 한국어를 더 잘한다"로 해석해서는 안 된다.

---

## 8. 미결 사항

다음은 아직 결정되지 않았거나 실행되지 않은 항목이며, 이 핸드오프는 이 중 어느 쪽으로
가야 한다고 권고하지 않는다 — owner/팀 결정을 기다리는 열린 항목으로만 남긴다.

- **`orthus/models/orchestration.py::ASSIGNMENTS`가 5장 표와 일치하도록 변경되지 않았다.**
  현재 main 코드는 §15(2026-07-20) 배정을 그대로 담고 있다. 5장 표를 반영하려면
  코드 변경 + `tests/unit/test_model_orchestration.py`의 회귀 테스트 갱신(현재는
  §15 배정을 고정하는 `test_diversified_assignment_2026_07_20`, `test_ax_holds_
  exactly_the_graph_bind_primary_slot`가 존재)이 필요하다.
  변경을 owner가 승인할지, 몇 개 슬롯만 채택할지는 미결이다.
- **이번 재측정 결과를 다루는 PR이 아직 열리지 않았다.** REM/E2E n=1,750 결과물은
  `.worktrees/remaining-slots`와 `.worktrees/golden-expand`에 흩어져 있고 main에
  merge되지 않았다.
- **`docs/model-orchestration.md`를 갱신할지 여부.** 현재 §11.4/§12.4/§15는 이번
  재측정(REM/E2E n=1,750) 이전 상태를 "SoR"로 표기하고 있다. 이 문서를 갱신해서
  5장 표를 반영할지, 갱신한다면 §15를 대체하는 새 절(§16?)로 추가할지는 미결이다.
- **5장 표 내 wiki_qa/synthesize의 p(Holm) 인용 오류(7장 5번) 수정 여부.** 정정된
  값(wiki_qa 9.24e-66, synthesize 7.19e-21)으로 바꿀지, 원래 인용을 유지하고 각주만
  달지는 미결이다.
- **routing 슬롯의 "약함(명목만)" 확신도를 어떻게 다룰지.** 5장 표에서 routing은
  Holm 미생존(0.06445)으로 표시돼 있다 — 이 슬롯을 EXAONE→Solar로 변경할지, 명목
  유의성만으로는 부족하다고 보고 현행(EXAONE) 유지할지는 통계적으로 결정되지 않은
  채 열려 있다.
- **wiki_qa/synthesize의 "제3 판정자 추가" 또는 "결정론 대리지표 전환" 여부.** REM은
  이 두 슬롯의 결론을 "단일 판정자에 슬롯을 걸 수 없다"며 조건부로만 제시하고, 해소
  조건으로 제3 프론티어 판정자 추가 또는 결정론 대리지표(인용마커 위반율 등) 전환을
  제안했다(REM §3 L451) — 어느 쪽을 택할지, 언제 재측정할지는 미결이다.
- **claim_headline을 A.X로 바꿀 경우의 지연 대가(583ms→1,452ms)를 수용할지.** 정확도
  이득(7.2%p, Holm p=7.3e-16)은 유의하지만 지연이 약 2.5배 늘어난다 — 이 트레이드오프를
  받아들일지는 제품 결정이지 통계적으로 결정되는 사안이 아니다.
