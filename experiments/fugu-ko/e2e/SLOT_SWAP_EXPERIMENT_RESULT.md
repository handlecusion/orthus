# 슬롯 스왑 실험 결과: production `ASSIGNMENTS` 변경 불필요 (negative result)

측정일: 2026-07-21. 결론: **`orthus/models/orchestration.py`의 `ASSIGNMENTS`를
바꿀 근거가 없다.** 새 3-모델 매트릭스 아티팩트와 슬롯별 재배정 통계 검증
둘 다 현재 production 배정을 유의하게 이기지 못했다. 재론이 반복되지 않도록
negative result를 기록한다.

> **2026-07-21 정정 (mid-session baseline 변경):** 이 실험을 처음 돌린 뒤
> PR #4(`feat/fugu-ko-9model-bench`)가 main에 머지되면서 실제
> `orthus/models/orchestration.py`의 `ASSIGNMENTS`가 이 문서가 baseline으로
> 썼던 "all-solar 1차 + `delegation_extract=exaone`"(117/145, 80.69%)에서
> **다변화 테이블**(t3 STRUCTURED=solar, t5 ROUTING=exaone, t6 INTENT=solar,
> t7 DECOMPOSE=exaone, t9 GRAPH_BIND=ax, t10 DELEGATION_EXTRACT=exaone,
> **118/145, 81.38%**)로 바뀌었다. 즉 이 문서가 처음 비교했던 baseline은
> 실험을 계산한 시점과 이 문서를 커밋하는 시점 사이에 사실이 아니게 됐다.
> 아래 §2는 domestic_best를 **실제 현재 production(118/145)** 기준으로
> 재비교한 수치로 정정했다 — 이는 "재론이 반복되지 않게 negative result를
> 기록한다"는 이 문서의 목적에 맞게, 틀린 baseline을 조용히 두기보다
> 투명하게 밝히고 바로잡는 것이 맞다고 판단해서다. 정정해도 결론(§3, "배정
> 변경 근거 없음")은 바뀌지 않는다 — stale/corrected 비교 모두 유의하지
> 않다. 원본 stale 비교와 정정 비교 둘 다
> `experiments/fugu-ko/analysis/orchestration_composite_slot_swap_exp.json`에
> `stale_comparison_vs_pre_pr4_main` / `corrected_comparison_vs_current_main`
> 키로 보존돼 있다.

## 1. 외부 아티팩트(prompt-lab 3-모델 매트릭스) 교차검증

`experiments/prompt-lab/analysis/{matrix-results,matrix-interpretation,
miniwiki-results,miniwiki-interpretation}.md`(아티팩트가 스스로 인용하는
출처 문서 — 본 워크트리에 실물 존재 여부는 미확인, 아티팩트 진술만 인용)가
Solar/A.X/EXAONE 3종을 rewrite/decompose/delegation/wiki_qa/synthesize/
distill 태스크에서 n=287 홀드아웃 + 70-doc miniwiki로 비교(McNemar+Holm,
codex OAuth judge)했다.

| 태스크 | 아티팩트 유의 결과 | 현재 production 배정 | 정합 |
|---|---|---|---|
| rewrite | EXAONE 최악(34.5% 잔여 지시어 결함), Solar 최선(1.7%), A.X 중간(10.3%) — Holm-유의 | `TASK_FOLLOWUP_REWRITE=solar` | ✅ 이미 최적 |
| decompose | A.X 최악(52.6% 오분해), Solar/EXAONE 둘 다 유의하게 우수 | `TASK_DECOMPOSE=exaone`(2026-07-20 PR#4 다변화 — 이전엔 solar) | ✅ A.X 회피 이미 반영 (solar/exaone 둘 다 유의 우수군) |
| delegation | A.X 최악(25.9% 오탐), EXAONE 최선(11.1%) — raw p=.008 | `TASK_DELEGATION_EXTRACT=exaone` | ✅ 이미 최적 |
| wiki_qa/synthesize/distill | 3-모델 간 유의차 없음 | wiki_qa=`exaone`(90% tie), synthesize/distill=`solar`(2026-07-20 PR#4 다변화 — 이전엔 wiki_qa도 solar) | ✅ 유의차 없으니 변경 근거 없음 |

70-doc miniwiki 저작 모델 비교(Solar/A.X/EXAONE)는 위키 밀도를 바꾸지만
(EXAONE 6.7 claims/doc, Solar 5.7, A.X 4.2 + build 실패 12건 + 10배 느림)
**downstream 답변 품질은 바꾸지 않는다**(Holm-유의차 없음) — 원가/지연/
신뢰성 근거로 distill/authoring은 Solar 유지를 권고한다는 아티팩트 결론도
현재 `TASK_DISTILL=solar`와 일치한다.

**결과: 아티팩트가 제시한 유의 결과 3건 전부 현재 production과 이미
정합한다. 정합하지 않는 항목(gap)이 하나도 없었다.**

## 2. Track A 통계 검증 (슬롯별 재배정, 이 워크트리에서 신규 계산)

별도 워크트리(`.worktrees/fugu-ko-9model-bench`, PR #4)의 9-model × 6-slot
E2E 벤치마크가 "슬롯별로 모델을 다시 배정하면(t3 STRUCTURED, t5 ROUTING,
t6 INTENT, t7 DECOMPOSE, t9 GRAPH_BIND, t10 DELEGATION_EXTRACT) 현재
production을 이길 수 있는가?"라는 질문을 남겼다. 이를 `slot_swap_exp.py`로
엄밀 검증했다. **PR #4는 그 벤치마크의 다변화 테이블을 실제
`orthus/models/orchestration.py` production `ASSIGNMENTS`로도 채택해 main에
머지했다** — 그래서 이 실험의 "production baseline"이 실험 도중 바뀌었다
(위 정정 노트 참조). 아래는 그 실제 현재 production 기준 정정 수치다.

### 방법

- **sanity gate 선행**: 새 데이터 로딩 경로가 이미 발표된 다변화-테이블
  composite(118/145, vs-baseline McNemar p=0.5488, CI=[-0.0207, 0.069],
  `analysis/raw/orchestration_composite_9model.json`)를 bit-for-bit
  재현하는지 먼저 확인 — **재현 성공(정확 일치)**. 재현 실패 시 새 숫자는
  신뢰하지 않기로 사전 결정했었다. 이 다변화-테이블 composite는 아래
  `current_production`과 동일한 배정(118/145)이므로, 이 sanity gate가 곧
  정정 비교에 쓰인 current_production 수치의 검증이기도 하다.
- **current production (실제, post-PR#4)**: `t3=solar, t5=exaone, t6=solar,
  t7=exaone, t9=ax, t10=exaone`.
- **domestic best-per-slot**: 국내 3모델(solar/ax/exaone) 중 슬롯별 최고
  정확도, 동률은 (이전) 현재 배정으로 tie-break — `t3=exaone, t5=ax,
  t6=solar, t7=exaone, t9=solar, t10=exaone`. 이 배정 자체는 PR #4 이전에
  계산됐고 PR #4로 바뀌지 않는다.
- 통계량은 `e2e/combine_stats.py` + `e2e/runner_lib.py`
  (`mcnemar_from_correct`, `bootstrap_paired_diff_ci`)를 재구현 없이
  그대로 재사용.

### 결과

| 배정 | passed/n | accuracy |
|---|---|---|
| current production (실제, post-PR#4 다변화 테이블) | 118/145 | 81.38% |
| domestic best-per-slot | 121/145 | 83.45% |

domestic_best vs current_production: **McNemar p=0.375**(유의 아님,
α=0.05), bootstrap paired-diff 95% CI = **[-0.0069, 0.0552]**(0을 포함 —
유의 아님).

절대 pass count는 domestic_best가 3문항 높지만(121 vs 118),
discordant pair 5건(a_only=4, b_only=1) 규모에서 이 정도 차이는
표본 크기(n=145) 대비 통계적으로 유의하지 않다.

#### 슬롯별 실제 차이 (domestic_best vs current production)

domestic_best와 current production은 6개 슬롯 중 **3개만** 다르다
(t6/t7/t10은 두 배정에서 이미 동일):

| 슬롯 | domestic_best | current production | 비고 |
| --- | --- | --- | --- |
| t3 STRUCTURED | exaone (15/28) | solar (13/28) | solar→exaone이면 +2 |
| t5 ROUTING | ax (19/21) | exaone (18/21) | exaone→ax이면 +1 |
| t9 GRAPH_BIND | solar (32/32) | ax (32/32) | 둘 다 32/32 만점(동률, 차이 0) |
| t6 INTENT | solar (19/20) | solar (19/20) | 동일 — 차이 없음 |
| t7 DECOMPOSE | exaone (15/22) | exaone (15/22) | 동일 — 차이 없음 |
| t10 DELEGATION_EXTRACT | exaone (21/22) | exaone (21/22) | 동일 — 차이 없음 |

전체 pass 차이(121−118=3)는 t3(+2)와 t5(+1)에서만 나오고, t9는 만점
동률이라 실질 차이가 0이다. 즉 domestic_best가 이길 수 있는 순수 후보는
사실상 t3/t5 2개 슬롯뿐이며, 그 규모(discordant 5건)로도 유의성에 못
미친다.

## 3. 결론 및 권고

- **`orthus/models/orchestration.py`의 `ASSIGNMENTS`를 변경하지 않는다.**
  이번 PR은 `orthus/` production 코드를 건드리지 않는다.
- 아티팩트의 3개 유의 결과와 슬롯 스왑 재배정 둘 다 현재 배정을 유의하게
  이기지 못했다 — "옮기면 좋아진다"는 근거가 없다. (원래 계산 당시의
  stale baseline 기준으로도, PR #4 머지 이후의 실제 baseline 기준으로도
  같은 결론이다 — §2 정정 참조.)
- 현재 production 배정(t3/t6/t9/t7/t10 등 슬롯별 다변화 테이블,
  `delegation_extract=exaone` 포함)을 유지하는 것이 근거 기반 결론이다.
  `delegation_extract=exaone`은 정확도가 아니라 안전(오탐 최소화) 근거로
  유지된 것이며 이번 검증도 이를 반박하지 않는다.
- 이 negative result를 기록해 두는 목적은 새 데이터 없이 같은 질문이
  재론되는 것을 막기 위함이다. 배정을 다시 검토하려면 이 문서보다 큰
  표본이나 다른 태스크 분포의 신규 측정이 필요하다.

## 4. 데이터/재현 포인터

- `experiments/fugu-ko/analysis/slot_swap_exp.py` — 이 실험의 실행 스크립트
  (sanity gate + true_baseline/domestic_best composite + McNemar/bootstrap CI 계산, 결과를 JSON으로 저장).
- `experiments/fugu-ko/analysis/orchestration_composite_slot_swap_exp.json` — 위 스크립트의 원본 출력(전체 수치, per-task breakdown 포함).
- `experiments/prompt-lab/analysis/matrix-results.md`,
  `experiments/prompt-lab/analysis/matrix-interpretation.md`,
  `experiments/prompt-lab/analysis/miniwiki-results.md`,
  `experiments/prompt-lab/analysis/miniwiki-interpretation.md` — §1 외부
  아티팩트가 스스로 인용하는 출처 문서(아티팩트 진술 기준 인용, 이 워크트리
  기준 실재 여부 별도 확인 안 함).
- 재현에 필요한 harness 모듈(`e2e/combine_stats.py`, `e2e/runner_lib.py`,
  `harness_e2e.py`, `e2e/tier_a.jsonl`)과 원본 raw jsonl(`analysis/raw/`)은
  이미 `main`에 9-model E2E 벤치마크 PR(#4 계열)로 존재하므로 본 PR에는
  포함하지 않았다.
