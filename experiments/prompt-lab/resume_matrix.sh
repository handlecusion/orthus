#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/../.."
export PYTHONPATH=$PWD:$PWD/experiments/fugu-ko:$PWD/experiments/fugu-ko/embedding:$PWD/experiments/prompt-lab
export FUGU_KEYS=experiments/fugu-ko/keys.json ORTHUS_EMBEDDING=solar
export ORTHUS_LLM_SOLAR_API_KEY=$(python3 -c "import json;print(next(e['key'] for e in json.load(open('experiments/fugu-ko/keys.json')) if e['provider']=='upstage'))")
M=experiments/prompt-lab/matrix_lab.py; SD=experiments/prompt-lab/score_distill.py; S=experiments/prompt-lab/data/mtx_distill.jsonl
echo "### 5b. distill exaone 채점 재개 (codex)"
uv run python $SD score --run distill_baseline_exaone_mtx --judge codex --sample $S 2>&1 | tail -1
echo "### 6. wiki_qa 라운드로빈 (codex, 병렬)"
uv run python $M qa-rrjudge --judge codex --workers 4 2>&1 | tail -3
echo "### 7. 리포트"
uv run python $M report --judge codex
echo "### DONE"
