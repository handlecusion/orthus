#!/usr/bin/env bash
# 엄격 순차 실행 — audit_log span 캡처는 순차 전제라 절대 동시 실행 금지.
set -uo pipefail
cd "$(dirname "$0")"
run() { echo "########## $* ##########"; ./run_cascade_arm.sh "$@" 2>&1 | grep -E '^\[wrap|FIRED|\[out'; echo "exit=$?"; }

run C t3 analysis/raw/e6_cascade_C.jsonl
run D t3 analysis/raw/e6_cascade_D.jsonl
run A tn analysis/raw/e6_cascade_A_tn.jsonl
run B tn analysis/raw/e6_cascade_B_tn.jsonl
run C tn analysis/raw/e6_cascade_C_tn.jsonl
echo "ALL DONE"
