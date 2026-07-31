#!/usr/bin/env bash
# 모델×태스크 매트릭스 전 파이프라인 (국내 모델 전용, OpenAI 불요).
set -e
cd "$(dirname "$0")/../.."
export PYTHONPATH=$PWD:$PWD/experiments/fugu-ko:$PWD/experiments/fugu-ko/embedding:$PWD/experiments/prompt-lab
export FUGU_KEYS=experiments/fugu-ko/keys.json ORTHUS_EMBEDDING=solar
# retrieve()의 Solar 임베딩용 키 (keys.json upstage 항목 재사용 — 같은 Upstage 계정)
export ORTHUS_LLM_SOLAR_API_KEY=$(python3 -c "import json;print(next(e['key'] for e in json.load(open('experiments/fugu-ko/keys.json')) if e['provider']=='upstage'))")
M=experiments/prompt-lab/matrix_lab.py
DH=experiments/prompt-lab/distill_harness.py
SD=experiments/prompt-lab/score_distill.py
S=experiments/prompt-lab/data/mtx_distill.jsonl
JUDGE=codex    # ChatGPT 플랜 OAuth 판정자 — 중립 GPT급, API 빌링 무관, 국내 self-preference 없음

echo "### 1. qa-gen (도메스틱 로테이션 생성기)"
uv run python $M qa-gen --n 60

echo "### 2. distill 3모델 실행"
for m in solar ax exaone; do
  uv run python $DH --variant baseline --model $m --sample $S --tag mtx 2>&1 | tail -1
done

echo "### 3. qa-answer 3모델"
for m in solar ax exaone; do uv run python $M qa-answer --model $m 2>&1 | tail -1; done

echo "### 4. rewrite/syn 골든 + 3모델"
uv run python $M rw-gen; uv run python $M syn-gen
for m in solar ax exaone; do
  uv run python $M rw --model $m 2>&1 | tail -1
  uv run python $M syn --model $m 2>&1 | tail -1
done

echo "### 5. distill 국내 판정자 채점 ($JUDGE)"
for m in solar ax exaone; do
  uv run python $SD score --run distill_baseline_${m}_mtx --judge $JUDGE --sample $S 2>&1 | tail -1
done

echo "### 6. wiki_qa 라운드로빈 판정 (codex 중립 판정자, 전 쌍 동일 판정자)"
uv run python $M qa-rrjudge --judge $JUDGE 2>&1 | tail -1

echo "### 7. 리포트"
uv run python $M report --judge $JUDGE

echo "### DONE"
