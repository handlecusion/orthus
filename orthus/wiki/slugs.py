"""Shared deterministic wiki slug helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from uuid import UUID

_SLUG_RE = re.compile(r"[^a-z0-9가-힣]+")


def kebab(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "untitled"


def source_slug_from_title(title: str, doc_id: Union["UUID", str]) -> str:
    short = str(doc_id).replace("-", "")[:8]
    return f"src-{kebab(title)[:180]}-{short}"
