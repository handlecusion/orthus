"""Consolidate: claims/open questions -> pages/tasks. Fully deterministic (no LLM).

Merges atomic claims into concept pages and, per AGENTS.md, NEVER silently
overwrites a conflicting claim — it raises a WikiTask(kind="conflict") instead.

Write order (so the full set is persisted): source -> non-conflicting claims ->
pages (upsert) -> tasks."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from orthus.audit import audit
from orthus.schemas.canonical import WikiClaim, WikiPage, WikiSource, WikiTask
from orthus.wiki import store
from orthus.wiki.task_hygiene import claims_equivalent_for_conflict, filter_generated_open_questions


def _merge_unique(existing: list[str], extra: list[str]) -> list[str]:
    out = list(existing)
    for x in extra:
        if x not in out:
            out.append(x)
    return out


def _task_slug(prefix: str, text: str) -> str:
    h = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{h}"


def _task_related_for_source(source: WikiSource) -> list[str]:
    # Keep source first: resolve/cleanup code historically treats related[0] as
    # the generated task's source hint. Page and claim slugs after it give
    # reviewers enough wiki context without changing that contract.
    return _merge_unique([source.slug], source.related_pages + source.key_claims)


# Page overview accumulation (deterministic — AGENTS.md §7: consolidate orchestrates,
# the LLM only extracts). Before this, `overview` was written ONCE at seed time from
# the first claim's evidence and never updated again: the fold below merged only
# evidence/sources/backlinks, so a page that had 20 substantive claims folded into it
# still rendered ONE sentence as its body. Since `wiki_chunks` indexes the page body,
# every later claim's substance was invisible to retrieval at page granularity, and
# pages (which outrank claims in `_merge_candidates`) answered with whatever the first
# claim happened to say — usually a meta description (prod 2026-07-16: `ai-영상관련툴`
# read "AI 영상관련툴에 대한 순위를 정리한 문서입니다." twice, while the real tool list
# sat in claims the page never showed). The overview is now the folded claims' text,
# bounded so hub pages cannot grow an unbounded chunk.
_OVERVIEW_MAX_CLAIMS = 20
_OVERVIEW_MAX_CHARS = 2000


def _overview_items(overview: str) -> list[str]:
    """Parse an overview body back into its claim lines (bullet or legacy prose)."""
    items: list[str] = []
    for line in (overview or "").splitlines():
        text = line.strip()
        if not text:
            continue
        items.append(text[2:].strip() if text.startswith("- ") else text)
    return items


def _merge_overview(existing: str, claims: list[WikiClaim]) -> str:
    """Fold claim texts into the page overview: dedupe by text, cap count + chars.

    Legacy single-sentence overviews survive as the first item, so a page written
    before this change keeps its prose until its claims are re-folded.
    """
    items = _overview_items(existing)
    for claim in claims:
        text = (claim.claim or "").strip()
        if text and text not in items:
            items.append(text)
    kept: list[str] = []
    total = 0
    for item in items[:_OVERVIEW_MAX_CLAIMS]:
        total += len(item) + 3  # "- " + newline
        if total > _OVERVIEW_MAX_CHARS:
            break
        kept.append(item)
    return "\n".join(f"- {item}" for item in kept)


def _seed_page(page_slug: str, claim: WikiClaim, source: WikiSource) -> WikiPage:
    """Create a fresh page seeded from a claim (definition from claim text)."""
    title = page_slug.replace("-", " ").title() or page_slug
    return WikiPage(
        slug=page_slug,
        title=title,
        definition=claim.claim,
        overview=_merge_overview("", [claim]),
        relations=[],
        evidence=[claim.slug],
        competing=[],
        open_questions=[],
        sources=[source.slug],
        backlinks=[claim.slug],
    )


def consolidate(
    source: WikiSource,
    claims: list[WikiClaim],
    *,
    user_id: UUID,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
    project: str = "atlas",
) -> tuple[list[WikiPage], list[WikiTask]]:
    """Persist source + claims and fold claims into pages. Conflicts -> tasks.

    `scope`/`owner_id` tag every written wiki page for tenant isolation (P2.1) and
    select the company/personal directory in the store. Existing-page/claim/task
    lookups (conflict detection, page folding) are scope-aware so a personal author
    never reads or merges another tenant's pages. `project` tags every written page
    with its company→project bucket (P2) — it is a content tag, not part of the
    (slug, scope, owner_id) store identity, so conflict detection is unaffected.

    CONFLICT RULE (no silent overwrite): emit a WikiTask(kind="conflict") and SKIP
    overwriting an existing claim when EITHER
      (a) a claim's `conflicting` references a claim slug already in the store, OR
      (b) a claim with the same slug already exists with DIFFERENT `claim` text.
    The pre-existing claim is left untouched."""
    with audit("wiki.consolidate") as span:
        tasks: list[WikiTask] = []
        conflict_skipped: set[str] = set()
        now = datetime.now(UTC)

        for question in filter_generated_open_questions(source.slug, source.open_questions):
            question = question.strip()
            if not question:
                continue
            tasks.append(
                WikiTask(
                    slug=_task_slug(f"open-question-{source.slug}", question),
                    kind="open_question",
                    description=question,
                    related=_task_related_for_source(source),
                    created_at=now,
                    source_excerpt=(source.summary or "").strip()[:500] or None,
                )
            )

        # 1) detect conflicts first (no writes yet, so detection sees prior state).
        for c in claims:
            # (b) same slug, different text already on disk (same scope/owner).
            existing = store.load_claim(c.slug, root=root, scope=scope, owner_id=owner_id)
            if (
                existing is not None
                and existing.claim.strip() != c.claim.strip()
                and not claims_equivalent_for_conflict(existing.claim, c.claim)
            ):
                conflict_skipped.add(c.slug)
                tasks.append(
                    WikiTask(
                        slug=f"conflict-{c.slug}",
                        kind="conflict",
                        description=(
                            f"'{c.slug}' claim에 기존과 다른 내용이 들어와 덮어쓰지 않았습니다. "
                            f"기존 내용: {existing.claim!r}; 새 내용: {c.claim!r}. "
                            "기존 유지·새 내용으로 교체·병합 중에서 검토해 주세요."
                        ),
                        related=[c.slug],
                        created_at=now,
                        # WTR.1: use_incoming write-back을 위해 incoming 텍스트 구조 저장.
                        incoming_claim=c.claim,
                    )
                )
            # (a) explicit conflict reference to an existing claim slug.
            # Intentional asymmetry: an explicit conflicting-reference always emits a
            # task, but the incoming claim (which has a DISTINCT slug) is still written
            # in step 3 below — only the same-slug-different-text case (b) skips the
            # write. This preserves both facts while flagging the tension for review.
            for other in c.conflicting:
                if store.exists("claim", other, root=root, scope=scope, owner_id=owner_id):
                    tasks.append(
                        WikiTask(
                            slug=f"conflict-{c.slug}-{other}",
                            kind="conflict",
                            description=(
                                f"'{c.slug}' claim이 기존 claim '{other}'과(와) 충돌합니다. "
                                "consolidate 전에 해결해 주세요."
                            ),
                            related=[c.slug, other],
                            created_at=now,
                        )
                    )

        # 2) write source.
        store.write_source(
            source, user_id=user_id, root=root, scope=scope, owner_id=owner_id, project=project
        )

        # 3) write non-conflicting claims.
        written_claims = [c for c in claims if c.slug not in conflict_skipped]
        for c in written_claims:
            store.write_claim(
                c, user_id=user_id, root=root, scope=scope, owner_id=owner_id, project=project
            )

        # 4) fold claims into pages (only claims actually written contribute).
        pages_by_slug: dict[str, WikiPage] = {}
        for c in written_claims:
            for page_slug in c.related_pages:
                page = pages_by_slug.get(page_slug)
                if page is None:
                    page = store.load_page(
                        page_slug, root=root, scope=scope, owner_id=owner_id
                    ) or _seed_page(page_slug, c, source)
                page = page.model_copy(
                    update={
                        # The page body must carry every folded claim's substance, not
                        # just the seed claim's (see _merge_overview). Deterministic and
                        # idempotent: re-folding the same claim dedupes by text.
                        "overview": _merge_overview(page.overview, [c]),
                        "evidence": _merge_unique(page.evidence, [c.slug]),
                        "sources": _merge_unique(page.sources, [source.slug]),
                        "backlinks": _merge_unique(page.backlinks, [c.slug]),
                    }
                )
                pages_by_slug[page_slug] = page

        pages = list(pages_by_slug.values())
        for page in pages:
            store.write_page(
                page, user_id=user_id, root=root, scope=scope, owner_id=owner_id, project=project
            )

        # 5) write tasks.
        for t in tasks:
            store.write_task(
                t, user_id=user_id, root=root, scope=scope, owner_id=owner_id, project=project
            )

        span.add_meta(
            n_pages=len(pages),
            n_tasks=len(tasks),
            n_claims_written=len(written_claims),
            n_conflicts=len(conflict_skipped),
        )
    return pages, tasks
