#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
for arm in A B C; do
  echo "########## $arm trap ##########"
  ./run_cascade_arm.sh $arm trap analysis/raw/e6_cascade_${arm}_trap.jsonl 2>&1 | grep -E '^\[wrap|FIRED|-----|\[out'
done
echo "TRAP ALL DONE"
