"""Shared wiki slug validation helpers."""

from __future__ import annotations

import re

WIKI_SLUG_RE = re.compile(r"^[\w가-힣/_-]{1,200}$")


def clean_wiki_slug(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    slug = value.strip()
    if not WIKI_SLUG_RE.fullmatch(slug):
        return None
    return slug
