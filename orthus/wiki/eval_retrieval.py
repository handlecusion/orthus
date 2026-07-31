"""Evaluate wiki retrieval against real-use query fixtures."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from orthus.models.registry import get_embedding_model
from orthus.wiki.retrieve import retrieve


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    question: str
    expected_slug: str
    project: str | None = None
    source: str | None = None


# 대회용 공개 빌드: 내부 지식이 담긴 golden 케이스는 배포하지 않는다. 자기
# 노드의 위키에 맞는 케이스를 JSON으로 만들어 `--cases`로 넘긴다(형식은
# EvalCase 필드와 동일). 이 하네스가 Solar embedding-passage 채택 근거를 만든
# 측정 도구다(experiments/fugu-ko/embedding/README.md).
DEFAULT_CASES: tuple[EvalCase, ...] = ()


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate wiki retrieval hit rates.")
    parser.add_argument("--scope", default="company")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--cases", type=Path, default=None, help="optional JSON case file")
    parser.add_argument("--out", type=Path, default=None, help="write detailed JSON result")
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args()

    cases = _load_cases(args.cases) if args.cases else list(DEFAULT_CASES)
    result = evaluate_retrieval(cases, scope=args.scope, k=args.k)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = result["summary"]
        print(
            "retrieval eval complete: "
            f"cases={summary['cases']} hit@1={summary['hit_at_1']:.3f} "
            f"hit@{args.k}={summary['hit_at_k']:.3f} mrr={summary['mrr']:.3f} "
            f"model={summary['model_version']}"
        )
        for row in result["cases"]:
            status = "PASS" if row["hit_at_k"] else "MISS"
            print(
                f"{status} {row['case_id']}: rank={row['rank']} "
                f"expected={row['expected_slug']} top={row['top_slug']}"
            )


def evaluate_retrieval(
    cases: list[EvalCase],
    *,
    scope: str = "company",
    k: int = 5,
) -> dict:
    user_id = uuid4()
    rows = []
    reciprocal_sum = 0.0
    for case in cases:
        hits = retrieve(
            user_id,
            case.question,
            k=k,
            scope=scope,
            project=case.project,
            source=case.source,
        )
        slugs = [hit.page_slug for hit in hits]
        rank = _rank(slugs, case.expected_slug)
        if rank is not None:
            reciprocal_sum += 1.0 / rank
        rows.append(
            {
                **asdict(case),
                "rank": rank,
                "hit_at_1": rank == 1,
                "hit_at_k": rank is not None and rank <= k,
                "top_slug": slugs[0] if slugs else None,
                "hits": [
                    {
                        "rank": i,
                        "slug": hit.page_slug,
                        "kind": hit.kind,
                        "title": hit.title,
                        "score": hit.score,
                        "excerpt": hit.excerpt[:240],
                    }
                    for i, hit in enumerate(hits, start=1)
                ],
            }
        )
    n = len(cases) or 1
    summary = {
        "cases": len(cases),
        "hit_at_1": sum(1 for r in rows if r["hit_at_1"]) / n,
        "hit_at_k": sum(1 for r in rows if r["hit_at_k"]) / n,
        "mrr": reciprocal_sum / n,
        "scope": scope,
        "k": k,
        "model_version": get_embedding_model().model_version,
    }
    return {"summary": summary, "cases": rows}


def _rank(slugs: list[str], expected_slug: str) -> int | None:
    for i, slug in enumerate(slugs, start=1):
        if expected_slug in slug:
            return i
    return None


def _load_cases(path: Path) -> list[EvalCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("case file must contain a JSON list")
    return [EvalCase(**item) for item in data]


if __name__ == "__main__":
    main()
