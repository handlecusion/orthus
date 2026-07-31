"""miss_param asking-vs-hallucinating split from BFCL raw result files.

For each multi_turn_miss_param item, the turn whose ground truth is [] is the
"must ask / must not call" turn. We count whether the model emitted any tool
call in that turn (hallucinated arguments) or refrained/asked (correct
behavior on that axis). This is independent of BFCL's own scoring.

Usage: python bfcl_split.py <model_registry_name> [more models...]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLONE = HERE / "external" / ".cache" / "bfcl" / "berkeley-function-call-leaderboard"
RUN_ROOT = Path(
    os.getenv(
        "BFCL_RUN_ROOT",
        "/private/tmp/claude-501/-Users-ys-orca-workspaces-orthus-ai-competition-"
        "research-dataset/ced3d5c8-f479-405f-83fb-c6fc893bafbf/scratchpad/bfcl-run",
    )
)


def _load_jsonl(path: Path) -> dict[str, dict]:
    return {
        row["id"]: row
        for row in (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    }


def _turn_has_call(turn: object) -> bool:
    if not isinstance(turn, list):
        return False
    for step in turn:
        if isinstance(step, list) and any(isinstance(c, dict) for c in step):
            return True
    return False


def split_for_model(model: str) -> dict[str, float | int]:
    gt = _load_jsonl(
        CLONE / "bfcl_eval" / "data" / "possible_answer" / "BFCL_v4_multi_turn_miss_param.json"
    )
    result_path = (
        RUN_ROOT / "result" / model / "multi_turn" / "BFCL_v4_multi_turn_miss_param_result.json"
    )
    results = _load_jsonl(result_path)
    n = asked = hallucinated = errored = 0
    for item_id, row in results.items():
        turns = row.get("result")
        gt_turns = gt[item_id]["ground_truth"]
        empty_idxs = [i for i, t in enumerate(gt_turns) if t == []]
        if not empty_idxs:
            continue
        n += 1
        if not isinstance(turns, list):  # inference error string
            errored += 1
            continue
        halluc = any(i < len(turns) and _turn_has_call(turns[i]) for i in empty_idxs)
        if halluc:
            hallucinated += 1
        else:
            asked += 1
    return {
        "model": model,
        "items_with_hold_turn": n,
        "asked_or_refrained": asked,
        "hallucinated_call": hallucinated,
        "inference_error": errored,
        "asked_rate": round(asked / n, 4) if n else float("nan"),
    }


if __name__ == "__main__":
    for m in sys.argv[1:]:
        print(json.dumps(split_for_model(m), ensure_ascii=False))
