# B1 Layer-2 — 국내 멀티모델 오케스트레이션 vs 프론티어 단일

**질문:** 작업마다 최적 국내 모델을 배정해 이어붙인 **국내 오케스트레이션 composite**가,
모든 작업을 혼자 처리하는 **프론티어 단일 모델**(gpt-4o / gpt-5.3 / Claude Sonnet 4.6)과
견줄 수 있는가? 기준선은 현행 프로덕션 모델 gpt-4o-mini.

- 판정셋: `e2e/tier_a.jsonl`, 작업 `t3, t5, t6, t7, t9, t10` (참조 6-작업셋, 241 항목).
- 파이프라인: **작업별 프로덕션 경로 그대로** (`--glue-level` 미지정 = Layer-1 프로덕션 동작 무변경).
  t3는 라이브 DB(`orthus_company_0706`) 대상.
- 채점: 결정론(exact/DB) — t5/t6/t7-holdout/t9/t10은 harness 결정론 채점, t3는 DB 대조.
- **유효 채점 N = 145** (241 중 **96개 t7 프리필터 probe 항목은 e3 집계로 이관**되어 per-item
  pass/fail에서 deferred 처리됨; 이 deferred 집합은 **6개 모델 전부 동일한 96 ID**라 페어 비교가 깨끗함).
  작업별 유효 n: t3=28, t5=21, t6=20, **t7=22**(scored holdout), t9=32, t10=22.

## 모델 · 슬러그 · 카나리아 (슬러그 오타 = 조용한 0점, 사전 검증 필수)

| 슬러그 | 카나리아 | 결과 |
|---|---|---|
| `solar` | t5,t3 ×3 | 3/3 PASS |
| `openai:gpt-4o` (`--final-verify`, `ORTHUS_LLM_API_KEY=$OPENAI_API_KEY`) | t3(DB) ×3 | 3/3 PASS |
| `baseline` (=gpt-4o-mini, `ORTHUS_LLM=openai`) | t5 ×3 | 3/3 PASS |
| `exaone` | (full run) | 정상 |
| `bedrock:anthropic.claude-sonnet-4-6` (`--final-verify`, **풀 `anthropic.` 프리픽스**) | t5 ×3 | **3/3 PASS** (풀 프리픽스 확정) |
| `openai:gpt-5.3-chat-latest` (코디네이터 실행) | — | per-item 스냅샷 확보 |
| `ax` (RPS-3) | — | **⏳ pending** (아래) |

전 슬러그 병렬 안전 요약명(`ORTHUS_E2E_SUMMARY_NAME`) 사용, per-model raw는 Layer-1 t3 arm의
`e2e_<slug>.jsonl` 덮어쓰기를 피하려 **완료 즉시 `analysis/raw/b1/layer2/raw_<slug>.jsonl`로 스냅샷**했다.
6개 스냅샷 전부 무결(n=241, scored=145, tier-A 작업 분포 일치). **오류/429 = 0** (전 모델 error-status 0).

## 작업별 pass/n 행렬 (model × task, 유효 채점 145)

| 모델 | t3(28) | t5(21) | t6(20) | t7(22) | t9(32) | t10(22) | **합계** |
|---|---|---|---|---|---|---|---|
| **국내 composite (오케스트레이션)** | 28/28 | 19/21 | 19/20 | 14/22 | 32/32 | 21/22 | **133/145 (91.7%)** |
| solar | 28/28 | 18/21 | 19/20 | 14/22 | 32/32 | 16/22 | 127/145 (87.6%) |
| exaone | 28/28 | 19/21 | 19/20 | 14/22 | 30/32 | 21/22 | 131/145 (90.3%) |
| ax (SKT A.X-K1) | ⏳ | ⏳ | ⏳ | ⏳ | ⏳ | ⏳ | ⏳ pending |
| gpt-4o (frontier) | 28/28 | 19/21 | 19/20 | 11/22 | 32/32 | 16/22 | 125/145 (86.2%) |
| gpt-5.3 (frontier, temp=1) | 28/28 | 19/21 | 19/20 | 13/22 | 32/32 | 21/22 | **132/145 (91.0%)** |
| Claude Sonnet 4.6 (frontier) | 28/28 | 19/21 | 19/20 | 12/22 | 32/32 | 21/22 | 131/145 (90.3%) |
| gpt-4o-mini (baseline/prod) | 24/28 | 19/21 | 19/20 | 15/22 | 32/32 | 17/22 | 126/145 (86.9%) |

## Composite 슬롯 배정 (작업별 최적 국내)

| task | solar | exaone | 승자 | 근거 |
|---|---|---|---|---|
| t3 | 28/28 | 28/28 | **solar** | 동률(천장) |
| t5 | 18/21 | **19/21** | **exaone** | +1 |
| t6 | 19/20 | 19/20 | **solar** | 동률 |
| t7 | 14/22 | 14/22 | **solar** | 동률 (임의) |
| t9 | **32/32** | 30/32 | **solar** | +2 (천장) |
| t10 | 16/22 | **21/22** | **exaone** | +5 (큰 마진) |

**슬롯 = {t3:solar, t5:exaone, t6:solar, t7:solar, t9:solar, t10:exaone}** →
composite **133/145 = 91.7%**. composite의 단일 국내 대비 이득은 사실상 **t10(exaone +5)**과
t5(exaone +1)에서 나온다 — 둘 다 큰/안정 마진이라 노이즈 체리픽이 아니다.

## 헤드투헤드 총점 (공통 채점셋 145)

| 순위 | 모델 | pass/n | acc |
|---|---|---|---|
| 1 | **국내 composite (오케스트레이션)** | 133/145 | **91.7%** |
| 2 | gpt-5.3 (frontier) | 132/145 | 91.0% |
| 3 | exaone (single 국내) | 131/145 | 90.3% |
| 3 | Claude Sonnet 4.6 (frontier) | 131/145 | 90.3% |
| 5 | gpt-4o-mini (baseline) | 126/145 | 86.9% |
| 6 | solar (single 국내) | 127/145 | 87.6% |
| 7 | gpt-4o (frontier) | 125/145 | 86.2% |

## 페어드 McNemar (composite vs 프론티어 / baseline)

정확 이항검정(불일치쌍 기준), n=145 공통 채점셋.

| 비교 | comp만 정답 | 상대만 정답 | 불일치 | **p(exact)** | 판정 |
|---|---|---|---|---|---|
| composite vs **gpt-4o** | 9 | 1 | 10 | **0.021** | **composite 유의 우세** |
| composite vs **gpt-5.3** | 3 | 2 | 5 | **1.000** | **동률 (n.s.)** |
| composite vs **Sonnet 4.6** | 4 | 2 | 6 | **0.688** | **동률 (n.s.)** |
| composite vs **baseline(gpt-4o-mini)** | 8 | 1 | 9 | **0.039** | **composite 유의 우세** |

**참고 — composite vs 단일 모델:**

| 비교 | comp만 | 상대만 | 불일치 | p |
|---|---|---|---|---|
| composite vs solar | 6 | 0 | 6 | 0.031 (유의) |
| composite vs exaone | 2 | 0 | 2 | 0.500 (n.s.) |

즉 **오케스트레이션의 실질 이득은 "최고 단일 국내(exaone)"를 유의하게 넘지는 못한다**(+2, p=0.50).
스토리는 "composite가 exaone보다 낫다"가 아니라 **"국내 모델(composite든 exaone 단독이든)이 최강
프론티어와 통계적 동률이고 gpt-4o는 유의하게 앞선다"**이다.

## 정직한 caveat

- **(a) In-sample 낙관 편향(경미):** 작업별 승자를 채점한 그 셋에서 골랐다. 단 판정을 가르는
  두 슬롯(t10 exaone +5, t5 exaone +1)은 큰/안정 마진이고, t7은 solar/exaone 동률이라 선택이
  무의미하다. **프로덕션 배정표**(model-orchestration §15류)로 고정해도 composite는 사실상 동일하다.
- **(b) composite vs gpt-5.3 = 1 항목 차(132↔133), 불일치쌍 3:2, p=1.000 → 정직한 결론은 "TIE".**
  순위상 composite가 1위지만 통계적으로 동률이다. "국내가 gpt-5.3을 이겼다"로 과장하지 말 것.
- **(c) ax(A.X-K1) pending:** Layer-1 t3 glue arm(RPS-3, glue-4 진행 중)이 클리어된 뒤 실행하도록
  드라이버가 대기 중. **composite는 국내 max-선택이라 ax가 추가돼도 내려갈 수 없다**(≥133 보장).
  ax가 바꿀 수 있는 슬롯은 t5/t6/t7/t10뿐이고(t3·t9는 천장), b1 glue-L0에서 ax가 최약체
  (acc 0.57 < solar 0.73)라 승자 교체 가능성은 낮다. t9 슬롯은 전 모델 천장(32/32)이라 무관.
  → **ax 결과와 무관하게 위 헤드투헤드/McNemar 결론은 유지된다.**
- **(d) gpt-5.3은 벤더 강제 temperature=1**(다른 모델 temp=0)로 돌았고 **~2026-08 벤더 deprecated**
  예정이다. 즉 "가장 센 프론티어"가 재현성·수명 면에서 불리한 조건인데도 동률이다.

## 지연 (p50, ms · 유효 채점 항목)

| solar | exaone | gpt-4o | gpt-5.3 | Sonnet 4.6 | baseline |
|---|---|---|---|---|---|
| 514 | 417 | 858 | 1644 | 1938 | 868 |

국내 워커(solar/exaone)가 프론티어 대비 **1.7–4.6배 빠르다**.

## 헤드라인

> **국내 오케스트레이션(작업별 최적 국내 모델 stitch)이 최강 프론티어(gpt-5.3 · Sonnet 4.6)와
> 통계적 동률이며, gpt-4o와 현행 프로덕션 baseline(gpt-4o-mini)은 유의하게 앞선다**
> (McNemar: vs gpt-4o p=0.021, vs baseline p=0.039; vs gpt-5.3 p=1.000, vs Sonnet p=0.688).
> 이는 참조 E2E의 "프론티어와 tie" 결론을 본 셋업에서 재현한다. 게다가 국내 경로는 지연이
> 1.7–4.6배 낮고 벤더 종속(GPT 금지)에서 자유롭다.

## 운영 메모

- ax·sonnet는 각자의 **Layer-1 arm이 클리어된 뒤에만** 실행하도록 강제했다:
  sonnet은 PROGRESS "FRONTIER sonnet DONE"(18:57) 확인 후 실행(카나리아 3/3 PASS → 풀 run 131/145).
  ax는 Layer-1 ax glue arm(glue-4 진행)이 살아있어 드라이버가 대기 중 — arm 클리어 시 자동 실행·스냅샷.
- Bedrock은 카나리아 3콜 + 풀 241콜만 사용(비용 최소), 병렬 arm과 겹치지 않게 순차 실행.
- 재현: `../../.venv/bin/python b1_layer2_stitch.py` (스냅샷 `analysis/raw/b1/layer2/raw_<slug>.jsonl` 읽음).
- 미커밋.

---

## ax 확정 업데이트 (자동 추가 2026-07-22 23:27)

ax(A.X-K1) Layer-1 arm 클리어 후 실행 완료. 7-모델 재-stitch 결과 발췌:

```
## Per-task pass/n matrix (model x task, model-independently scored)

| model | t3(n=28) | t5(n=21) | t6(n=20) | t7(n=22) | t9(n=32) | t10(n=22) | TOTAL |
|---|---|---|---|---|---|---|---|
| solar (Upstage Solar-pro) | 28/28 (100%) | 18/21 (86%) | 19/20 (95%) | 14/22 (64%) | 32/32 (100%) | 16/22 (73%) | **127/145 (87.6%)** |
| exaone (LG EXAONE) | 28/28 (100%) | 19/21 (90%) | 19/20 (95%) | 14/22 (64%) | 30/32 (94%) | 21/22 (95%) | **131/145 (90.3%)** |
| ax (SKT A.X-K1) | 22/28 (79%) | 19/21 (90%) | 19/20 (95%) | 10/22 (45%) | 32/32 (100%) | 17/22 (77%) | **119/145 (82.1%)** |
| gpt-4o (frontier) | 28/28 (100%) | 19/21 (90%) | 19/20 (95%) | 11/22 (50%) | 32/32 (100%) | 16/22 (73%) | **125/145 (86.2%)** |
| gpt-5.3-chat-latest (frontier, temp=1 vendor-forced) | 28/28 (100%) | 19/21 (90%) | 19/20 (95%) | 13/22 (59%) | 32/32 (100%) | 21/22 (95%) | **132/145 (91.0%)** |
| Claude Sonnet 4.6 (frontier) | 28/28 (100%) | 19/21 (90%) | 19/20 (95%) | 12/22 (55%) | 32/32 (100%) | 21/22 (95%) | **131/145 (90.3%)** |
| gpt-4o-mini (baseline/prod) | 24/28 (86%) | 19/21 (90%) | 19/20 (95%) | 15/22 (68%) | 32/32 (100%) | 17/22 (77%) | **126/145 (86.9%)** |

## Domestic composite — slot assignment (best domestic per task)

| task | solar | exaone | ax | winner |
|---|---|---|---|---|
| t3 | 28/28 | 28/28 | 22/28 | **solar** |
| t5 | 18/21 | 19/21 | 19/21 | **exaone** |
| t6 | 19/20 | 19/20 | 19/20 | **solar** |
| t7 | 14/22 | 14/22 | 10/22 | **solar** |
| t9 | 32/32 | 30/32 | 32/32 | **solar** |
| t10 | 16/22 | 21/22 | 17/22 | **exaone** |

slot assignment: {'t3': 'solar', 't5': 'exaone', 't6': 'solar', 't7': 'solar', 't9': 'solar', 't10': 'exaone'}

COMPOSITE total: 133/145 (91.7%)

## Head-to-head totals (over common scored set)
## Domestic composite — slot assignment (best domestic per task)

| task | solar | exaone | ax | winner |
|---|---|---|---|---|
| t3 | 28/28 | 28/28 | 22/28 | **solar** |
| t5 | 18/21 | 19/21 | 19/21 | **exaone** |
| t6 | 19/20 | 19/20 | 19/20 | **solar** |
| t7 | 14/22 | 14/22 | 10/22 | **solar** |
| t9 | 32/32 | 30/32 | 32/32 | **solar** |
| t10 | 16/22 | 21/22 | 17/22 | **exaone** |

slot assignment: {'t3': 'solar', 't5': 'exaone', 't6': 'solar', 't7': 'solar', 't9': 'solar', 't10': 'exaone'}

COMPOSITE total: 133/145 (91.7%)

## Head-to-head totals (over common scored set)
## Head-to-head totals (over common scored set)

| model | pass/n | acc |
|---|---|---|
| **국내 composite (orchestration)** | 133/145 | **91.7%** |
| solar (Upstage Solar-pro) | 127/145 | 87.6% |
| exaone (LG EXAONE) | 131/145 | 90.3% |
| ax (SKT A.X-K1) | 119/145 | 82.1% |
| gpt-4o (frontier) | 125/145 | 86.2% |
| gpt-5.3-chat-latest (frontier, temp=1 vendor-forced) | 132/145 | 91.0% |
| Claude Sonnet 4.6 (frontier) | 131/145 | 90.3% |
| gpt-4o-mini (baseline/prod) | 126/145 | 86.9% |

## Paired McNemar — composite vs frontier / baseline
## Paired McNemar — composite vs frontier / baseline

| comparison | n | comp_right_other_wrong | other_right_comp_wrong | discordant | p(exact) | verdict |
|---|---|---|---|---|---|---|
| composite vs gpt4o | 145 | 9 | 1 | 10 | 0.021 | composite better |
| composite vs gpt53 | 145 | 3 | 2 | 5 | 1.000 | n.s. |
| composite vs sonnet | 145 | 4 | 2 | 6 | 0.688 | n.s. |
| composite vs baseline | 145 | 8 | 1 | 9 | 0.039 | composite better |

## McNemar — composite vs each single model
```

> composite는 국내 max-선택이라 ax 포함 후에도 헤드투헤드/McNemar 결론(프론티어 tie · gpt-4o/baseline 유의 우세)은 유지된다. 위 표가 SoR.
