"""BFCL v3 multi-turn runner (fugu-ko external validity; see analysis/bfcl-prereg.md).

Loads vendor keys from repo-root .env in-process (never printed, never written to
disk), then invokes the pinned BFCL clone's CLI (external/.cache/bfcl) with
BFCL_PROJECT_ROOT pointed at a scratch run dir.

Usage:
  python bfcl_run.py generate --model solar-pro-FC --category multi_turn_base \
      [--canary 5] [--threads 4]
  python bfcl_run.py evaluate --model solar-pro-FC --category multi_turn_base
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ENV = HERE.parents[1] / ".env"
CLONE = HERE / "external" / ".cache" / "bfcl" / "berkeley-function-call-leaderboard"
RUN_ROOT = Path(
    os.getenv(
        "BFCL_RUN_ROOT",
        "/private/tmp/claude-501/-Users-ys-orca-workspaces-orthus-ai-competition-"
        "research-dataset/ced3d5c8-f479-405f-83fb-c6fc893bafbf/scratchpad/bfcl-run",
    )
)
VENV_PY = Path(
    os.getenv(
        "BFCL_VENV_PY",
        "/private/tmp/claude-501/-Users-ys-orca-workspaces-orthus-ai-competition-"
        "research-dataset/ced3d5c8-f479-405f-83fb-c6fc893bafbf/scratchpad/"
        "bfcl-venv/bin/python",
    )
)

_KEY_PREFIXES = ("ORTHUS_LLM_SOLAR_", "ORTHUS_LLM_EXAONE_", "ORTHUS_LLM_AX_", "ORTHUS_LLM_BEDROCK_")


def _load_env() -> dict[str, str]:
    env = dict(os.environ)
    for line in REPO_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.startswith(_KEY_PREFIXES):
            env[k] = v
    bedrock_key = env.get("ORTHUS_LLM_BEDROCK_API_KEY")
    if bedrock_key:
        env["AWS_BEARER_TOKEN_BEDROCK"] = bedrock_key
    env["BFCL_PROJECT_ROOT"] = str(RUN_ROOT)
    return env


def _write_canary_ids(categories: list[str], n: int) -> None:
    ids: dict[str, list[str]] = {}
    for cat in categories:
        data = CLONE / "bfcl_eval" / "data" / f"BFCL_v4_{cat}.json"
        cat_ids = [json.loads(line)["id"] for line in data.read_text().splitlines() if line.strip()]
        ids[cat] = cat_ids[:n]
    out = RUN_ROOT / "test_case_ids_to_generate.json"
    out.write_text(json.dumps(ids, indent=2))
    print(f"canary ids written: { {k: len(v) for k, v in ids.items()} } -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["generate", "evaluate"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--category", required=True, help="comma-separated test categories")
    ap.add_argument("--canary", type=int, default=0, help="run only first N ids per category")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--allow-overwrite", action="store_true")
    ap.add_argument("--partial", action="store_true", help="evaluate: partial-eval for canary")
    args = ap.parse_args()

    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    categories = [c.strip() for c in args.category.split(",") if c.strip()]

    cmd = [str(VENV_PY), "-m", "bfcl_eval", args.command, "--model", args.model]
    if args.command == "generate":
        if args.canary:
            _write_canary_ids(categories, args.canary)
            cmd += ["--run-ids"]
        else:
            cmd += ["--test-category", ",".join(categories)]
        cmd += ["--num-threads", str(args.threads)]
        if args.allow_overwrite:
            cmd += ["--allow-overwrite"]
    else:
        cmd += ["--test-category", ",".join(categories)]
        if args.partial:
            cmd += ["--partial-eval"]

    print("exec:", " ".join(cmd))
    return subprocess.call(cmd, env=env, cwd=str(CLONE))


if __name__ == "__main__":
    sys.exit(main())
