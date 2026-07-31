"""Unit tests for orthus.wiki.slugs.source_slug_from_title uniqueness fix."""

from __future__ import annotations

import re
import uuid

from orthus.wiki.slug import WIKI_SLUG_RE
from orthus.wiki.slugs import source_slug_from_title

_DOC_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_DOC_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def test_source_slug_is_deterministic_and_unique_per_doc_id():
    # Same (title, doc_id) → same slug (deterministic)
    slug1 = source_slug_from_title("Release v2.0.0", _DOC_A)
    slug2 = source_slug_from_title("Release v2.0.0", _DOC_A)
    assert slug1 == slug2

    # Same title, different doc_id → different slugs
    slug_b = source_slug_from_title("Release v2.0.0", _DOC_B)
    assert slug1 != slug_b

    # Both start with the expected prefix
    assert slug1.startswith("src-release-v2-0-0-")
    assert slug_b.startswith("src-release-v2-0-0-")

    # All slugs satisfy WIKI_SLUG_RE
    for slug in (slug1, slug2, slug_b):
        assert WIKI_SLUG_RE.fullmatch(slug), f"slug {slug!r} fails WIKI_SLUG_RE"


def test_source_slug_accepts_string_doc_id():
    doc_id_str = str(_DOC_A)
    slug_uuid = source_slug_from_title("My Doc", _DOC_A)
    slug_str = source_slug_from_title("My Doc", doc_id_str)
    assert slug_uuid == slug_str


def test_source_slug_long_title_stays_within_200_chars():
    long_title = "A" * 300
    slug = source_slug_from_title(long_title, _DOC_A)
    assert len(slug) <= 200
    assert WIKI_SLUG_RE.fullmatch(slug)


def test_source_slug_shape():
    slug = source_slug_from_title("Release v2.0.0", _DOC_A)
    # shape: src-<kebab>-<8 hex chars>
    assert re.fullmatch(r"src-[a-z0-9가-힣-]+-[0-9a-f]{8}", slug), f"unexpected shape: {slug!r}"
