"""Run harness_e2e / m7_run / b2_run with exact per-call token-usage capture.

Untracked helper for the DeepSeek V4 Pro / GLM-5.2 bench arms (2026-07-23).
The production `OpenAIChat` adapter returns text only and discards the
`usage` block, so `_ProdAdapterUsageWrapper` records every call as
`missing=True`. For reasoning models (GLM-5.2 burned ~$10 mid-run on
reasoning-token overage in a prior incident) we need REAL token counts to
extrapolate cost. This wrapper monkeypatches the single HTTP choke point
(`_post_json`) to accumulate each response's `usage` dict into a JSONL
sidecar, then execs the target runner's `main()` with the remaining argv.

Usage:
  .venv/bin/python experiments/fugu-ko/usage_capture_run.py \
      --usage-out experiments/fugu-ko/analysis/raw/usage_glm52_l2.jsonl \
      --target harness -- <harness_e2e.py args...>

No production files are modified; the patch lives only in this process.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
for _p in (str(HERE), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_LOCK = threading.Lock()
_OUT: Path | None = None


def _record(body: dict, data: dict) -> None:
    usage = data.get("usage") if isinstance(data, dict) else None
    rec = {
        "model": body.get("model"),
        "usage": usage,
    }
    with _LOCK:
        assert _OUT is not None
        with open(_OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    global _OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--usage-out", required=True)
    ap.add_argument("--target", choices=["harness", "m7", "b2"], required=True)
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    _OUT = Path(args.usage_out)
    _OUT.parent.mkdir(parents=True, exist_ok=True)

    import orthus.models.adapters.openai_compat as oc

    _orig = oc._post_json

    def patched(base, path, key, body, timeout, retries=oc._RETRIES):  # noqa: ANN001
        data = _orig(base, path, key, body, timeout, retries)
        try:
            _record(body if isinstance(body, dict) else {}, data)
        except Exception:  # noqa: BLE001 — accounting must never break the run
            pass
        return data

    oc._post_json = patched

    rest = args.rest
    if rest and rest[0] == "--":
        rest = rest[1:]

    if args.target == "harness":
        import harness_e2e as mod
    elif args.target == "m7":
        import m7_run as mod

        mod._post_json = patched  # from-import binding in m7_run's namespace
    else:
        import b2_run as mod

        mod._post_json = patched  # from-import binding in b2_run's namespace

    sys.argv = [
        str(HERE / f"{'harness_e2e' if args.target == 'harness' else args.target + '_run'}.py"),
        *rest,
    ]
    mod.main()


if __name__ == "__main__":
    main()
