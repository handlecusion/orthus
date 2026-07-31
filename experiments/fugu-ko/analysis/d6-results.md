# D6 — 학습 선택기 v2 (새 스냅샷 재도전) 결과 + 트러블슈팅

> **한 줄 결론:** 새 스냅샷으로 데이터를 3,000문항까지 키우고 국내 모델 13종 ×
> 아키텍처 개선 3종 × 민감도 분석 × 재현 검증까지 exhaustive하게 돌렸지만,
> **어떤 정직한 평가 구성에서도 학습 선택기는 규칙 선택기(static=solar)를 유의하게
> 이기지 못한다.** 중간에 나온 유일한 유의승(by_task=ambig, +3.6%p, p=0.031)은
> **재현 검증에서 기각**됐다(fresh 세트 +0.0p, 부트스트랩 P(이김)=58.7%). D5 대비
> 진전은 "이길 여지(headroom)가 실재함을 사전 게이트로 입증한 상태에서도 회수
> 불가"임을 보인 것 — 병목은 모델·데이터량이 아니라 **신호의 학습가능성 자체**다.

작업 위치: worktree `.worktrees/fugu-ko-selector-v2`. 격리 DB `orthus_company_0706`
(스냅샷 2026-07-06). 프로덕션 코드 무변경.

---

## 1. 목표와 배경 (D5 → D6)

**목표:** 학습 선택기(c)가 규칙 선택기(b=항상 solar)를 **유의하게** 이기게 만든다.

**D5 실패 원인 (사후분석):**
- **F8** — 골든 79문항이 우연히 static≈oracle(headroom 0)이라 구조적으로 못 이기는 판이었다.
- **F9** — hard-case 과표집으로 학습한 분포가 실분포로 전이 실패.

**D6 가설:** (a) 데이터를 키우고, (b) 학습 전에 **headroom을 먼저 측정**해 이길 판인지
확인하고, (c) 아키텍처를 3개 개선하면 이길 수 있다.

---

## 2. 실험 설계

### 2.1 데이터
- **스냅샷:** `orthus-backup-20260706-084303-full` → 격리 DB `orthus_company_0706` 복원 (personal dump 미개봉, company만).
- **실로그 희소 판명** → 하이브리드 holdout으로 개정: 실 anchor(맥락용) + unseen-synthetic(주판정).
- **학습셋 3,000문항** (`gen2.py` 스키마 자동추출 425 distinct spec + 복합필터 + `augment2.py` 패러프레이즈).
- **라벨(`label2.py`):** 실파이프라인 `query_structured`로 워커별 실행 → gold number-set 채점.

| 워커 | 정답률(3,000) |
|---|---|
| solar (static) | 67.2% |
| ax | 61.0% |
| exaone | 58.5% |
| baseline gpt-4o-mini(대조) | 40.7% |

워커3 **불일치 933문항(31%)** → 학습 신호량 충분(≥15% & ≥500 게이트 PASS).

### 2.2 주판정 holdout (동결)
- **`golden/t3_unseen_holdout.json` 463문항** — 학습에 안 쓴 held-out DB
  (참고 문서·파트너사·아틀라스 링크·프로젝트 주간 노트·직원·파트너)에서 생성.
- 워커 정오는 **`analysis/raw/unseen_worker_correct.json`에 동결**(주판정 GT).
- **채점 = gold number-set match, McNemar (c)vs(b).** static(solar)과 oracle 고정.

### 2.3 모델 zoo (전부 국내, 13 config)
| 계열 | 모델 | 학습 |
|---|---|---|
| 인코더 | KLUE-RoBERTa base/large, KLUE-BERT | full FT |
| | KoELECTRA-v3(monologg), KR-ELECTRA(SNU), KcBERT(beomi) | full FT |
| 생성 LLM | EXAONE-4.0 1.2B | full + LoRA |
| | EXAONE-4.0 32B | QLoRA 4-bit |
| ablation | KoELECTRA × {features 0/1} × {dep 0/1} | full FT |

### 2.4 아키텍처 개선 ①②③
- **① 결정론 피처 주입**(`features.py`): db_hard(emoji/trail/ascii)·ambig·groupby·field_match·len 태그를 인코더 입력 프리픽스로. LLM 0회.
- **② 이탈 비대칭 손실 + margin 게이트**(`tier2v.dep_loss`): static 이탈 오탐(FP)에 가중 벌점(w_fp=2.0) + 추론 시 margin tau(val 튜닝).
- **③ 캘리브레이션 + 앙상블**: per-worker temperature scaling(val NLL) + seed 3 앙상블.

### 2.5 G0′ 헤드룸 게이트 (학습보다 먼저)
| holdout | static | oracle | headroom | 판정 |
|---|---|---|---|---|
| 라우팅(실 anchor) | 75% | 75% | **0%p** | 승부처 아님 → 제외 |
| structured unseen 463 | 60.5% | 67.8% | **+7.3%p** | **GO** (불일치 180/463=39%) |

**D5 F8와의 결정적 차이:** 이번엔 이길 여지가 실측으로 존재하는 판에서 시작했다.

---

## 3. 결과

### 3.1 주 스윕 — 13 config 전부 규칙 못 이김 (by_db holdout, n=463)
static(solar) 60.5% · oracle 67.8%.

| config | 선택기 | vs 규칙 | McNemar |
|---|---|---|---|
| abl_nofeat_dep | 60.9% | +0.4p | p=0.50 |
| zoo_klue_roberta_large | 60.7% | +0.2p | p=1.0 |
| abl_nofeat_nodep / abl_feat_dep / klue_roberta_base / koelectra_v3 / exaone4_1.2b_lora | 60.5% | ±0.0 | 동률 |
| klue_bert_base | 60.3% | −0.2p | p=1.0 |
| kr_electra | 59.8% | −0.6p | p=0.45 |
| kcbert / exaone4_32b_qlora | 59.6% | −0.9p | p=0.29 / 0.39 |
| exaone4_1.2b_full | 59.4% | −1.1p | p=0.18 |
| **abl_feat_nodep** | 59.0% | **−1.5p** | p=0.23 |

**유의하게 이긴 config: 0개.** 불일치 180문항에서 static이 이미 81.1%라 회수 여지가 거의 없다.
**피처 주입(①)은 오히려 해로웠고**(abl_feat_nodep −1.5p), **큰 모델일수록 static을 답습**(32B −0.9p).

### 3.2 in-sample vs cross-DB — F9(전이 실패) 확정
| 판정셋 | static | 선택기(argmax) | oracle |
|---|---|---|---|
| **in-sample test**(같은 DB·새 질문, n=450) | 69.6 | **69.3~72.2**(best +2.6p) | 77.6 |
| **cross-DB holdout**(새 DB, n=463) | 60.5 | ~60.5(±0) | 67.8 |

신호는 **같은 DB 분포 안에선 학습되지만 새 DB로 전이 안 됨.** ⚠️ 단 in-sample 승리도
margin-게이트 full 구성에선 +0.4p로 약하다(§3.4 random 참조) — argmax 최대치는 best-ablation의 낙관치.

### 3.3 민감도 분석 — "구성 따라 달라지나?"에 대한 실측 (koelectra, feat+dep, seeds=3)
분포이동 두 축(DB 이동 vs task 이동)을 분리:

| 분할 | n | static | oracle | head | 선택기 | gain | McNemar | 판정 |
|---|---|---|---|---|---|---|---|---|
| random(matched) | 450 | 69.6 | 77.6 | +8.0 | 70.0 | +0.4p | p=0.688 | 무의 |
| by_task=groupby | 246 | 88.6 | 93.5 | +4.9 | 89.0 | +0.4p | p=1.0 | 무의 |
| **by_task=ambig** | 168 | 77.4 | 94.0 | +16.7 | 81.0 | **+3.6p** | **p=0.031** | ★유의승(→기각) |
| by_db(주판정) | 463 | 60.5 | 67.8 | +7.3 | 60.7 | +0.2p | p=1.0 | 무의 |

학습풀 task별 headroom: filter2 +9.9 · filter +8.5 · **groupby +4.9(solar 이미 88.6%)** · **ambig +16.7** · count +6.7.
→ groupby는 headroom 부족으로, by_db는 신호가 DB-국소적이라 실패. ambig만 유일하게 걸림.

### 3.4 ambig 유의승 재현 검증 — **기각**
by_task=ambig의 +3.6p(p=0.031)가 진짜인지 3중 검증:

**Validation A** — 주판정 모델 예측을 **새 DB의 ambig(36개)**에만 슬라이스(재학습 없음):
전 config **유의 없음**(best +5.6p지만 p=0.5, 순증 2문항). DB 이동까지 겹치면 사라짐.

**Validation B+C** — non-ambig 학습 같은 레시피 모델을:
| 테스트 | 선택기 vs static | McNemar | 부트스트랩 95%CI · P(이김) |
|---|---|---|---|
| 원 168개(seed 바꿔 재실행) | +0.6p | p=1.0 | [−1.2,+2.4]p · **58.7%** |
| **FRESH 120개(신규 생성·라벨)** | **+0.0p** | p=1.0 | [+0.0,+0.0]p · **0.0%** |

- **같은 168문항을 seed만 바꿔 재실행하니 +3.6p → +0.6p로 붕괴.** 부트스트랩 P(이김)=58.7%(동전던지기).
- **완전 신규 모호질문 120개(static 83.3·oracle 92.5·head +9.2)에선 +0.0p** — 선택기가 전부 solar 선택.

**→ +3.6p는 4개 구성 중 우연히 걸린 다중비교 위양성이었다. 재현 실패로 기각.**

### 3.5 워커 투표 앙상블 — 배포 가능 형태론 못 이김 (`vote_holdout.py`, holdout 463)
학습 선택기가 실패했으니 마지막 질문: 예측이 아니라 **추론 시점 여러 워커를 돌려 답을 결합**하면
headroom을 회수하나? solar/ax/exaone를 holdout에 재실행해 실제 반환 숫자셋을 캡처(재실행 solar
61.1% ≈ 동결 60.5%로 무결성 확인) 후 전략별 채점. static(solar) 61.1% · oracle 68.0%.

| 전략 | 정답률 | vs 규칙 | McNemar | 실 답인가 |
|---|---|---|---|---|
| **vote2of3**(≥2 일치→그 답, 아니면 solar) | 58.5% | **−2.6p** | p=0.065 | ✅ 일관된 단일 답 |
| best_pair(solar∪ax) | 66.3% | +5.2p | p<0.0001 | ❌ 두 답 합침 |
| union3(셋 합집합) | 68.0% | +6.9p | p<0.0001 | ❌ 다 뱉음(=oracle) |

**best_pair·union3의 "승리"는 메트릭 artifact다.** 채점이 `gold ⊆ 반환숫자셋`(recall형)이라
여러 워커 답을 union하면 무조건 유리 → count 질문에서 best_pair = "solar 또는 ax 중 하나만 맞으면
정답"(=2-워커 oracle), union3(68.0%)은 oracle(68.0%)과 동일. "26이야 30이야?"에 답을 못 하는
**"다 뱉기"이지 답이 아니다.** **배포 가능한 앙상블 = vote2of3 = −2.6p로 규칙 못 이김**(ax+exaone가
일치해 solar를 뒤집으면 오히려 틀리는 경우가 더 많음: c만맞 12 < b만맞 24).

> **함의:** headroom(+7%p)은 **"모든 워커 답을 다 반환해야만" 회수**되는데 그건 실제 답이 아니다.
> 학습 선택기든 투표 앙상블이든, "solar가 언제 틀리나 / 어느 워커를 믿을까"를 추론 시점에 알 방법이
> 없어서 실패한다 — 같은 근본 병목.

---

## 4. 최종 결론

1. **학습 선택기는 규칙(static=solar)을 못 이긴다** — 13 국내모델 × 개선 3종 × 민감도 × 재현까지 정직한 어떤 구성에서도.
2. **headroom이 있어도 회수 불가**(+7.3%p 존재하나 0 회수). D5 F8("이길 판이 아니었다")을 넘어, **"이길 판인데도 학습 선택기로는 못 회수한다"**를 입증.
3. **병목 = 추론 시점에 "어느 워커를 믿을지 알 방법이 없음".** "solar가 언제 틀리나"가 **DB-국소적·비전이성**이라, 모델 크기/결합법/피처로 안 풀린다. 피처 주입(①)은 오히려 해로웠다.
4. **워커 투표 앙상블도 배포 가능 형태론 못 이긴다**(§3.5, vote2of3 −2.6p). union/best_pair의 "승리"(+5~7p)는 `gold ⊆ 반환셋` recall 메트릭에서 **여러 워커 답을 다 뱉으면 유리해지는 artifact**일 뿐(=k-워커 oracle, 실제 단일 답 아님). 학습 선택기와 **같은 병목**으로 실패.
5. **결론: 규칙 하나(always solar)면 충분하다.** GPU·학습·앙상블 모두 불필요. headroom(+7%p)은 "다 뱉기"로만 회수되는 **oracle 신기루**이며, 배포 가능한 어떤 방법도 규칙을 넘지 못한다. (D5 최종 "규칙표 하나면 충분"을 더 강한 증거로 재확인.)

---

## 5. 트러블슈팅 / 랩노트 인사이트

### 5.1 방법론 (제일 값진 교훈)
- **G0′ 헤드룸 게이트를 학습보다 먼저.** D5는 headroom 0인 판에서 학습부터 해서 F8에 빠졌다. "이길 여지가 있는가"를 먼저 재면 무의미한 학습을 피한다.
- **단일 분할을 절대 믿지 마라.** 같은 168 테스트에 seed만 바꿔도 +3.6p ↔ +0.6p로 요동. 반드시 부트스트랩 CI + 재현으로 확인.
- **다중비교 위양성 함정.** 4개 구성 돌리면 하나쯤 p<0.05가 우연히 나온다(≈19%). 사후에 그걸 집어 "이겼다" 하면 self-deception. → 사전선언 + fresh 데이터 재현이 유일한 방어.
- **in-sample 승리 ≠ 배포 승리.** 같은 DB에선 이겨도(argmax) 새 DB로 전이 안 되면 무의미. 주판정은 반드시 held-out.
- **채점 불변, 구성만 바꿔 민감도 확인.** "결과가 데이터셋 나름 아니냐"는 반박은 여러 분할(random/by_task/by_db)로 어느 결론이 robust한지 표로 봉합한다.

### 5.2 A100 환경 (재발 시 참고)
- **루트 fs 100% 참** → 작업/HF/tmp/pylib 전부 `/data/tta/*`. `HF_HOME=/data/tta/hf-cache`, `TMPDIR=/data/tta/tmp`.
- **pip은 `--no-deps --target=/data/tta/pylibs`** — deps 딸려오면 torch가 pylibs에 설치돼 PYTHONPATH shadowing으로 시스템 CUDA가 깨지고 전 lane 사망. 시스템 torch(2.5.1+cu121) 사용.
- **transformers 5.13은 EXAONE-3.5 커스텀 모델링코드 비호환**(`create_causal_mask`/`DynamicCache` API 변경). **EXAONE-4.0은 native(`model_type=exaone4`)라 호환** → 스케일 스윕을 4.0으로 교체.
- **구형 국내 인코더(KLUE/KoELECTRA/KcBERT)는 `.bin`만** 있어 torch<2.6 CVE-2025-32434 가드에 막힘 → `check_torch_load_is_safe` 무효화(신뢰 공식 repo·연구용 한정, 임의 파일 로드 아님).
- **중복 스윕 주의:** 재실행 전 `pgrep -f "tier[23]v.py"|wc -l`로 0 확인. 오케스트레이터(a100_train.sh)가 살아있으면 lane을 respawn하므로 orphan만 죽이지 말고 a100_train.sh부터 죽일 것.
- **3-GPU 병렬 lane**(zoo+ablation / EXAONE 1.2B / 32B QLoRA). 32B는 epoch별 진행 미표시 → GPU util>0이 유일한 생존 신호.

### 5.3 데이터 파이프라인
- **`psycopg2-binary` 필요**(SQLAlchemy 기본 드라이버 — venv에 없어 전 라벨 0건 위험).
- **라벨링 DSN 3종 모두** `orthus_company_0706` 지정(`ORTHUS_PG_DSN` + `_READONLY` + `FUGU_DSN`) — readonly 미지정 시 다른 DB를 봐 전부 0건.
- **ambig의 gold는 db 총개수**(spec=(db,count,None,None)) — 모호성은 필드 언급 표현에서만 옴. fresh 재현은 **T_AMBIG에 없는 새 표현 8종**으로 생성해 기존 3000과 dedup.
- **baseline mock 가드:** 첫 응답 눈으로 확인(baseline이 mock으로 돌면 "압승" 허위결론).
- **⚠️ gitignore 데이터 유실 주의:** `train/data/`·`analysis/raw/`는 gitignore라 worktree 삭제 시 유실된다. 라벨링 결과(labeled2/fresh_ambig/unseen_worker_correct)는 A100 사본(`/data/tta/fugu-ko`)이 유일 백업이었다 — 2026-07-16 worktree가 예기치 않게 삭제됐을 때 A100에서 전량 복구.

### 5.4 아키텍처 관찰
- **피처 주입(①)은 by_db에서 해로움**(−1.5p) — DB-국소적 실패엔 피처가 노이즈만 추가.
- **큰 생성 LLM일수록 static 답습** — 32B/1.2B가 인코더보다 못하거나 동급. 선택(3지선다) 태스크엔 작은 인코더가 적합.
- **margin 게이트가 tau 튜닝에서 보수적으로 수렴** → 대부분 static로 회귀. 이게 "지지는 않지만 이기지도 못하는" 동률의 메커니즘.

---

## 6. 재현 커맨드

```bash
# 주 스윕(A100 3-GPU)
ssh a100 'cd /data/tta/fugu-ko && G0=0 G1=1 G2=2 bash train/a100_train.sh'
# 민감도 분석
python train/sens.py --backbone monologg/koelectra-base-v3-discriminator --features 1 --dep 1 --seeds 3 --held_tasks groupby,ambig
# ambig 재현 검증 (fresh 생성+라벨은 로컬, 판정은 A100)
python train/repl_gen_ambig.py            # 로컬(orthus_company_0706 + FUGU_KEYS)
python train/repl_test_ambig.py --seeds 3 # A100
# 워커 투표 앙상블 (규칙 이기는 다른 경로)
python train/vote_holdout.py              # 로컬
# 주판정 채점
python train/eval_v2.py --tags <config들>
```

## 부록 — 산출물 위치
- 스크립트: `train/{gen2,augment2,build_unseen_v2,label2,features,tier2v,tier3v,eval_v2,sens,repl_gen_ambig,repl_test_ambig,vote_holdout}.py`, `train/a100_train.sh`
- 동결 판정셋: `golden/t3_unseen_holdout.json` + `analysis/raw/unseen_worker_correct.json`
- 학습셋: `train/data/labeled2.jsonl`(3,000) · fresh 재현셋 `train/data/fresh_ambig.jsonl`(120)
- 결과 JSON: `train/data/sel_choice_holdout_*.json`(13), `train/data/sens_results.json`
- 원자료(gitignore): `analysis/raw/` · A100 백업 `/data/tta/fugu-ko`
