#!/bin/bash
# D6 Phase E — A100 3-GPU 병렬 국내 backbone 종합 스윕 (full FT + LoRA/QLoRA 스케일).
# 선택기 백본 전부 국내: 학계/커뮤니티 인코더(full FT) + LG EXAONE 2.4/7.8/32B(full/LoRA/QLoRA).
#
# ⚠️ A100 루트 fs가 100% 참 → 작업/캐시/tmp/pylib 전부 /data(6.9TB 여유)에. python은 시스템 python.
# 【WSL sync】(labeled2.jsonl 완성 후):
#   cd .worktrees/fugu-ko-selector-v2/experiments/fugu-ko
#   ssh a100 'mkdir -p /data/tta/fugu-ko/train/data /data/tta/fugu-ko/golden /data/tta/fugu-ko/analysis/raw'
#   scp train/{features,tier2v,tier3v,eval_v2,a100_train}.* a100:/data/tta/fugu-ko/train/
#   scp train/data/{labeled2.jsonl,db_names.json} a100:/data/tta/fugu-ko/train/data/
#   scp golden/t3_unseen_holdout.json a100:/data/tta/fugu-ko/golden/
#   scp analysis/raw/unseen_worker_correct.json a100:/data/tta/fugu-ko/analysis/raw/
# 【A100 실행】: ssh a100 'cd /data/tta/fugu-ko && G0=0 G1=1 G2=2 bash train/a100_train.sh'  (nvidia-smi 확인 후)
# 【회수】: scp 'a100:/data/tta/fugu-ko/train/data/sel_choice_holdout_*.json' train/data/
set -u
cd "$(dirname "$0")/.."          # /data/tta/fugu-ko
export HF_HOME=${HF_HOME:-/data/tta/hf-cache}
export TMPDIR=${TMPDIR:-/data/tta/tmp}; mkdir -p "$TMPDIR"
PYLIB=/data/tta/pylibs          # 루트 fs가 꽉 차서 peft/bnb는 여기에 --target 설치
export PYTHONPATH="$PWD:$PWD/train:$PYLIB"
PY=${PY:-python}
G0=${G0:-0}; G1=${G1:-1}; G2=${G2:-2}
L=${L:-$PWD/train/logs}; mkdir -p "$L"
echo "[a100] GPU 사용중: $(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader | tr '\n' ' ')"
echo "[a100] lanes → G0=$G0(zoo+ablation) G1=$G1(EXAONE 2.4/7.8 LoRA) G2=$G2(EXAONE 7.8full/32B)"
# peft/bnb는 LoRA/QLoRA/8bit 런에만 필요. ⚠️ --no-deps 필수 — deps 없이 설치해야 torch를 pylibs에
# 안 끌어온다(끌어오면 PYTHONPATH shadowing으로 시스템 torch/CUDA가 깨져 전 lane 죽음). 시스템 torch 사용.
$PY -c "import peft" 2>/dev/null || pip install -q --no-deps --target="$PYLIB" peft accelerate safetensors huggingface_hub || echo "[warn] peft 설치 실패 → LoRA/QLoRA 스킵됨"
$PY -c "import bitsandbytes" 2>/dev/null || pip install -q --no-deps --target="$PYLIB" bitsandbytes || echo "[warn] bnb 설치 실패 → 8bit/QLoRA 스킵됨"

T2="$PY -u train/tier2v.py"; T3="$PY -u train/tier3v.py"
KE=monologg/koelectra-base-v3-discriminator

lane0() {  # 소형 국내 인코더 full FT: ablation(4) + zoo(6)
  export CUDA_VISIBLE_DEVICES=$G0
  $T2 --backbone $KE --features 0 --dep 0 --seeds 3 --tag abl_nofeat_nodep
  $T2 --backbone $KE --features 0 --dep 1 --seeds 3 --tag abl_nofeat_dep
  $T2 --backbone $KE --features 1 --dep 0 --seeds 3 --tag abl_feat_nodep
  $T2 --backbone $KE --features 1 --dep 1 --seeds 3 --tag abl_feat_dep
  for bb in "klue/roberta-base:zoo_klue_roberta_base" "klue/roberta-large:zoo_klue_roberta_large" \
            "klue/bert-base:zoo_klue_bert_base" "$KE:zoo_koelectra_v3" \
            "snunlp/KR-ELECTRA-discriminator:zoo_kr_electra" "beomi/kcbert-base:zoo_kcbert"; do
    $T2 --backbone "${bb%%:*}" --features 1 --dep 1 --seeds 3 --tag "${bb##*:}" || echo "[warn] ${bb##*:} skip"
  done
}
lane1() {  # EXAONE-4.0 1.2B full+LoRA (native, transformers 5.13 호환). 3.5는 5.13 비호환이라 4.0으로 교체.
  export CUDA_VISIBLE_DEVICES=$G1
  $T3 --model LGAI-EXAONE/EXAONE-4.0-1.2B --full 1 --lr 1e-5 --bs 8 \
      --features 1 --dep 1 --seeds 2 --tag exaone4_1.2b_full || echo "[warn] 4.0-1.2B full skip"
  $T3 --model LGAI-EXAONE/EXAONE-4.0-1.2B --full 0 --lr 1e-4 --bs 8 \
      --features 1 --dep 1 --seeds 2 --tag exaone4_1.2b_lora || echo "[warn] 4.0-1.2B LoRA skip"
}
lane2() {  # EXAONE-4.0 32B QLoRA(4bit). 다운로드 ~64GB(/data/tta/hf-cache).
  export CUDA_VISIBLE_DEVICES=$G2
  $T3 --model LGAI-EXAONE/EXAONE-4.0-32B --quant4 1 --lr 1e-4 --bs 2 \
      --features 1 --dep 1 --seeds 1 --tag exaone4_32b_qlora || echo "[warn] 4.0-32B QLoRA skip(디스크/메모리 확인)"
}

lane0 > "$L/lane0.log" 2>&1 &
lane1 > "$L/lane1.log" 2>&1 &
lane2 > "$L/lane2.log" 2>&1 &
wait
echo "[a100] 전 lane 완료 — 주판정:"
$PY -u train/eval_v2.py --tags abl_nofeat_nodep,abl_nofeat_dep,abl_feat_nodep,abl_feat_dep,zoo_klue_roberta_base,zoo_klue_roberta_large,zoo_klue_bert_base,zoo_koelectra_v3,zoo_kr_electra,zoo_kcbert,exaone4_1.2b_full,exaone4_1.2b_lora,exaone4_32b_qlora
echo "[a100] DONE"
