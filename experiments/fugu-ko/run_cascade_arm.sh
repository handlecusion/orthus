#!/usr/bin/env bash
# SVC 캐스케이드 라이브 arm 실행 래퍼. 시크릿은 env로만 주입(커밋 금지).
# 사용: run_cascade_arm.sh <ARM: A|B|C|D> <SET: t3|tn> <OUT.jsonl>
set -euo pipefail
ARM="$1"; SET="$2"; OUT="$3"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"     # 워크트리 루트
cd "$ROOT"
source .venv/bin/activate

# --- company 노드 DB/런타임 env ---
set -a
source ~/.orthus/nodes/company/node.env
set +a
export ORTHUS_NODE=company
export FUGU_DSN="${FUGU_DSN:-postgresql://orthus:orthus@localhost:5433/orthus_company}"
export ORTHUS_MODEL_ORCHESTRATION_ENABLED=false   # 배정 계층 비개입 — arm이 1차 슬롯 직접 통제

# --- 벤더 키를 keys.json에서 추출(평문 노출 없이 env로) ---
KEYS="${FUGU_KEYS:-<로컬 키 저장소>/keys.json}"
eval "$(python3 - "$KEYS" <<'PY'
import json, sys, shlex
d = json.load(open(sys.argv[1], encoding="utf-8"))
def key(sub):
    for it in d:
        if sub in it["provider"]:
            return it["key"].strip()
    return ""
print(f"export ORTHUS_LLM_SOLAR_API_KEY={shlex.quote(key('Solar'))}")
print(f"export ORTHUS_LLM_AX_API_KEY={shlex.quote(key('A.X'))}")
print(f"export ORTHUS_LLM_EXAONE_API_KEY={shlex.quote(key('EXAONE'))}")
print(f"export ORTHUS_LLM_EXAONE_MODEL={shlex.quote('depmkuykpfon9lg')}")
PY
)"

# --- SVC 캐스케이드 on ---
export ORTHUS_STRUCTURED_FALLBACK_MODE=on

# --- arm별 1차/2차 ---
case "$ARM" in
  A)  # baseline gpt-4o-mini → A.X (핵심: 이모지 zero-answer 회수)
    export ORTHUS_LLM=openai ORTHUS_LLM_MODEL=gpt-4o-mini
    export ORTHUS_LLM_FALLBACK_PROVIDER=ax ORTHUS_LLM_FALLBACK_MODEL=A.X-K1 ;;
  B)  # 대조: gpt-4o-mini → gpt-4o-mini (같은 모델, 회수 ~0 재현). 경고 로그 정상.
    export ORTHUS_LLM=openai ORTHUS_LLM_MODEL=gpt-4o-mini
    export ORTHUS_LLM_FALLBACK_PROVIDER=openai ORTHUS_LLM_FALLBACK_MODEL=gpt-4o-mini ;;
  C)  # prod 배정 조합: solar → A.X
    export ORTHUS_LLM=solar
    export ORTHUS_LLM_FALLBACK_PROVIDER=ax ORTHUS_LLM_FALLBACK_MODEL=A.X-K1 ;;
  D)  # E1 조합: solar → EXAONE
    export ORTHUS_LLM=solar
    export ORTHUS_LLM_FALLBACK_PROVIDER=exaone ORTHUS_LLM_FALLBACK_MODEL=depmkuykpfon9lg ;;
  *) echo "unknown arm $ARM"; exit 2 ;;
esac

echo "[wrap] ARM=$ARM SET=$SET ORTHUS_LLM=$ORTHUS_LLM/$ORTHUS_LLM_MODEL FALLBACK=$ORTHUS_LLM_FALLBACK_PROVIDER/$ORTHUS_LLM_FALLBACK_MODEL"
PYTHONPATH="$ROOT:$ROOT/experiments/fugu-ko" python experiments/fugu-ko/e6_cascade.py --arm "$ARM" --set "$SET" --out "$OUT"
