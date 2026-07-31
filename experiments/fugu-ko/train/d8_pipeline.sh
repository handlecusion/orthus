#!/usr/bin/env bash
# D8 자동 파이프라인 — 32B 학습완료 → 32B val 생성 → 백본 동결 → c8 k=5 생성 → 다운로드.
# 본판정(judge_d8)은 사람이 확인 후 1회 실행(사전선언 §5). GPU는 0번만 사용.
set -u
cd <repo>/.worktrees/fugu-ko-selector-v2/experiments/fugu-ko
PY=<repo>/.venv/bin/python
RENV='HF_HOME=/data/tta/hf-cache TMPDIR=/data/tta/tmp HF_HUB_OFFLINE=1 PYTHONPATH=/data/tta/pylibs CUDA_VISIBLE_DEVICES=0'

echo "[1/5] 32B 학습 완료 대기..."
until ssh -o ConnectTimeout=10 a100 'grep -q "\[train\] done" /data/tta/fugu-ko/train/logs/sft32b_v4.log' 2>/dev/null; do sleep 180; done
ssh a100 'grep "val_loss\|\[train\] done" /data/tta/fugu-ko/train/logs/sft32b_v4.log | tail -3'

echo "[2/5] 32B val 생성 (백본 선택용, GPU0)"
ssh a100 "cd /data/tta/fugu-ko && $RENV python3 -u train/sft_worker.py --gen train/data/sft_val.jsonl --ckpt train/ckpt/sft32b_v4 --quant4 --k 1 --bs_gen 8 --out_file train/data/sft_val_gen_32b_v4.jsonl > train/logs/gen_val_32b_v4.log 2>&1"
scp -q a100:/data/tta/fugu-ko/train/data/sft_val_gen_32b_v4.jsonl train/data/

echo "[3/5] 백본 동결 (학습셋 val — 판정셋 무접촉)"
$PY train/val_select.py --gen data/sft_val_gen_12b_v4.jsonl --gen2 data/sft_val_gen_32b_v4.jsonl 2>&1 | tail -6
WINNER=$($PY -c "import json;print(json.load(open('train/data/backbone_choice.json'))['winner'])")
echo "동결된 주판정 백본: $WINNER"

echo "[4/5] c8 k=5 생성 (7,190건, GPU0, 백본=$WINNER)"
QUANT=""; BSG=32
[ "$WINNER" = "sft32b_v4" ] && QUANT="--quant4" && BSG=8
ssh a100 "cd /data/tta/fugu-ko && $RENV nohup python3 -u train/sft_worker.py --gen train/data/sft_d8_prompts.jsonl --ckpt train/ckpt/$WINNER $QUANT --k 5 --bs_gen $BSG --out_file train/data/sft_d8_gen.jsonl > train/logs/gen_d8_c8.log 2>&1"
ssh a100 'tail -2 /data/tta/fugu-ko/train/logs/gen_d8_c8.log'

echo "[5/5] 생성물 회수"
scp -q a100:/data/tta/fugu-ko/train/data/sft_d8_gen.jsonl train/data/
echo "c8 생성 완료: $(wc -l < train/data/sft_d8_gen.jsonl) / 7190 줄"
echo "=== 본판정 준비 완료 — 실행 명령 ==="
echo "  $PY train/judge_d8.py --gen_file data/sft_d8_gen.jsonl"
