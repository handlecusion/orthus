"""LLM wiki store: markdown SoR <-> Postgres index + pgvector embeddings.

Markdown files live under a scope-partitioned tree (P2.1, docs/architecture-v2.md §2):
  company:  `wiki-store/company/<sources|claims|wiki|tasks>/<slug>.md`
  personal: `wiki-store/personal/<owner_id>/<sources|claims|wiki|tasks>/<slug>.md`
The markdown files are the source of record (AGENTS.md / docs/llm-wiki.md §3).
Postgres mirrors them for search/graph/audit (`wiki_pages`, `wiki_chunks`,
`wiki_links`), with `wiki_pages.scope`/`owner_id` + `embeddings.scope` carrying the
tenant dimension so retrieval can isolate per-tenant content.

Codec design (dependency-free, no pyyaml):
- Frontmatter (between `---` fences) holds scalar-without-newline and short
  string-list fields: slug, title, enums, ISO dates/datetimes, bools, and
  list-of-ref/term strings. Every string scalar and list item is double-quoted
  with `"`/`\\` escaped so values containing `:` or quotes round-trip. Literal
  newlines in values are escaped as `\\n` so each field stays on one line.
  The `scope` (+ `owner_id` for personal) tenant fields are injected as the first
  frontmatter lines for human-readable round-trip; the partitioned path is the
  authoritative locator (see `_read_scope_meta`).
- Free-form PROSE fields live in the body under stable `## ` headings that mirror
  `wiki-store/templates/*.md`. `load_*` reconstructs the exact model from
  frontmatter + body.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update

from orthus.audit import audit
from orthus.audit.redact import redact_pii
from orthus.corpus.pipeline import chunk_markdown
from orthus.db import session
from orthus.kg.outbox import enqueue_kg_event
from orthus.models.registry import get_embedding_model
from orthus.schemas.canonical import (
    SCHEMA_VERSION,
    WikiClaim,
    WikiPage,
    WikiSource,
    WikiTask,
    WikiTaskResolution,
)
from orthus.settings import get_settings
from orthus.tables import embeddings, wiki_chunks, wiki_links, wiki_pages

# kind -> wiki-store subdirectory.
_KIND_DIR = {"source": "sources", "claim": "claims", "page": "wiki", "task": "tasks"}

# Per-model: ordered frontmatter (scalar/short-list) fields and prose-body fields.
# Prose fields map to a stable `## ` heading (mirrors templates/*.md).
_SOURCE_FRONT = [
    "slug",
    "title",
    "source_type",
    "source_ref",
    "ingested_at",
    "key_concepts",
    "key_claims",
    "terminology",
    "related_pages",
    "open_questions",
]
_SOURCE_PROSE = [("summary", "Concise Summary"), ("confidence_notes", "Confidence Notes")]

_CLAIM_FRONT = [
    "slug",
    "supporting",
    "conflicting",
    "confidence",
    "last_reviewed",
    "related_pages",
    "display_title",
]
_CLAIM_PROSE = [("claim", "Claim"), ("evidence", "Evidence"), ("notes", "Notes")]

_PAGE_FRONT = [
    "slug",
    "title",
    "relations",
    "evidence",
    # S2: competing moved from body to frontmatter so items with embedded newlines
    # round-trip losslessly via the newline-safe _quote/_unquote codec.
    "competing",
    "open_questions",
    "sources",
    "backlinks",
]
_PAGE_PROSE = [
    ("definition", "Concise Definition"),
    ("overview", "Conceptual Overview"),
]

_TASK_FRONT = [
    "slug",
    "kind",
    "related",
    "created_at",
    "resolved",
    "incoming_claim",
    "source_excerpt",
    "resolution_decision",
    "resolution_note",
    "resolution_resolved_by",
    "resolution_resolved_at",
    "resolution_produced_claim_slugs",
]
_TASK_PROSE = [("description", "Description")]

# S3: known heading sets per model kind, used by _parse_prose to distinguish
# template section boundaries from prose-embedded markdown headings.
_SOURCE_HEADINGS = frozenset(h for _, h in _SOURCE_PROSE)
_CLAIM_HEADINGS = frozenset(h for _, h in _CLAIM_PROSE)
_PAGE_HEADINGS = frozenset(h for _, h in _PAGE_PROSE)
_TASK_HEADINGS = frozenset(h for _, h in _TASK_PROSE)


# --- frontmatter scalar codec --------------------------------------------------


def _dump_scalar(value: object) -> str:
    """Serialize a single frontmatter scalar to a quoted/escaped string."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return _quote(value.isoformat())
    if isinstance(value, date):
        return _quote(value.isoformat())
    return _quote(str(value))


def _quote(s: str) -> str:
    # Escape backslash and double-quote first, then encode literal newlines as \n
    # so that any value — including multi-line prose in a list item — stays on one
    # frontmatter line and round-trips losslessly.
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    return '"' + s + '"'


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        body = token[1:-1]
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt == "n":
                    out.append("\n")
                else:
                    out.append(nxt)
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)
    return token


def _dump_list(items: list[str]) -> str:
    """Inline JSON-style list of quoted items; round-trips colons/quotes/newlines."""
    return "[" + ", ".join(_quote(str(x)) for x in items) + "]"


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return []
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    buf: list[str] = []
    in_str = False
    esc = False
    for ch in inner:
        if esc:
            buf.append(ch)
            esc = False
            continue
        if ch == "\\":
            buf.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            buf.append(ch)
            continue
        if ch == "," and not in_str:
            items.append(_unquote("".join(buf)))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        items.append(_unquote("".join(buf)))
    return items


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (raw frontmatter map of key -> unparsed value string, body)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if line.strip() and ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
        i += 1
    body = "\n".join(lines[i + 1 :]) if i < len(lines) else ""
    return fm, body.lstrip("\n")


def _front_value(fm: dict[str, str], key: str, kind: str) -> object:
    """Type-coerce a raw frontmatter value back into a model field."""
    raw = fm.get(key, "")
    if kind == "list":
        return _parse_list(raw)
    if kind == "bool":
        return _unquote(raw).lower() == "true"
    if kind == "date":
        return date.fromisoformat(_unquote(raw))
    if kind == "datetime":
        return datetime.fromisoformat(_unquote(raw))
    if kind == "opt_str":
        # 명시 `null`만 None으로 매핑한다(opt_str 공통 시맨틱을 좁게 유지 —
        # incoming_claim/resolution_note 등 기존 필드에 파급되지 않게). 신규 optional
        # 필드는 _dump_scalar(None)이 "null"을 쓰므로 None round-trip이 성립하고, 옛
        # 파일의 키 부재는 ""로 읽혀도 display_title 등은 falsy 처리라 안전하다.
        if raw.strip() == "null":
            return None
        return _unquote(raw)
    return _unquote(raw)


# --- tenant (scope/owner) frontmatter ------------------------------------------


def _inject_meta(text: str, scope: str, owner_id: UUID | None, project: str) -> str:
    """Prepend scope/owner_id/project frontmatter lines right after the opening `---`.

    Storage-layer metadata, not part of the canonical Wiki* models — the partitioned
    path is authoritative, this is for human-readable round-trip (prompt §4). `project`
    is a content tag (P2) carried for round-trip; the DB column is authoritative."""
    meta = [f"scope: {_dump_scalar(scope)}"]
    if owner_id is not None:
        meta.append(f"owner_id: {_dump_scalar(str(owner_id))}")
    meta.append(f"project: {_dump_scalar(project)}")
    if text.startswith("---\n"):
        return "---\n" + "\n".join(meta) + "\n" + text[len("---\n") :]
    return text


def _read_scope_meta(text: str) -> tuple[str, UUID | None, str]:
    """Parse (scope, owner_id, project) from frontmatter. Defaults company/None/atlas."""
    fm, _ = _split_frontmatter(text)
    scope = _unquote(fm["scope"]) if "scope" in fm else "company"
    owner_raw = fm.get("owner_id")
    owner_id = UUID(_unquote(owner_raw)) if owner_raw and owner_raw.strip() != "null" else None
    project = _unquote(fm["project"]) if "project" in fm else "atlas"
    return scope, owner_id, project


# --- body (prose) codec --------------------------------------------------------


def _dump_prose(prose: list[tuple[str, str]], values: dict[str, object], title: str) -> str:
    """Render prose sections under `# {title}` with stable `## ` headings."""
    parts = [f"# {title}", ""]
    for field, heading in prose:
        val = values.get(field)
        parts.append(f"## {heading}")
        parts.append("")
        if val is None:
            parts.append("(none)")
        else:
            parts.append(str(val))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _parse_prose(
    body: str, prose: list[tuple[str, str]], known_headings: frozenset[str]
) -> dict[str, object]:
    """Extract prose-field text from `## ` sections.

    S3: only treat a `## ` line as a section boundary when its heading text is in
    `known_headings` (the fixed template set for this model kind). Any other
    `## ...` line is treated as prose content within the current section.
    `(none)` -> None for optional fields.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.split("\n"):
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading in known_headings:
                current = heading
                sections[current] = []
                continue
        if current is not None:
            sections[current].append(line)
    out: dict[str, object] = {}
    for field, heading in prose:
        text = "\n".join(sections.get(heading, [])).strip()
        out[field] = None if text == "(none)" else text
    return out


# --- model <-> markdown --------------------------------------------------------


def _source_to_md(m: WikiSource) -> str:
    fm = {
        "slug": _dump_scalar(m.slug),
        "title": _dump_scalar(m.title),
        "source_type": _dump_scalar(m.source_type),
        "source_ref": _dump_scalar(m.source_ref),
        "ingested_at": _dump_scalar(m.ingested_at),
        "key_concepts": _dump_list(m.key_concepts),
        "key_claims": _dump_list(m.key_claims),
        "terminology": _dump_list(m.terminology),
        "related_pages": _dump_list(m.related_pages),
        "open_questions": _dump_list(m.open_questions),
    }
    front = "\n".join(f"{k}: {fm[k]}" for k in _SOURCE_FRONT)
    body = _dump_prose(
        _SOURCE_PROSE,
        {"summary": m.summary, "confidence_notes": m.confidence_notes},
        m.title,
    )
    return f"---\n{front}\n---\n\n{body}"


def _md_to_source(text: str) -> WikiSource:
    fm, body = _split_frontmatter(text)
    prose = _parse_prose(body, _SOURCE_PROSE, _SOURCE_HEADINGS)
    return WikiSource(
        slug=_front_value(fm, "slug", "str"),
        title=_front_value(fm, "title", "str"),
        source_type=_front_value(fm, "source_type", "str"),
        source_ref=_front_value(fm, "source_ref", "str"),
        ingested_at=_front_value(fm, "ingested_at", "datetime"),
        summary=prose["summary"] or "",
        key_concepts=_front_value(fm, "key_concepts", "list"),
        key_claims=_front_value(fm, "key_claims", "list"),
        terminology=_front_value(fm, "terminology", "list"),
        related_pages=_front_value(fm, "related_pages", "list"),
        open_questions=_front_value(fm, "open_questions", "list"),
        confidence_notes=prose["confidence_notes"],
    )


def _claim_to_md(m: WikiClaim) -> str:
    fm = {
        "slug": _dump_scalar(m.slug),
        "supporting": _dump_list(m.supporting),
        "conflicting": _dump_list(m.conflicting),
        "confidence": _dump_scalar(m.confidence),
        "last_reviewed": _dump_scalar(m.last_reviewed),
        "related_pages": _dump_list(m.related_pages),
        "display_title": _dump_scalar(m.display_title),
    }
    front = "\n".join(f"{k}: {fm[k]}" for k in _CLAIM_FRONT)
    body = _dump_prose(
        _CLAIM_PROSE,
        {"claim": m.claim, "evidence": m.evidence, "notes": m.notes},
        f"Claim: {m.slug}",
    )
    return f"---\n{front}\n---\n\n{body}"


def _md_to_claim(text: str) -> WikiClaim:
    fm, body = _split_frontmatter(text)
    prose = _parse_prose(body, _CLAIM_PROSE, _CLAIM_HEADINGS)
    return WikiClaim(
        slug=_front_value(fm, "slug", "str"),
        claim=prose["claim"] or "",
        supporting=_front_value(fm, "supporting", "list"),
        conflicting=_front_value(fm, "conflicting", "list"),
        confidence=_front_value(fm, "confidence", "str"),
        last_reviewed=_front_value(fm, "last_reviewed", "date"),
        related_pages=_front_value(fm, "related_pages", "list"),
        evidence=prose["evidence"] or "",
        notes=prose["notes"],
        # 명시 null·옛 파일의 키 부재("")를 모두 None으로 매핑(round-trip 안전). opt_str
        # 공통 시맨틱은 좁게(=="null") 두고, ""→None은 이 필드에서만 `or None`으로 처리한다.
        display_title=_front_value(fm, "display_title", "opt_str") or None,
    )


def _page_to_md(m: WikiPage) -> str:
    fm = {
        "slug": _dump_scalar(m.slug),
        "title": _dump_scalar(m.title),
        "relations": _dump_list(m.relations),
        "evidence": _dump_list(m.evidence),
        # S2: competing stored as frontmatter list (newline-safe via _quote/_unquote)
        # instead of body \n-join so multi-line items round-trip without fragmentation.
        "competing": _dump_list(m.competing),
        "open_questions": _dump_list(m.open_questions),
        "sources": _dump_list(m.sources),
        "backlinks": _dump_list(m.backlinks),
    }
    front = "\n".join(f"{k}: {fm[k]}" for k in _PAGE_FRONT)
    body = _dump_prose(
        _PAGE_PROSE,
        {"definition": m.definition, "overview": m.overview},
        m.title,
    )
    return f"---\n{front}\n---\n\n{body}"


def _md_to_page(text: str) -> WikiPage:
    fm, body = _split_frontmatter(text)
    prose = _parse_prose(body, _PAGE_PROSE, _PAGE_HEADINGS)
    return WikiPage(
        slug=_front_value(fm, "slug", "str"),
        title=_front_value(fm, "title", "str"),
        definition=prose["definition"] or "",
        overview=prose["overview"] or "",
        relations=_front_value(fm, "relations", "list"),
        evidence=_front_value(fm, "evidence", "list"),
        competing=_front_value(fm, "competing", "list"),
        open_questions=_front_value(fm, "open_questions", "list"),
        sources=_front_value(fm, "sources", "list"),
        backlinks=_front_value(fm, "backlinks", "list"),
    )


def _task_to_md(m: WikiTask) -> str:
    r = m.resolution
    fm = {
        "slug": _dump_scalar(m.slug),
        "kind": _dump_scalar(m.kind),
        "related": _dump_list(m.related),
        "created_at": _dump_scalar(m.created_at),
        "resolved": _dump_scalar(m.resolved),
        # WTR.1: 새 optional 필드 — None이면 null로 직렬화해 round-trip 유지.
        "incoming_claim": _dump_scalar(m.incoming_claim),
        "source_excerpt": _dump_scalar(m.source_excerpt),
        "resolution_decision": _dump_scalar(r.decision) if r else "null",
        "resolution_note": _dump_scalar(r.note) if r else "null",
        "resolution_resolved_by": _dump_scalar(str(r.resolved_by)) if r else "null",
        "resolution_resolved_at": _dump_scalar(r.resolved_at) if r else "null",
        "resolution_produced_claim_slugs": _dump_list(r.produced_claim_slugs) if r else "[]",
    }
    front = "\n".join(f"{k}: {fm[k]}" for k in _TASK_FRONT)
    body = _dump_prose(_TASK_PROSE, {"description": m.description}, f"Task: {m.slug}")
    return f"---\n{front}\n---\n\n{body}"


def _md_to_task(text: str) -> WikiTask:
    fm, body = _split_frontmatter(text)
    prose = _parse_prose(body, _TASK_PROSE, _TASK_HEADINGS)
    # WTR.1: resolution 필드 복원 — 키 없으면 None(기존 task 파일 하위호환).
    resolution: WikiTaskResolution | None = None
    raw_decision = fm.get("resolution_decision", "null").strip()
    if raw_decision and raw_decision != "null":
        from uuid import UUID as _UUID

        resolution = WikiTaskResolution(
            decision=_front_value(fm, "resolution_decision", "str"),
            note=_front_value(fm, "resolution_note", "opt_str"),
            resolved_by=_UUID(_front_value(fm, "resolution_resolved_by", "str")),
            resolved_at=_front_value(fm, "resolution_resolved_at", "datetime"),
            produced_claim_slugs=_front_value(fm, "resolution_produced_claim_slugs", "list"),
        )
    return WikiTask(
        slug=_front_value(fm, "slug", "str"),
        kind=_front_value(fm, "kind", "str"),
        description=prose["description"] or "",
        related=_front_value(fm, "related", "list"),
        created_at=_front_value(fm, "created_at", "datetime"),
        resolved=_front_value(fm, "resolved", "bool"),
        incoming_claim=_front_value(fm, "incoming_claim", "opt_str"),
        source_excerpt=_front_value(fm, "source_excerpt", "opt_str")
        if "source_excerpt" in fm
        else None,
        resolution=resolution,
    )


# --- redaction over prose fields -----------------------------------------------


def _redact_source(m: WikiSource) -> WikiSource:
    # S1: also redact free-text list fields (key_concepts, terminology,
    # open_questions) — these carry raw LLM output and can contain PII.
    # key_claims and related_pages are slug/ref identifiers — left unredacted.
    return m.model_copy(
        update={
            "summary": redact_pii(m.summary),
            "confidence_notes": redact_pii(m.confidence_notes)
            if m.confidence_notes is not None
            else None,
            "title": redact_pii(m.title),
            "key_concepts": [redact_pii(x) for x in m.key_concepts],
            "terminology": [redact_pii(x) for x in m.terminology],
            "open_questions": [redact_pii(x) for x in m.open_questions],
        }
    )


def _redact_claim(m: WikiClaim) -> WikiClaim:
    return m.model_copy(
        update={
            "claim": redact_pii(m.claim),
            "evidence": redact_pii(m.evidence),
            "notes": redact_pii(m.notes) if m.notes is not None else None,
            # display_title은 그래프에 저장돼 표시되는 짧은 라벨 — 다른 prose 필드와
            # 동일 PII redaction 정책(불변식 5: redaction 통과 라벨만 그래프 진입).
            "display_title": redact_pii(m.display_title) if m.display_title is not None else None,
        }
    )


def _redact_page(m: WikiPage) -> WikiPage:
    # S1: also redact open_questions (free-text LLM output).
    # relations, evidence, sources, backlinks are slug/ref identifiers — left unredacted.
    return m.model_copy(
        update={
            "title": redact_pii(m.title),
            "definition": redact_pii(m.definition),
            "overview": redact_pii(m.overview),
            "competing": [redact_pii(c) for c in m.competing],
            "open_questions": [redact_pii(q) for q in m.open_questions],
        }
    )


def _redact_task(m: WikiTask) -> WikiTask:
    # WTR.1: incoming_claim과 resolution.note도 PII redaction 대상(다른 task 필드와 동일 정책).
    r = m.resolution
    redacted_resolution = (
        r.model_copy(update={"note": redact_pii(r.note) if r.note is not None else None})
        if r is not None
        else None
    )
    return m.model_copy(
        update={
            "description": redact_pii(m.description),
            "incoming_claim": redact_pii(m.incoming_claim)
            if m.incoming_claim is not None
            else None,
            "source_excerpt": redact_pii(m.source_excerpt)
            if m.source_excerpt is not None
            else None,
            "resolution": redacted_resolution,
        }
    )


# --- path helpers --------------------------------------------------------------


def _store_root(root: Path | None) -> Path:
    return Path(root) if root is not None else get_settings().wiki_store_path


def _scope_subdir(scope: str, owner_id: UUID | None) -> Path:
    """Tenant partition under the store root: company/ or personal/<owner_id>/."""
    if scope == "personal":
        if owner_id is None:
            raise ValueError("personal scope requires owner_id")
        return Path("personal") / str(owner_id)
    return Path("company")


def _path(kind: str, slug: str, root: Path | None, scope: str, owner_id: UUID | None) -> Path:
    base = _store_root(root) / _scope_subdir(scope, owner_id)
    return base / _KIND_DIR[kind] / f"{slug}.md"


def _rel_path(kind: str, slug: str, scope: str, owner_id: UUID | None) -> str:
    return f"{_scope_subdir(scope, owner_id).as_posix()}/{_KIND_DIR[kind]}/{slug}.md"


def _body_of(text: str) -> str:
    """The prose body (after frontmatter), used for content_hash + embedding."""
    _, body = _split_frontmatter(text)
    return body


# --- Postgres mirror -----------------------------------------------------------


def _upsert_page_row(
    s,
    *,
    slug: str,
    kind: str,
    path: str,
    title: str,
    confidence: str | None,
    display_title: str | None,
    content_hash: str,
    scope: str,
    owner_id: UUID | None,
    project: str,
) -> UUID:
    # Identity is (slug, scope, owner_id) — a company page and a personal page may
    # share a slug without colliding (migration 0004 composite unique). `project` is
    # a content tag, not part of identity.
    existing = s.execute(
        select(wiki_pages.c.page_id).where(
            wiki_pages.c.slug == slug,
            wiki_pages.c.scope == scope,
            wiki_pages.c.owner_id.is_(owner_id)
            if owner_id is None
            else wiki_pages.c.owner_id == owner_id,
        )
    ).first()
    if existing is None:
        page_id = uuid.uuid4()
        s.execute(
            insert(wiki_pages).values(
                page_id=page_id,
                slug=slug,
                kind=kind,
                path=path,
                title=title,
                confidence=confidence,
                display_title=display_title,
                content_hash=content_hash,
                schema_version=SCHEMA_VERSION,
                scope=scope,
                project=project,
                owner_id=owner_id,
            )
        )
        return page_id
    page_id = existing[0]
    s.execute(
        update(wiki_pages)
        .where(wiki_pages.c.page_id == page_id)
        .values(
            kind=kind,
            path=path,
            title=title,
            confidence=confidence,
            # coalesce: 헤드라인이 없거나 빈 문자열인 재저작이 기존 백필값을 지우지
            # 않게 한다(None·"" 모두 falsy → 기존 값 유지). _headline_fallback이 빈
            # 입력에 ""를 낼 수 있어 falsy 가드가 필요하다(백필/저작 순서 무관 idempotency).
            display_title=(display_title if display_title else wiki_pages.c.display_title),
            content_hash=content_hash,
            project=project,
            updated_at=func.now(),
        )
    )
    return page_id


def _reindex_chunks(
    s,
    page_id: UUID,
    user_id: UUID,
    pieces: list[str],
    vectors: list,
    model_version: str | None,
    scope: str,
    project: str,
) -> int:
    """Drop prior wiki_chunks/embeddings for page_id, then store the precomputed
    chunk embeddings. Mirrors corpus.pipeline.index_document for idempotency.
    Embedding is done by the caller OUTSIDE this DB transaction so the slow
    embedding network call does not pin a pool connection. Returns chunk count.
    Embeddings are stamped with `scope`/`project` for tenant- and project-isolated
    retrieval (P2.1 / P2)."""
    old_emb_ids = [
        r[0]
        for r in s.execute(
            select(wiki_chunks.c.embedding_id).where(wiki_chunks.c.page_id == page_id)
        ).all()
        if r[0] is not None
    ]
    s.execute(delete(wiki_chunks).where(wiki_chunks.c.page_id == page_id))
    if old_emb_ids:
        s.execute(delete(embeddings).where(embeddings.c.embedding_id.in_(old_emb_ids)))

    for ordinal, (content, vec) in enumerate(zip(pieces, vectors)):
        emb_id = uuid.uuid4()
        s.execute(
            insert(embeddings).values(
                embedding_id=emb_id,
                user_id=user_id,
                kind="wiki_chunk",
                ref_id=page_id,
                vec=vec,
                meta={"ordinal": ordinal},
                schema_version=SCHEMA_VERSION,
                model_version=model_version,
                scope=scope,
                project=project,
            )
        )
        s.execute(
            insert(wiki_chunks).values(
                chunk_id=uuid.uuid4(),
                page_id=page_id,
                ordinal=ordinal,
                content=content,
                embedding_id=emb_id,
            )
        )
    return len(pieces)


def _replace_links(s, page_id: UUID, links: list[tuple[str, str]]) -> None:
    """Replace all wiki_links rows for src page_id with (dst_slug, rel) pairs.
    Dangling targets are allowed."""
    s.execute(delete(wiki_links).where(wiki_links.c.src_page_id == page_id))
    seen: set[tuple[str, str]] = set()
    for dst_slug, rel in links:
        if not dst_slug or (dst_slug, rel) in seen:
            continue
        seen.add((dst_slug, rel))
        s.execute(insert(wiki_links).values(src_page_id=page_id, dst_slug=dst_slug, rel=rel))


# --- public write API ----------------------------------------------------------
#
# Wiki is agent-internal, but `embeddings.user_id` carries an FK to `users`, so
# wiki_chunk embeddings are owned by the authoring user (same as the source doc).
# `scope`/`owner_id` add the tenant dimension: company pages have owner_id=None,
# personal pages are owned by `owner_id` (= the user).


def write_source(
    model: WikiSource,
    *,
    user_id: UUID,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
    project: str = "atlas",
) -> UUID:
    m = _redact_source(model)
    text = _source_to_md(m)
    return _persist(
        kind="source",
        slug=m.slug,
        title=m.title,
        confidence=None,
        display_title=None,
        text=text,
        embed_body=False,
        links=[(s, "derived_from") for s in m.related_pages]
        + [(c, "supports") for c in m.key_claims],
        user_id=user_id,
        root=root,
        scope=scope,
        owner_id=owner_id,
        project=project,
    )


def write_claim(
    model: WikiClaim,
    *,
    user_id: UUID,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
    project: str = "atlas",
) -> UUID:
    m = _redact_claim(model)
    text = _claim_to_md(m)
    return _persist(
        kind="claim",
        slug=m.slug,
        # title=slug는 claim 정체성 보존(불변). 사람이 읽는 헤드라인은 별도 display_title.
        title=m.slug,
        confidence=m.confidence,
        display_title=m.display_title,
        text=text,
        embed_body=True,
        links=[(s, "supports") for s in m.supporting]
        + [(c, "conflicts") for c in m.conflicting]
        + [(p, "backlink") for p in m.related_pages],
        user_id=user_id,
        root=root,
        scope=scope,
        owner_id=owner_id,
        project=project,
    )


def write_page(
    model: WikiPage,
    *,
    user_id: UUID,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
    project: str = "atlas",
) -> UUID:
    m = _redact_page(model)
    text = _page_to_md(m)
    return _persist(
        kind="page",
        slug=m.slug,
        title=m.title,
        confidence=None,
        display_title=None,
        text=text,
        embed_body=True,
        links=[(r, "derived_from") for r in m.relations]
        + [(e, "supports") for e in m.evidence]
        + [(s, "derived_from") for s in m.sources]
        + [(b, "backlink") for b in m.backlinks],
        user_id=user_id,
        root=root,
        scope=scope,
        owner_id=owner_id,
        project=project,
    )


def write_task(
    model: WikiTask,
    *,
    user_id: UUID,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
    project: str = "atlas",
) -> UUID:
    m = _redact_task(model)
    text = _task_to_md(m)
    return _persist(
        kind="task",
        slug=m.slug,
        title=m.slug,
        confidence=None,
        display_title=None,
        text=text,
        embed_body=False,
        links=[(r, "backlink") for r in m.related],
        user_id=user_id,
        root=root,
        scope=scope,
        owner_id=owner_id,
        project=project,
    )


def _existing_content_hash(slug: str, scope: str, owner_id: UUID | None) -> str | None:
    """기존 wiki_pages 행의 content_hash(없으면 None) — _persist 재임베딩 단락용."""
    with session() as s:
        row = s.execute(
            select(wiki_pages.c.content_hash).where(
                wiki_pages.c.slug == slug,
                wiki_pages.c.scope == scope,
                wiki_pages.c.owner_id.is_(owner_id)
                if owner_id is None
                else wiki_pages.c.owner_id == owner_id,
            )
        ).first()
    return row[0] if row else None


def _persist(
    *,
    kind: str,
    slug: str,
    title: str,
    confidence: str | None,
    display_title: str | None,
    text: str,
    embed_body: bool,
    links: list[tuple[str, str]],
    user_id: UUID,
    root: Path | None,
    scope: str,
    owner_id: UUID | None,
    project: str,
) -> UUID:
    text = _inject_meta(text, scope, owner_id, project)
    path = _path(kind, slug, root, scope, owner_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    body = _body_of(text)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    rel = _rel_path(kind, slug, scope, owner_id)

    # Skip re-chunk/re-embed when the body is unchanged. content_hash is body-only
    # (frontmatter excluded), so a metadata-only re-save — e.g. a display_title
    # headline backfill — reuses the existing chunks/embeddings instead of paying a
    # redundant embed call per page. Row metadata (title/display_title/links) and the
    # KG outbox event still update below.
    reuse_chunks = embed_body and _existing_content_hash(slug, scope, owner_id) == content_hash

    # Chunk + embed BEFORE opening the DB transaction so the embedding network
    # call does not pin a pool connection (mirrors corpus.pipeline.index_document).
    # Concurrent wiki authoring (e.g. dashboard reflect background tasks) otherwise
    # holds a connection per page across each embed and drains the pool, surfacing
    # as QueuePool timeouts on unrelated requests (auth, dashboard saves).
    pieces = chunk_markdown(body) if (embed_body and not reuse_chunks) else []
    if pieces:
        embedder = get_embedding_model()
        with audit("wiki.embed") as span:
            vectors = embedder.embed(pieces)
            span.add_meta(model_version=embedder.model_version, n_chunks=len(pieces))
        model_version = embedder.model_version
    else:
        vectors = []
        model_version = None

    with session() as s:
        page_id = _upsert_page_row(
            s,
            slug=slug,
            kind=kind,
            path=rel,
            title=title,
            confidence=confidence,
            display_title=display_title,
            content_hash=content_hash,
            scope=scope,
            owner_id=owner_id,
            project=project,
        )
        # Reindex only when the body changed: stores new chunks when embedded, clears
        # stale when embed_body is off. Unchanged body reuses existing chunks.
        if not reuse_chunks:
            _reindex_chunks(s, page_id, user_id, pieces, vectors, model_version, scope, project)
        _replace_links(s, page_id, links)
        # K3 KG outbox — 모든 wiki 쓰기의 단일 commit 지점이라 여기 1줄이면
        # source/claim/page/task 전부를 잡는다(kg-implementation-spec §5.2).
        enqueue_kg_event(
            s, entity_kind="wiki_page", entity_id=page_id, scope=scope, owner_id=owner_id
        )
        s.commit()
    return page_id


# --- public read API -----------------------------------------------------------


def exists(
    kind: str,
    slug: str,
    root: Path | None = None,
    *,
    scope: str = "company",
    owner_id: UUID | None = None,
) -> bool:
    return _path(kind, slug, root, scope, owner_id).is_file()


def list_slugs(
    kind: str,
    root: Path | None = None,
    *,
    scope: str = "company",
    owner_id: UUID | None = None,
) -> list[str]:
    d = _store_root(root) / _scope_subdir(scope, owner_id) / _KIND_DIR[kind]
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.md"))


def load_source(
    slug: str,
    *,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
) -> WikiSource | None:
    p = _path("source", slug, root, scope, owner_id)
    if not p.is_file():
        return None
    return _md_to_source(p.read_text(encoding="utf-8"))


def load_claim(
    slug: str,
    *,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
) -> WikiClaim | None:
    p = _path("claim", slug, root, scope, owner_id)
    if not p.is_file():
        return None
    return _md_to_claim(p.read_text(encoding="utf-8"))


def load_page(
    slug: str,
    *,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
) -> WikiPage | None:
    p = _path("page", slug, root, scope, owner_id)
    if not p.is_file():
        return None
    return _md_to_page(p.read_text(encoding="utf-8"))


def load_task(
    slug: str,
    *,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
) -> WikiTask | None:
    p = _path("task", slug, root, scope, owner_id)
    if not p.is_file():
        return None
    return _md_to_task(p.read_text(encoding="utf-8"))


def delete_item(
    kind: str,
    slug: str,
    *,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
) -> bool:
    """Delete one wiki markdown item and its Postgres mirror for this tenant.

    의도적으로 KG-비인지(no `enqueue_kg_event`)다 — `_persist`(쓰기)는 KG outbox에
    enqueue하지만 이 삭제 primitive는 하지 않는다. 현재 company page 삭제의 유일한
    경로는 `orthus.kg.erase`이고 그쪽이 `op='delete'` enqueue + `:Entity` detach를 소유한다
    (personal scope는 v1 그래프에 미투영이라 무관). **향후 새 company-scope page 삭제
    경로를 추가하면 KG 노드가 고아로 남으므로(다음 rebuild까지) 그 경로가 직접
    `op='delete'`를 enqueue하거나 erase 경유여야 한다**(K6 리뷰 C2)."""
    path = _path(kind, slug, root, scope, owner_id)
    had_file = path.is_file()
    with session() as s:
        row = s.execute(
            select(wiki_pages.c.page_id).where(
                wiki_pages.c.slug == slug,
                wiki_pages.c.scope == scope,
                wiki_pages.c.owner_id.is_(owner_id)
                if owner_id is None
                else wiki_pages.c.owner_id == owner_id,
            )
        ).first()
        if row is not None:
            page_id = row.page_id
            old_emb_ids = [
                r[0]
                for r in s.execute(
                    select(wiki_chunks.c.embedding_id).where(wiki_chunks.c.page_id == page_id)
                ).all()
                if r[0] is not None
            ]
            s.execute(delete(wiki_links).where(wiki_links.c.src_page_id == page_id))
            s.execute(delete(wiki_chunks).where(wiki_chunks.c.page_id == page_id))
            if old_emb_ids:
                s.execute(delete(embeddings).where(embeddings.c.embedding_id.in_(old_emb_ids)))
            s.execute(delete(wiki_pages).where(wiki_pages.c.page_id == page_id))
        s.commit()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return row is not None or had_file


# Public codec helpers (used by round-trip unit tests).
to_markdown = {
    "source": _source_to_md,
    "claim": _claim_to_md,
    "page": _page_to_md,
    "task": _task_to_md,
}
from_markdown = {
    "source": _md_to_source,
    "claim": _md_to_claim,
    "page": _md_to_page,
    "task": _md_to_task,
}
