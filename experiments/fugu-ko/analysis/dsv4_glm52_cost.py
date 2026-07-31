"""Token/cost accounting for the DeepSeek V4 Pro / GLM-5.2 arms (2026-07-23).

Aggregates the usage sidecars written by usage_capture_run.py. Prices
(per 1M tokens, fetched from vendor docs 2026-07-23):
- deepseek-v4-pro: input cache-miss $0.435, cache-hit $0.003625, output $0.87
  (output price covers reasoning tokens as completion tokens).
- glm-5.2 (z.ai): input $1.40, cached input $0.26, output $4.40.
"""

from __future__ import annotations

import json
from pathlib import Path

RAW = Path(__file__).resolve().parent / "raw"

PRICE = {
    "deepseek-v4-pro": {"in_miss": 0.435, "in_hit": 0.003625, "out": 0.87},
    "glm-5.2": {"in_miss": 1.40, "in_hit": 0.26, "out": 4.40},
}


def agg(path: Path) -> dict | None:
    if not path.exists():
        return None
    n = it = ot = rt = hit = 0
    model = None
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        u = r.get("usage") or {}
        if not u:
            continue
        model = r.get("model") or model
        n += 1
        it += u.get("prompt_tokens", 0)
        ot += u.get("completion_tokens", 0)
        rt += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        hit += (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    if model is None:
        return None
    p = PRICE[model]
    cost = ((it - hit) * p["in_miss"] + hit * p["in_hit"] + ot * p["out"]) / 1e6
    return {
        "model": model,
        "calls": n,
        "in_tok": it,
        "cached_in_tok": hit,
        "out_tok": ot,
        "reasoning_tok": rt,
        "reasoning_share_of_out": round(rt / ot, 3) if ot else None,
        "usd": round(cost, 4),
    }


def main() -> None:
    files = sorted(RAW.glob("usage_*.jsonl"))
    total_by_model: dict[str, float] = {}
    for f in files:
        a = agg(f)
        if not a:
            continue
        total_by_model[a["model"]] = total_by_model.get(a["model"], 0.0) + a["usd"]
        print(
            f"{f.name:<34} {a['model']:<16} calls={a['calls']:<5} in={a['in_tok']:<9} "
            f"(hit {a['cached_in_tok']}) out={a['out_tok']:<8} "
            f"reason={a['reasoning_tok']:<8} ({a['reasoning_share_of_out']}) ${a['usd']}"
        )
    print()
    for m, usd in total_by_model.items():
        print(f"TOTAL {m}: ${usd:.4f}")


if __name__ == "__main__":
    main()
