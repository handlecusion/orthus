#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
echo "########## A tn2 (적대적 TN 19) ##########"
./run_cascade_arm.sh A tn2 analysis/raw/e6_cascade_A_tn2.jsonl 2>&1 | grep -E '^\[wrap|FIRED|\[out'
echo "########## A zprobe (zero_answer 회수 프로브 9) ##########"
./run_cascade_arm.sh A zprobe analysis/raw/e6_cascade_A_zprobe.jsonl 2>&1 | grep -E '^\[wrap|FIRED|-----|\[out'
echo "R3 CONFIRM DONE"
