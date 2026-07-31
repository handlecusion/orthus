"""Purge stale personal-board journal artifacts from the LLM wiki (owner-scoped).

WHY
---
The personal board's daily journal used to distill its ``## 할 일``(tasks) and
``## 일정``(events) into wiki claims/pages. Those live-board items froze into the
wiki as claims ("현장결제 공지는 미완료 할 일로 기록됐다") and surfaced in
``wiki_search``/``wiki_ask``, so an old journal todo answered "오늘 할 일" instead of
the live board (the 2026-07-14 misroute). The code fix
(``personal_board._daily_wiki_markdown``) stops NEW todo/event claims, but
``consolidate`` is additive — it never deletes a claim a source stopped producing —
so already-authored claims/pages must be removed explicitly. That is this script.

WHAT IT DOES  (per owner, personal scope only — company wiki is never touched)
-----------------------------------------------------------------------------
1. Finds every ``personal_board`` source document (``documents`` table).
2. Finds the wiki artifacts they authored: ``WikiSource`` rows whose ``source_ref``
   is a personal_board ``doc_id``, their ``key_claims``, and the pure daily-journal
   ``WikiPage`` rows (``backlinks`` claim slugs ⊆ those board claims).
3. Deletes those claims + journal pages + sources via ``store.delete_item``.
4. (default) Re-runs ``sync_day_to_wiki`` for each affected board day so any durable
   ``특이사항`` notes are re-authored as notes-only knowledge. Todo-only days are
   simply gone.

Pages that only PARTIALLY overlap board claims (a journal claim folded together with
some other personal knowledge) are reported and SKIPPED, never deleted — reconcile
those by hand.

SAFETY
------
- Dry-run by default; prints the plan and deletes nothing. Pass ``--apply`` to write.
- ``--owner <uuid>`` limits to one owner; otherwise every owner that has a
  personal_board doc is processed.
- ``--no-regen`` skips step 4 (notes are then not restored — only use if you know
  there are no durable notes worth keeping).
- Operates on whatever DB + wiki-store the loaded node env points at. Run it on the
  node that HOLDS the personal board/wiki (see docs/personal-board-wiki-cleanup.md).

Usage:
    # dry-run, all owners
    uv run python -m scripts.wiki.purge_personal_board_wiki
    # dry-run, one owner
    uv run python -m scripts.wiki.purge_personal_board_wiki --owner <uuid>
    # apply
    uv run python -m scripts.wiki.purge_personal_board_wiki --owner <uuid> --apply
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import select

from orthus.db import session
from orthus.tables import documents
from orthus.wiki import store


@dataclass
class OwnerPlan:
    owner_id: UUID
    board_doc_ids: set[str] = field(default_factory=set)
    source_slugs: list[str] = field(default_factory=list)
    claim_slugs: set[str] = field(default_factory=set)
    journal_page_slugs: list[str] = field(default_factory=list)
    mixed_page_slugs: list[str] = field(default_factory=list)  # skipped, report only
    days: set[tuple[str, str, date]] = field(default_factory=set)  # (node_id, user_id, day)


def _load_board_docs(owner: UUID | None) -> dict[UUID, dict[str, tuple[str, str, date]]]:
    """{owner_id: {doc_id_str: (node_id, user_id, day)}} for personal_board docs."""
    stmt = select(
        documents.c.doc_id, documents.c.user_id, documents.c.source_external_id
    ).where(documents.c.source == "personal_board", documents.c.scope == "personal")
    if owner is not None:
        stmt = stmt.where(documents.c.user_id == owner)
    out: dict[UUID, dict[str, tuple[str, str, date]]] = defaultdict(dict)
    with session() as s:
        for doc_id, user_id, ext_id in s.execute(stmt).all():
            parsed = _parse_source_external_id(ext_id)
            out[user_id][str(doc_id)] = parsed  # parsed may be None; handled by caller
    return out


def _parse_source_external_id(ext_id: str | None) -> tuple[str, str, date] | None:
    # personal_board:daily:<node_id>:<user_id>:<YYYY-MM-DD>
    if not ext_id:
        return None
    parts = ext_id.split(":")
    if len(parts) != 5 or parts[0] != "personal_board" or parts[1] != "daily":
        return None
    node_id, user_id, day_s = parts[2], parts[3], parts[4]
    try:
        return node_id, user_id, date.fromisoformat(day_s)
    except ValueError:
        return None


def build_plan(owner_id: UUID, doc_map: dict[str, tuple[str, str, date] | None]) -> OwnerPlan:
    plan = OwnerPlan(owner_id=owner_id, board_doc_ids=set(doc_map))
    for parsed in doc_map.values():
        if parsed is not None:
            plan.days.add(parsed)

    # sources authored from a personal_board doc → collect their claim slugs.
    for src_slug in store.list_slugs("source", scope="personal", owner_id=owner_id):
        src = store.load_source(src_slug, scope="personal", owner_id=owner_id)
        if src is not None and src.source_ref in plan.board_doc_ids:
            plan.source_slugs.append(src_slug)
            plan.claim_slugs.update(src.key_claims)

    # pages: a page's `backlinks` are the claim slugs folded into it (consolidate
    # `_seed_page`/fold). A pure journal page's claims are ALL board claims → delete;
    # partial overlap → report + skip (folded together with other knowledge).
    for pg_slug in store.list_slugs("page", scope="personal", owner_id=owner_id):
        pg = store.load_page(pg_slug, scope="personal", owner_id=owner_id)
        if pg is None:
            continue
        page_claims = set(pg.backlinks)
        if not page_claims or not (page_claims & plan.claim_slugs):
            continue
        if page_claims <= plan.claim_slugs:
            plan.journal_page_slugs.append(pg_slug)
        else:
            plan.mixed_page_slugs.append(pg_slug)
    return plan


def print_plan(plan: OwnerPlan) -> None:
    print(f"\n=== owner {plan.owner_id} ===")
    print(f"  personal_board source docs : {len(plan.board_doc_ids)}")
    print(f"  wiki sources to delete     : {len(plan.source_slugs)}")
    print(f"  claims to delete           : {len(plan.claim_slugs)}")
    print(f"  journal pages to delete    : {len(plan.journal_page_slugs)}")
    print(f"  board days to re-sync      : {len(plan.days)}")
    if plan.mixed_page_slugs:
        print(f"  MIXED pages (SKIPPED, review by hand): {len(plan.mixed_page_slugs)}")
        for slug in plan.mixed_page_slugs:
            print(f"    - {slug}")
    for slug in sorted(plan.claim_slugs)[:20]:
        print(f"    claim: {slug}")
    if len(plan.claim_slugs) > 20:
        print(f"    … +{len(plan.claim_slugs) - 20} more claims")


def apply_plan(plan: OwnerPlan, *, regen: bool) -> None:
    for slug in plan.claim_slugs:
        store.delete_item("claim", slug, scope="personal", owner_id=plan.owner_id)
    for slug in plan.journal_page_slugs:
        store.delete_item("page", slug, scope="personal", owner_id=plan.owner_id)
    for slug in plan.source_slugs:
        store.delete_item("source", slug, scope="personal", owner_id=plan.owner_id)
    print(
        f"  deleted {len(plan.claim_slugs)} claims / "
        f"{len(plan.journal_page_slugs)} pages / {len(plan.source_slugs)} sources"
    )
    if not regen:
        return
    # Re-author durable notes (notes-only markdown; todo-only days author nothing).
    from orthus.personal_board import sync_day_to_wiki

    for node_id, user_id, day in sorted(plan.days):
        try:
            sync_day_to_wiki(UUID(user_id), node_id, day)
        except Exception as exc:  # noqa: BLE001 — keep going; report at the end
            print(f"  ! re-sync failed for {user_id} {day}: {type(exc).__name__}: {exc}")
    print(f"  re-synced {len(plan.days)} board days (notes-only)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", type=UUID, default=None, help="limit to one owner_id (UUID)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--no-regen", action="store_true", help="skip notes-only re-sync (step 4)")
    args = ap.parse_args()

    by_owner = _load_board_docs(args.owner)
    if not by_owner:
        print("no personal_board documents found for the given filter — nothing to do.")
        return

    print(f"mode: {'APPLY' if args.apply else 'DRY-RUN'} · owners: {len(by_owner)}")
    for owner_id, doc_map in by_owner.items():
        plan = build_plan(owner_id, doc_map)
        print_plan(plan)
        if args.apply:
            apply_plan(plan, regen=not args.no_regen)

    if not args.apply:
        print("\nDRY-RUN only — re-run with --apply to delete. Back up wiki-store + DB first.")


if __name__ == "__main__":
    main()
