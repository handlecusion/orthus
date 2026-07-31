"""Classification logic for the personal-board wiki purge (scripts/wiki).

Guards the two decisions that make the cleanup safe: parsing the board day out of
a source_external_id, and telling a pure daily-journal page (delete) apart from a
page that folded a board claim together with other knowledge (skip, review by hand).
Store access is monkeypatched — no DB, no wiki-store on disk."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from orthus.schemas.canonical import WikiPage, WikiSource
from scripts.wiki import purge_personal_board_wiki as purge


def _source(slug: str, source_ref: str, key_claims: list[str]) -> WikiSource:
    return WikiSource(
        slug=slug,
        title=slug,
        source_type="corpus_doc",
        source_ref=source_ref,
        ingested_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
        summary="",
        key_claims=key_claims,
    )


def _page(slug: str, backlinks: list[str]) -> WikiPage:
    return WikiPage(slug=slug, title=slug, definition="", overview="", backlinks=backlinks)


def test_parse_source_external_id():
    p = purge._parse_source_external_id(
        "personal_board:daily:personal-a:11111111-1111-1111-1111-111111111111:2026-07-14"
    )
    assert p == ("personal-a", "11111111-1111-1111-1111-111111111111", date(2026, 7, 14))
    # non-board / malformed / bad date all reject
    assert purge._parse_source_external_id("notion:page:abc") is None
    assert purge._parse_source_external_id("personal_board:daily:n:u:not-a-date") is None
    assert purge._parse_source_external_id(None) is None


def test_build_plan_deletes_pure_journal_skips_mixed(monkeypatch):
    owner = uuid4()
    doc_map = {"doc-1": ("personal-a", str(owner), date(2026, 7, 14))}

    sources = {
        "src-board": _source("src-board", "doc-1", ["c1", "c2"]),  # board-derived
        "src-other": _source("src-other", "doc-x", ["c9"]),  # unrelated source
    }
    pages = {
        "pg-journal": _page("pg-journal", ["c1", "c2"]),  # pure board → delete
        "pg-mixed": _page("pg-mixed", ["c1", "c9"]),  # board + other → skip
        "pg-unrelated": _page("pg-unrelated", ["c9"]),  # no board claim → ignore
    }

    def fake_list_slugs(kind, *, scope, owner_id):
        assert scope == "personal" and owner_id == owner
        return list(sources) if kind == "source" else list(pages)

    monkeypatch.setattr(purge.store, "list_slugs", fake_list_slugs)
    monkeypatch.setattr(
        purge.store, "load_source", lambda slug, *, scope, owner_id: sources[slug]
    )
    monkeypatch.setattr(purge.store, "load_page", lambda slug, *, scope, owner_id: pages[slug])

    plan = purge.build_plan(owner, doc_map)

    assert plan.source_slugs == ["src-board"]
    assert plan.claim_slugs == {"c1", "c2"}
    assert plan.journal_page_slugs == ["pg-journal"]
    assert plan.mixed_page_slugs == ["pg-mixed"]  # never auto-deleted
    assert plan.days == {("personal-a", str(owner), date(2026, 7, 14))}
