from __future__ import annotations

import uuid
from datetime import datetime
from uuid import UUID

from orthus.federation import client as central_client
from orthus.models.base import ChatModel
from orthus.schemas.canonical import (
    AssistantResult,
    AssistantResultPart,
    ChatTurn,
    RoutedAnswer,
    ValidationResult,
    WikiAnswer,
    WikiSourceRef,
)
from orthus.settings import get_settings
from orthus.structured.query import query_structured
from orthus.wiki.qa import answer_from_hits
from orthus.wiki.retrieve import retrieve


def should_federate(scope: str) -> bool:
    settings = get_settings()
    return settings.node_kind == "personal" and scope == "all"


def federated_wiki_answer(
    user_id: UUID,
    question: str,
    *,
    k: int = 5,
    project: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    source: str | None = None,
    chat_model: ChatModel | None = None,
    context_wiki_slug: str | None = None,
    history: list[ChatTurn] | None = None,
) -> WikiAnswer:
    warnings: list[str] = []
    local_hits = retrieve(
        user_id,
        question,
        k=k,
        scope="personal",
        project=project,
        since=since,
        until=until,
        source=source,
    )
    central_hits: list[WikiSourceRef] = []
    try:
        central_hits = central_client.retrieve_company_wiki(
            user_id,
            question,
            k=k,
            project=project,
            since=since,
            until=until,
            source=source,
        )
    except central_client.CentralReadUnavailable as exc:
        warnings.append(str(exc))

    hits = _merge_wiki_hits([*local_hits, *central_hits], k=k)
    learn = not any(hit.source_scope == "company" for hit in hits)
    return answer_from_hits(
        user_id,
        question,
        hits,
        chat_model=chat_model,
        learn=learn,
        warnings=warnings,
        context_wiki_slug=context_wiki_slug,
        history=history,
    )


def federated_structured_answer(
    user_id: UUID,
    question: str,
    *,
    project: str | None = None,
    chat_model: ChatModel | None = None,
) -> RoutedAnswer:
    settings = get_settings()
    warnings: list[str] = []
    parts: list[AssistantResultPart] = []

    local_result = query_structured(
        user_id,
        question,
        scope="personal",
        project=project,
        chat_model=chat_model,
    )
    parts.append(
        AssistantResultPart(
            source_scope="personal",
            source_node_id=settings.node_id,
            result=local_result,
        )
    )

    central_result: AssistantResult | None = None
    try:
        central_result = central_client.query_company_structured(
            user_id,
            question,
            project=project,
        )
        parts.append(
            AssistantResultPart(
                source_scope="company",
                source_node_id="company",
                result=central_result,
            )
        )
    except central_client.CentralReadUnavailable as exc:
        warnings.append(str(exc))

    primary = _primary_structured_result(local_result, central_result)
    return RoutedAnswer(
        question=question,
        mode="structured",
        structured=primary,
        structured_parts=parts,
        warnings=warnings,
    )


def _merge_wiki_hits(hits: list[WikiSourceRef], *, k: int) -> list[WikiSourceRef]:
    merged: dict[tuple[str, str, str | None], WikiSourceRef] = {}
    for hit in hits:
        key = (hit.source_scope, hit.page_slug, hit.source_node_id)
        existing = merged.get(key)
        if existing is None or hit.score > existing.score:
            merged[key] = hit
    return sorted(
        merged.values(),
        key=lambda hit: (-hit.score, hit.source_scope, hit.page_slug),
    )[:k]


def _primary_structured_result(
    local_result: AssistantResult,
    central_result: AssistantResult | None,
) -> AssistantResult:
    candidates = [r for r in [central_result, local_result] if r is not None]
    if not candidates:
        return _empty_structured_result()
    return sorted(candidates, key=_structured_priority, reverse=True)[0]


def _structured_priority(result: AssistantResult) -> tuple[int, int, int]:
    row_count = result.row_count if result.row_count is not None else len(result.rows)
    executed = 1 if result.status == "executed" else 0
    has_rows = 1 if row_count > 0 else 0
    return (executed, has_rows, row_count)


def _empty_structured_result() -> AssistantResult:
    return AssistantResult(
        query_id=uuid.uuid4(),
        question="",
        compiled=None,
        validation=ValidationResult(rejected_reason="no_structured_result"),
        status="rejected",
        message="No structured result available.",
    )
