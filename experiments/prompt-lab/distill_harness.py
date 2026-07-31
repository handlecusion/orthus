"""Track C — distill 프롬프트 per-doc A/B 하네스.

프로덕션 `_complete_distill_json`(순수 LLM 단계)을 프롬프트 변형 monkeypatch로
실행한다 — DB/wiki-store 무접촉, 프로덕션 코드 diff 0줄. 표본은
`data/distill_sample.jsonl`(prod company-scope 층화 80 docs, gitignore).

실행:
    PYTHONPATH=$PWD:$PWD/experiments/fugu-ko \
    FUGU_KEYS=experiments/fugu-ko/keys.json \
    uv run python experiments/prompt-lab/distill_harness.py \
        --variant baseline --model solar [--limit 5] [--repeat 1]

출력: analysis/raw/distill_{variant}_{model}.jsonl (resume: (doc_id, rep) 키가
이미 있으면 스킵). 각 행: 파싱된 claims(프로덕션 동일 정규화) + raw JSON +
usage + 소요시간 + 오류.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import UUID

LAB_DIR = Path(__file__).resolve().parent
RAW_DIR = LAB_DIR / "analysis" / "raw"

_MAX_CLAIMS_KEY = "_MAX_CLAIMS"


def _parse_claims_like_production(data: dict, title: str) -> dict:
    """`distill_document`의 claim 파싱 루프(정규화·dedupe·cap)를 미러링한다.

    프로덕션 헬퍼(`_coerce_*`, `_kebab`, `_claim_slug`)를 import해 동일 동작을
    보장한다 — 루프 자체는 함수로 분리돼 있지 않아 최소한으로 미러링.
    """
    from orthus.wiki import distill as d

    claims: list[dict] = []
    seen: set[str] = set()
    page_slugs: list[str] = []

    raw_claims = data.get("claims", [])
    if not isinstance(raw_claims, list):
        raw_claims = []
    for c in raw_claims[: getattr(d, _MAX_CLAIMS_KEY)]:
        if not isinstance(c, dict):
            continue
        page = c.get("page", {}) or {}
        if not isinstance(page, dict):
            page = {}
        page_slug = d._kebab(
            d._coerce_text(page.get("slug")) or d._coerce_text(page.get("title")) or title
        )
        claim_text = d._coerce_text(c.get("claim"))
        if not claim_text:
            continue
        slug = d._claim_slug(page_slug, claim_text)
        if slug in seen:
            continue
        seen.add(slug)
        if page_slug not in page_slugs:
            page_slugs.append(page_slug)
        claims.append(
            {
                "slug": slug,
                "page_slug": page_slug,
                "claim": claim_text,
                "evidence": d._coerce_text(c.get("evidence")),
                "confidence": d._coerce_confidence(c.get("confidence")),
            }
        )
    return {
        "summary": d._coerce_text(data.get("summary")),
        "key_concepts": d._coerce_text_list(data.get("key_concepts", [])),
        "n_raw_claims": len(raw_claims),
        "claims": claims,
        "page_slugs": page_slugs,
    }


def _build_chat(model: str):
    import pool  # experiments/fugu-ko/pool.py (PYTHONPATH에 포함 필요)

    if model == "baseline":
        return pool._baseline_chat()
    return pool.build_pool([model])[model]


def _load_done(out_path: Path) -> set[tuple[str, int]]:
    done: set[tuple[str, int]] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            done.add((row["doc_id"], row.get("rep", 0)))
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True)
    ap.add_argument("--model", default="solar")
    ap.add_argument("--sample", default=str(LAB_DIR / "data" / "distill_sample.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=1, help="비결정성 흡수용 반복 횟수")
    ap.add_argument("--tag", default="", help="출력 파일 접미사 (다른 코퍼스 격리용)")
    args = ap.parse_args()

    from variants_distill import VARIANTS  # noqa: E402

    if args.variant not in VARIANTS:
        raise SystemExit(f"unknown variant: {args.variant} (있는 것: {list(VARIANTS)})")
    variant = VARIANTS[args.variant]

    from orthus.wiki import distill as distill_mod

    if variant.get("system"):
        distill_mod._SYSTEM = variant["system"]
    if variant.get("system_append"):
        distill_mod._SYSTEM = distill_mod._SYSTEM + variant["system_append"]
    if variant.get("json_shape"):
        distill_mod._JSON_SHAPE = variant["json_shape"]

    docs = [
        json.loads(line)
        for line in Path(args.sample).read_text(encoding="utf-8").splitlines()
        if line.strip() and line.strip() != "t"
    ]
    docs = [r for r in docs if isinstance(r, dict)]
    if args.limit:
        docs = docs[: args.limit]

    chat = _build_chat(args.model)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    _suffix = f"_{args.tag}" if args.tag else ""
    out_path = RAW_DIR / f"distill_{args.variant}_{args.model}{_suffix}.jsonl"
    done = _load_done(out_path)

    n_run = n_skip = n_err = 0
    with out_path.open("a", encoding="utf-8") as out:
        for rep in range(args.repeat):
            for doc in docs:
                key = (doc["doc_id"], rep)
                if key in done:
                    n_skip += 1
                    continue
                chat.reset_usage()
                t0 = time.monotonic()
                row = {
                    "variant": args.variant,
                    "model": args.model,
                    "rep": rep,
                    "doc_id": doc["doc_id"],
                    "bucket": doc["bucket"],
                    "source": doc["source"],
                    "len": doc["len"],
                }
                try:
                    data = distill_mod._complete_distill_json(
                        chat, doc["title"], doc["markdown"], UUID(doc["doc_id"])
                    )
                    row["parsed"] = _parse_claims_like_production(data, doc["title"])
                    row["raw"] = data
                except Exception as exc:  # 실패도 데이터다 (format-failure rate)
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    n_err += 1
                row["elapsed_s"] = round(time.monotonic() - t0, 2)
                row["usage"] = chat.usage_totals()
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                n_run += 1
                claims_n = len(row.get("parsed", {}).get("claims", [])) if "parsed" in row else "ERR"
                print(f"[{n_run}] {doc['doc_id'][:8]} b{doc['bucket']} claims={claims_n} {row['elapsed_s']}s")
    print(f"done: run={n_run} skip={n_skip} err={n_err} -> {out_path}")


if __name__ == "__main__":
    main()
