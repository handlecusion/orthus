"""WB — WorkBench ko/en 슬라이스 채점 (evaluator 무수정 재사용).

각 (model, domain)의 최신 결과 CSV를 WorkBench `compute_metrics`로 채점해
3-way(success / harmless fail / harmful side-effect) 집계를 출력하고
`wb_scores.json`(clone 루트)에 남긴다.

실행:
  external/.cache/workbench/.venv/bin/python external/wb_score.py \
      --models solar,exaone,sonnet --files email_ko,calendar_ko
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
WB = HERE / ".cache" / "workbench"
TAO = WB / "data" / "processed" / "tasks_and_outcomes"

import os  # noqa: E402

os.chdir(WB)
sys.path.insert(0, str(WB))

from src.evals.metrics import compute_metrics, get_latest_results_path  # noqa: E402

MODEL_SLUGS = {
    "solar": "solar",
    "exaone": "exaone",
    "sonnet": "claude-sonnet-4-6-bedrock",
    "opus": "claude-opus-4-5-bedrock",
    "opus-4-6": "claude-opus-4-6-bedrock",
    "gpt-5.3": "gpt-5.3-chat",
}


def score_one(model_name: str, domain: str) -> dict | None:
    found = get_latest_results_path(str(WB / "data" / "results"), model_name, domain)
    if found is None:
        return None
    results_path, _ = found  # gt 경로는 ko/enctl 파일로 직접 지정
    gt = pd.read_csv(TAO / f"{domain}_tasks_and_outcomes.csv", dtype=str)
    gt["outcome"] = gt["outcome"].apply(ast.literal_eval)
    pred = pd.read_csv(results_path, dtype=str, engine="python", on_bad_lines="warn")
    pred = pred.fillna("")
    pred["function_calls"] = pred["function_calls"].apply(ast.literal_eval)
    df = compute_metrics(gt[["task", "outcome"]], pred)
    n = len(df)
    correct = int(df["correct"].sum())
    side = int(df["unwanted_side_effects"].sum())
    return {
        "results_file": Path(results_path).name,
        "n": n,
        "success": correct,
        "harmful_side_effect": side,
        "harmless_fail": n - correct - side,
        "success_pct": round(correct / n * 100, 1),
        "harmful_pct": round(side / n * 100, 1),
        "no_actions": int(df["no_actions"].sum()),
        "errors": int((df["error"] != "").sum()),
        "failed_tasks": df.loc[~df["correct"], "task"].tolist(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,exaone,sonnet")
    ap.add_argument("--files", default="email_ko,calendar_ko")
    ap.add_argument("--out", default="wb_scores.json")
    args = ap.parse_args()

    all_scores: dict = {}
    for short in args.models.split(","):
        model_name = MODEL_SLUGS[short.strip()]
        for domain in args.files.split(","):
            s = score_one(model_name, domain.strip())
            key = f"{short}/{domain}"
            if s is None:
                print(f"{key}: NO RESULTS")
                continue
            all_scores[key] = s
            print(
                f"{key}: success {s['success']}/{s['n']} ({s['success_pct']}%) | "
                f"harmless {s['harmless_fail']} | harmful {s['harmful_side_effect']} "
                f"({s['harmful_pct']}%) | no_actions {s['no_actions']} | errors {s['errors']}"
            )
    out = WB / args.out
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing.update(all_scores)
    out.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
