"""Deterministically backfill low-confidence claims for source-only wiki entries."""

from __future__ import annotations

import argparse
import hashlib
from datetime import date
from uuid import UUID

from sqlalchemy import select

from orthus.db import session
from orthus.schemas.canonical import WikiClaim
from orthus.tables import documents
from orthus.wiki import store
from orthus.wiki.consolidate import consolidate
from orthus.wiki.distill import _headline_fallback
from orthus.wiki.slugs import kebab


def _claim_slug(page_slug: str, claim_text: str) -> str:
    normalized = " ".join(claim_text.strip().lower().split())
    h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{page_slug}-{h}"


def _doc_context(source_ref: str):
    try:
        doc_id = UUID(source_ref)
    except ValueError:
        return None
    with session() as s:
        return s.execute(
            select(
                documents.c.user_id,
                documents.c.scope,
                documents.c.project,
            ).where(documents.c.doc_id == doc_id)
        ).first()


def backfill_source_claims(*, scope: str = "company") -> dict[str, int]:
    totals = {"sources": 0, "backfilled": 0, "skipped": 0}
    today = date.today()
    for slug in store.list_slugs("source", scope=scope, owner_id=None):
        source = store.load_source(slug, scope=scope, owner_id=None)
        if source is None:
            totals["skipped"] += 1
            continue
        totals["sources"] += 1
        if source.key_claims or not source.summary.strip():
            totals["skipped"] += 1
            continue
        ctx = _doc_context(source.source_ref)
        if ctx is None:
            totals["skipped"] += 1
            continue
        user_id, doc_scope, project = ctx
        if doc_scope != scope:
            totals["skipped"] += 1
            continue

        page_seed = (source.key_concepts[:1] or source.terminology[:1] or [source.title])[0]
        page_slug = kebab(page_seed)
        claim = WikiClaim(
            slug=_claim_slug(page_slug, source.summary),
            claim=source.summary,
            supporting=[source.slug],
            conflicting=[],
            confidence="low",
            last_reviewed=today,
            related_pages=[page_slug],
            evidence=source.summary,
            # 결정론 헤드라인(이 백필은 LLM 미사용) — 그래프 slug 노출 방지.
            display_title=_headline_fallback(source.summary),
        )
        updated_source = source.model_copy(
            update={
                "key_claims": [claim.slug],
                "related_pages": [page_slug],
            }
        )
        consolidate(updated_source, [claim], user_id=user_id, scope=scope, project=project)
        totals["backfilled"] += 1
        if totals["backfilled"] % 100 == 0:
            print(f"[backfill] source claims {totals['backfilled']}/{totals['sources']}")
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill source summaries into wiki claims.")
    parser.add_argument("--scope", default="company")
    args = parser.parse_args()
    totals = backfill_source_claims(scope=args.scope)
    print("wiki backfill complete: " + ", ".join(f"{k}={v}" for k, v in totals.items()))


if __name__ == "__main__":
    main()
