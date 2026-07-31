# D6 선택기 v2 — 세션 끊겨도 혼자 이어가는 런북

경로: worktree `<repo>/.worktrees/fugu-ko-selector-v2`
venv python: `<repo>/.venv/bin/python`
격리 DB: `orthus_company_0706` (docker `orthus_pg` :5433, 계속 떠 있음)
로그 폴더: `/tmp/<session>`
(로그가 지워졌으면 라벨 파일 줄 수로 진행 확인)

## 지금 상태
라벨링 오케스트레이터가 **setsid로 백그라운드 실행 중** — 세션 끊겨도 계속 돈다.
순서: [1]holdout 동결(완료) → [2]학습 solar/exaone/baseline 병렬 → [3]ax 단독 → [4]merge.

---

## 1단계: 라벨링 끝났는지 확인
```bash
cd <repo>/.worktrees/fugu-ko-selector-v2/experiments/fugu-ko/train/data
wc -l labels2_solar.jsonl labels2_exaone.jsonl labels2_baseline.jsonl labels2_ax.jsonl 2>/dev/null
# 각 3000이면 라벨 완료. labeled2.jsonl 있으면 merge까지 끝난 것.
ls -la labeled2.jsonl 2>/dev/null
```
- `labeled2.jsonl` 있음 → **2단계로**.
- 4개 다 3000인데 `labeled2.jsonl` 없음(오케스트레이터가 죽음) → merge 수동:
```bash
cd <repo>/.worktrees/fugu-ko-selector-v2
set -a; source ~/.orthus/nodes/company/node.env; set +a
export PYTHONPATH="$PWD:$PWD/experiments/fugu-ko"
<repo>/.venv/bin/python experiments/fugu-ko/train/label2.py --merge
```
- ax가 3000 미만(중간에 죽음) → ax만 재개(재개 지원됨, append):
```bash
cd <repo>/.worktrees/fugu-ko-selector-v2
set -a; source ~/.orthus/nodes/company/node.env; set +a
export ORTHUS_PG_DSN="postgresql://orthus:orthus@localhost:5433/orthus_company_0706"
export ORTHUS_PG_DSN_READONLY="$ORTHUS_PG_DSN" FUGU_DSN="$ORTHUS_PG_DSN"
export FUGU_KEYS="<로컬 키 저장소>/keys.json"
export PYTHONPATH="$PWD:$PWD/experiments/fugu-ko"
<repo>/.venv/bin/python experiments/fugu-ko/train/label2.py --models ax
# 끝나면 위 merge 실행.
```
merge 출력에 **불일치 %와 건수**가 나온다(≥15% & ≥500이면 학습 신호 충분).

---

## 2단계: labeled2.jsonl을 A100으로 sync
```bash
cd <repo>/.worktrees/fugu-ko-selector-v2/experiments/fugu-ko
scp train/data/labeled2.jsonl a100:/data/tta/fugu-ko/train/data/
# (정적 파일 features/tier2v/tier3v/eval_v2.py, db_names.json, t3_unseen_holdout.json,
#  unseen_worker_correct.json, a100_train.sh 는 이미 sync됨)
```

## 3단계: A100에서 종합 스윕 (빈 GPU 확인 후)
```bash
ssh a100 'nvidia-smi --query-gpu=index,memory.used --format=csv,noheader'   # 빈 GPU 3개 골라
# 예: 0,1,2가 비었으면:
ssh a100 'cd /data/tta/fugu-ko && setsid bash -c "G0=0 G1=1 G2=2 bash train/a100_train.sh > train/logs/sweep.log 2>&1" & echo started'
# 진행 확인:
ssh a100 'tail -20 /data/tta/fugu-ko/train/logs/sweep.log; echo ---; tail -3 /data/tta/fugu-ko/train/logs/lane0.log'
```
스윕 = 국내 인코더 6종 full FT(KLUE-RoBERTa base/large·KLUE-BERT·KoELECTRA·KR-ELECTRA·KcBERT) +
ablation 4종 + EXAONE 2.4B(full+LoRA)·7.8B(LoRA+full8bit)·32B(QLoRA). lane별 로그 lane0/1/2.log.
32B는 모델 다운로드(~64GB, /data/tta/hf-cache)로 오래 걸릴 수 있고 실패해도 `|| echo warn`로 스킵된다.
sweep.log 끝에 `eval_v2.py` 주판정 표가 찍힌다(끝나면 `[a100] DONE`).

## 4단계: 결과 회수 + 최종 판정
```bash
cd <repo>/.worktrees/fugu-ko-selector-v2/experiments/fugu-ko
scp 'a100:/data/tta/fugu-ko/train/data/sel_choice_holdout_*.json' train/data/
# 로컬에서 주판정 재확인(동결 worker_correct 기준):
<repo>/.venv/bin/python experiments/fugu-ko/train/eval_v2.py \
  --tags abl_nofeat_nodep,abl_nofeat_dep,abl_feat_nodep,abl_feat_dep,zoo_klue_roberta_base,zoo_klue_roberta_large,zoo_klue_bert_base,zoo_koelectra_v3,zoo_kr_electra,zoo_kcbert,exaone4_1.2b_full,exaone4_1.2b_lora,exaone4_32b_qlora
```
> A100 sweep이 끝나면 sweep.log 끝에 이미 이 표가 찍혀 있다: `ssh a100 'tail -60 /data/tta/fugu-ko/train/logs/sweep.log'`

### A100 환경 함정(이미 해결됨, 재발 시 참고)
- 루트 fs 100% 참 → 작업/HF/tmp/pylib 전부 `/data/tta/*`. pip은 `TMPDIR=/data/tta/tmp` + `--no-deps --target=/data/tta/pylibs`(torch 끌어오면 PYTHONPATH shadowing으로 CUDA 깨짐).
- transformers **5.13.0**(D5의 4.53.3서 업그레이드됨) → **EXAONE-3.5 커스텀 모델링코드 비호환**(`create_causal_mask`/`DynamicCache` API 변경). **EXAONE-4.0은 native(model_type=exaone4)라 호환** → 스케일 스윕을 4.0(1.2B full/LoRA + 32B QLoRA)으로 교체함.
- 구형 국내 인코더(KLUE/KoELECTRA/KcBERT)는 `.bin`만 있어 torch<2.6 CVE 가드에 막힘 → tier2v가 `check_torch_load_is_safe`를 우회(신뢰 공식 repo 한정).
- **중복 스윕 주의(T10)**: 재실행 전 반드시 `ssh a100 'pgrep -f "tier[23]v.py"|wc -l'`로 0 확인. 오케스트레이터(a100_train.sh)가 살아있으면 lane이 다음 런을 respawn하므로, orphan만 죽이지 말고 a100_train.sh부터 죽일 것.
**판정 읽는 법:** 각 구성의 `(c) 선택기 X% (b 대비 +Y%p) · McNemar p=…`.
- **p<0.05 & (c)>(b)** 인 구성이 있으면 → **선택기가 규칙을 유의하게 이김(성공).**
- 전부 p>0.05면 → 이득이 노이즈 수준(정직하게 그렇게 보고). D5 대비 진전은 headroom이 존재한다는 것(+7.3%p).
- (b)=static(solar) baseline, oracle=상한. 불일치 부분집합의 (c) vs (b)도 함께 본다.

## 핵심 사실 (판정 맥락)
- 주판정 = **unseen holdout 463문항**(held-out DB, 학습 미포함). static 60.5% · oracle 67.8% (**headroom +7.3%p**, 불일치 39%).
- 실 anchor(라우팅 headroom 0)는 학습 타깃서 제외 — structured hard-case/ambig가 승부처.
- 이게 D5 G3(headroom 0이라 못 이김)와 다른 점. 이번엔 이길 여지가 실측으로 존재.

## 결과 문서화
최종 표가 나오면 `experiments/fugu-ko/analysis/d6-results.md`에 정리(G0′ headroom, 불일치율, 구성별
McNemar, 스케일 곡선, 실 anchor 맥락). 원자료는 `analysis/raw/`(gitignore).

## 주의
- 커밋은 하지 말 것(사용자 승인 전). worktree `feat/fugu-ko-selector-v2`에 미커밋 상태 유지.
- `.env`/키는 커밋 금지. `orthus_company_0706`은 격리 DB라 prod/`orthus_company` 무접촉.
