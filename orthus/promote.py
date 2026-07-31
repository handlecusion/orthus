"""Personal→central publish/promote gate.

Personal content never auto-syncs into central. The only transfer shape is:
personal export package -> central staging -> explicit approval -> company import.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update

from orthus.audit import audit
from orthus.audit.redact import redact_pii_text
from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.schemas.canonical import (
    InternalDocument,
    PromotionAuditEvent,
    PromotionPackage,
    PromotionStage,
)
from orthus.settings import Settings, get_settings
from orthus.tables import audit_log, documents, promote_staging

_PROMOTE_AUDIT_NODE_ORDER = {
    "promote.export": 0,
    "promote.stage": 1,
    "promote.approve": 2,
    "promote.reject": 2,
}

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]
_ALLOWED_PROJECTS = {"atlas", "nova", "orbit", "company"}


def export_personal_document(
    doc_id: UUID,
    user_id: UUID,
    settings: Settings | None = None,
) -> PromotionPackage:
    """Build sanitized transfer package from a personal-node document."""
    settings = settings or get_settings()
    if settings.node_kind != "personal":
        raise ValueError("promotion export is personal-node only")

    with audit("promote.export") as span:
        with session() as s:
            row = s.execute(
                select(
                    documents.c.doc_id,
                    documents.c.user_id,
                    documents.c.title,
                    documents.c.markdown,
                    documents.c.source,
                    documents.c.source_external_id,
                    documents.c.source_db_name,
                    documents.c.source_last_edited_at,
                    documents.c.scope,
                    documents.c.project,
                    documents.c.updated_at,
                ).where(
                    documents.c.doc_id == doc_id,
                    documents.c.user_id == user_id,
                    documents.c.scope == "personal",
                )
            ).first()
        if row is None:
            raise KeyError(f"personal document not found: {doc_id}")

        title, title_sanitization = _sanitize_text_with_summary(row.title)
        markdown, markdown_sanitization = _sanitize_text_with_summary(row.markdown)
        if not markdown.strip():
            raise ValueError("promotion export requires non-empty sanitized markdown")

        source_meta = _sanitize_meta(
            {
                "source": row.source,
                "source_external_id": row.source_external_id,
                "source_db_name": row.source_db_name,
                "source_last_edited_at": _iso(row.source_last_edited_at),
                "source_updated_at": _iso(row.updated_at),
                "project": row.project,
            }
        )
        source_meta["export_sanitization"] = {
            "title": title_sanitization,
            "markdown": markdown_sanitization,
        }
        package = PromotionPackage(
            source_node_id=settings.node_id,
            source_doc_id=row.doc_id,
            source_owner_id=row.user_id,
            source_scope="personal",
            source_title=title,
            sanitized_title=title,
            sanitized_markdown=markdown,
            source_meta=source_meta,
            target_project=_target_project(row.project),
        )
        span.add_meta(
            source_node_id=package.source_node_id,
            source_doc_id=str(package.source_doc_id),
            target_project=package.target_project,
        )
        return package


def stage_promotion_package(
    package: PromotionPackage,
    user_id: UUID,
    settings: Settings | None = None,
) -> PromotionStage:
    """Store sanitized package in central staging. Does not import content."""
    settings = settings or get_settings()
    if settings.node_kind != "company":
        raise ValueError("promotion staging is company-node only")

    package = _normalize_package(package)
    stage_id = uuid.uuid4()
    with audit("promote.stage") as span:
        with session() as s:
            s.execute(
                insert(promote_staging).values(
                    stage_id=stage_id,
                    source_node_id=package.source_node_id,
                    source_doc_id=package.source_doc_id,
                    source_owner_id=package.source_owner_id,
                    source_scope=package.source_scope,
                    source_title=package.source_title,
                    sanitized_title=package.sanitized_title,
                    sanitized_markdown=package.sanitized_markdown,
                    source_meta={
                        **package.source_meta,
                        "target_project": package.target_project,
                    },
                    status="pending",
                    created_by=user_id,
                )
            )
            s.commit()
        span.add_meta(stage_id=str(stage_id), source_node_id=package.source_node_id)
    return get_promotion_stage(stage_id, settings=settings)


def list_promotion_stages(
    *,
    status: str | None = None,
    settings: Settings | None = None,
) -> list[PromotionStage]:
    settings = settings or get_settings()
    if settings.node_kind != "company":
        raise ValueError("promotion staging list is company-node only")
    stmt = select(promote_staging).order_by(promote_staging.c.created_at.desc())
    if status is not None:
        stmt = stmt.where(promote_staging.c.status == status)
    with session() as s:
        rows = s.execute(stmt).all()
    row_dicts = [dict(row._mapping) for row in rows]
    events = _audit_events_by_stage_id([row["stage_id"] for row in row_dicts])
    return [_stage_from_row(row, audit_events=events.get(row["stage_id"], [])) for row in row_dicts]


def get_promotion_stage(
    stage_id: UUID,
    *,
    settings: Settings | None = None,
) -> PromotionStage:
    settings = settings or get_settings()
    if settings.node_kind != "company":
        raise ValueError("promotion staging is company-node only")
    with session() as s:
        row = s.execute(
            select(promote_staging).where(promote_staging.c.stage_id == stage_id)
        ).first()
    if row is None:
        raise KeyError(f"promotion stage not found: {stage_id}")
    events = _audit_events_by_stage_id([stage_id])
    return _stage_from_row(dict(row._mapping), audit_events=events.get(stage_id, []))


def approve_promotion_stage(
    stage_id: UUID,
    user_id: UUID,
    settings: Settings | None = None,
) -> PromotionStage:
    """Import one pending staged package into company corpus/wiki."""
    settings = settings or get_settings()
    if settings.node_kind != "company":
        raise ValueError("promotion approval is company-node only")

    with audit("promote.approve") as span:
        stage = get_promotion_stage(stage_id, settings=settings)
        if stage.status != "pending":
            raise ValueError(f"promotion stage is not pending: {stage.status}")

        source_external_id = f"promote:{stage.source_node_id}:{stage.source_doc_id}"
        doc = InternalDocument(
            title=stage.sanitized_title,
            markdown=stage.sanitized_markdown,
            source="promoted_personal",
            source_external_id=source_external_id,
            source_last_edited_at=datetime.now(UTC),
            project=stage.target_project,
        )
        promoted_doc_id, _changed = upsert_source_document(user_id, doc, scope="company")

        with session() as s:
            s.execute(
                update(promote_staging)
                .where(promote_staging.c.stage_id == stage_id)
                .values(
                    status="approved",
                    approved_by=user_id,
                    promoted_doc_id=promoted_doc_id,
                    updated_at=datetime.now(UTC),
                    decided_at=datetime.now(UTC),
                )
            )
            s.commit()
        span.add_meta(stage_id=str(stage_id), promoted_doc_id=str(promoted_doc_id))
    return get_promotion_stage(stage_id, settings=settings)


def reject_promotion_stage(
    stage_id: UUID,
    user_id: UUID,
    settings: Settings | None = None,
) -> PromotionStage:
    settings = settings or get_settings()
    if settings.node_kind != "company":
        raise ValueError("promotion rejection is company-node only")

    with audit("promote.reject") as span:
        stage = get_promotion_stage(stage_id, settings=settings)
        if stage.status != "pending":
            raise ValueError(f"promotion stage is not pending: {stage.status}")
        with session() as s:
            s.execute(
                update(promote_staging)
                .where(promote_staging.c.stage_id == stage_id)
                .values(
                    status="rejected",
                    approved_by=user_id,
                    updated_at=datetime.now(UTC),
                    decided_at=datetime.now(UTC),
                )
            )
            s.commit()
        span.add_meta(stage_id=str(stage_id), rejected_by=str(user_id))
    return get_promotion_stage(stage_id, settings=settings)


def _normalize_package(package: PromotionPackage) -> PromotionPackage:
    title, title_sanitization = _sanitize_text_with_summary(
        package.sanitized_title or package.source_title
    )
    markdown, markdown_sanitization = _sanitize_text_with_summary(package.sanitized_markdown)
    if not markdown.strip():
        raise ValueError("promotion package requires non-empty sanitized markdown")
    source_title, source_title_sanitization = _sanitize_text_with_summary(
        package.source_title or title
    )
    source_meta = _sanitize_meta(package.source_meta)
    source_meta["central_sanitization"] = {
        "source_title": source_title_sanitization,
        "sanitized_title": title_sanitization,
        "markdown": markdown_sanitization,
    }
    return PromotionPackage(
        source_node_id=_sanitize_text(package.source_node_id).strip(),
        source_doc_id=package.source_doc_id,
        source_owner_id=package.source_owner_id,
        source_scope="personal",
        source_title=source_title,
        sanitized_title=title or "Untitled promotion",
        sanitized_markdown=markdown,
        source_meta=source_meta,
        target_project=_target_project(package.target_project),
    )


def _stage_from_row(
    row: dict[str, Any],
    *,
    audit_events: list[PromotionAuditEvent] | None = None,
) -> PromotionStage:
    meta = dict(row["source_meta"] or {})
    return PromotionStage(
        stage_id=row["stage_id"],
        source_node_id=row["source_node_id"],
        source_doc_id=row["source_doc_id"],
        source_owner_id=row["source_owner_id"],
        source_scope=row["source_scope"],
        source_title=row["source_title"],
        sanitized_title=row["sanitized_title"],
        sanitized_markdown=row["sanitized_markdown"],
        source_meta=meta,
        target_project=_target_project(
            str(meta.get("target_project") or meta.get("project") or "")
        ),
        status=row["status"],
        created_by=row["created_by"],
        approved_by=row["approved_by"],
        promoted_doc_id=row["promoted_doc_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        decided_at=row["decided_at"],
        audit_events=audit_events or [],
    )


def _audit_events_by_stage_id(
    stage_ids: list[UUID],
) -> dict[UUID, list[PromotionAuditEvent]]:
    if not stage_ids:
        return {}
    stage_id_texts = {str(stage_id) for stage_id in stage_ids}
    with session() as s:
        rows = s.execute(
            select(
                audit_log.c.audit_id,
                audit_log.c.node_run_id,
                audit_log.c.node,
                audit_log.c.phase,
                audit_log.c.meta,
                audit_log.c.error_class,
                audit_log.c.error_message,
                audit_log.c.occurred_at,
            )
            .where(
                audit_log.c.node.in_(
                    ["promote.export", "promote.stage", "promote.approve", "promote.reject"]
                ),
                audit_log.c.phase.in_(["exit", "error"]),
                audit_log.c.meta["stage_id"].as_string().in_(stage_id_texts),
            )
            .order_by(audit_log.c.occurred_at.asc(), audit_log.c.audit_id.asc())
        ).all()

    out: dict[UUID, list[PromotionAuditEvent]] = {stage_id: [] for stage_id in stage_ids}
    for row in rows:
        meta = dict(row.meta or {})
        stage_id_raw = meta.get("stage_id")
        try:
            stage_id = UUID(str(stage_id_raw))
        except (TypeError, ValueError):
            continue
        if stage_id not in out:
            continue
        out[stage_id].append(
            PromotionAuditEvent(
                audit_id=row.audit_id,
                node_run_id=row.node_run_id,
                node=row.node,
                phase=row.phase,
                status="error" if row.phase == "error" or row.error_class else "ok",
                occurred_at=row.occurred_at,
                meta=meta,
                error_class=row.error_class,
                error_message=row.error_message,
            )
        )
    for events in out.values():
        events.sort(
            key=lambda event: (
                _PROMOTE_AUDIT_NODE_ORDER.get(event.node, 99),
                event.occurred_at,
                str(event.audit_id),
            )
        )
    return out


def _sanitize_meta(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {str(k): _sanitize_meta(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_sanitize_meta(v) for v in value]
    return value


def _sanitize_text(text: str) -> str:
    return _sanitize_text_with_summary(text)[0]


def _sanitize_text_with_summary(text: str) -> tuple[str, dict[str, Any]]:
    normalized = text.replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    out = normalized
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    secret_redacted = out
    out = redact_pii_text(secret_redacted)
    return out, {
        "input_chars": len(text),
        "normalized_chars": len(normalized),
        "sanitized_chars": len(out),
        "removed_chars": max(0, len(text) - len(out)),
        "changed": text != out,
        "whitespace_normalized": text != normalized,
        "secret_markers": out.count("[REDACTED_SECRET]"),
        "pii_changed": secret_redacted != out,
    }


def _target_project(value: str | None) -> str:
    return value if value in _ALLOWED_PROJECTS else "company"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
