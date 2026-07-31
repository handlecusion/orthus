"""Read-only wiki relation projection for Agent Work responses."""

from __future__ import annotations

import re
from collections.abc import Callable
from uuid import UUID

from orthus.schemas.canonical import AgentWorkItem
from orthus.settings import Settings, get_settings
from orthus.wiki import store

_WIKI_SLUG_RE = re.compile(r"^[\w가-힣/_-]{1,200}$")
AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT = 500


def derive_wiki_slugs_for_item(
    item: AgentWorkItem,
    *,
    page_exists: Callable[[str], bool] | None = None,
) -> list[str]:
    """Project related wiki pages from existing Agent Work provenance.

    This creates no durable relation. Callers should pass a node-local
    `page_exists` resolver so unresolved claims/tasks/federated pages are
    dropped.
    """

    candidates: list[str] = []
    payload = item.payload if isinstance(item.payload, dict) else {}

    context = payload.get("context")
    if isinstance(context, dict):
        _append_slug(candidates, context.get("wiki_slug"))

    if item.source_kind == "wiki_task":
        _append_slug(candidates, item.source_ref_id)
        related = payload.get("related")
        if isinstance(related, list):
            for value in related:
                _append_slug(candidates, value)

    slugs = _dedupe(candidates)
    if page_exists is None:
        return slugs
    return [slug for slug in slugs if page_exists(slug)]


def enrich_agent_work_item(
    item: AgentWorkItem,
    *,
    user_id: UUID,
    settings: Settings | None = None,
) -> AgentWorkItem:
    settings = settings or get_settings()
    page_exists = _page_exists_for(user_id=user_id, settings=settings)
    return item.model_copy(
        update={"wiki_slugs": derive_wiki_slugs_for_item(item, page_exists=page_exists)}
    )


def enrich_agent_work_items(
    items: list[AgentWorkItem],
    *,
    user_id: UUID,
    settings: Settings | None = None,
) -> list[AgentWorkItem]:
    settings = settings or get_settings()
    page_exists = _page_exists_for(user_id=user_id, settings=settings)
    return [
        item.model_copy(
            update={"wiki_slugs": derive_wiki_slugs_for_item(item, page_exists=page_exists)}
        )
        for item in items
    ]


def _page_exists_for(
    *,
    user_id: UUID,
    settings: Settings,
) -> Callable[[str], bool]:
    scope = "personal" if settings.node_kind == "personal" else "company"
    owner_id = user_id if settings.node_kind == "personal" else None
    cache: dict[str, bool] = {}

    def exists(slug: str) -> bool:
        if slug not in cache:
            # 존재 판정 전용 — load_page(전체 read+parse) 대신 같은 _path 규칙의 stat만.
            cache[slug] = store.exists("page", slug, scope=scope, owner_id=owner_id)
        return cache[slug]

    return exists


def _append_slug(values: list[str], raw: object) -> None:
    if not isinstance(raw, str):
        return
    slug = raw.strip()
    if not slug or not _WIKI_SLUG_RE.fullmatch(slug):
        return
    values.append(slug)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
