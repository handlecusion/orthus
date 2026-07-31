#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/../.."
export PYTHONPATH=$PWD:$PWD/experiments/fugu-ko:$PWD/experiments/fugu-ko/embedding:$PWD/experiments/prompt-lab
export FUGU_KEYS=experiments/fugu-ko/keys.json
export ORTHUS_PG_DSN="postgresql+psycopg://orthus:orthus@localhost:5433/orthus_miniwiki"
export ORTHUS_WIKI_STORE="$HOME/.orthus/miniwiki/wiki-store"
export ORTHUS_EMBEDDING=solar
export ORTHUS_LLM_SOLAR_API_KEY=$(python3 -c "import json;print(next(e['key'] for e in json.load(open('experiments/fugu-ko/keys.json')) if e['provider']=='upstage'))")
export ORTHUS_EMBEDDING_SOLAR_API_KEY=$ORTHUS_LLM_SOLAR_API_KEY
mkdir -p "$ORTHUS_WIKI_STORE"
MW=experiments/prompt-lab/mini_wiki.py
echo "### gen-q"; uv run python $MW gen-q 2>&1 | tail -1
for m in solar ax exaone; do
  echo "### build $m"; uv run python $MW build --distill $m --concurrency 4 2>&1 | tail -1
  echo "### answer $m"; uv run python $MW answer --distill $m 2>&1 | tail -1
done
echo "### rrjudge codex"; uv run python $MW rrjudge --judge codex --workers 5 2>&1 | tail -1
echo "### report"; uv run python $MW report --judge codex
echo "### DONE"
