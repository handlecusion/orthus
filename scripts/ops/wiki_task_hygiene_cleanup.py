#!/usr/bin/env python
"""Resolve low-signal generated WikiTasks.

Dry-run by default. Pass --apply with --resolved-by after taking a node snapshot.
The script never deletes task files; it marks generated low-signal tasks resolved
with a resolution note so the cleanup is auditable and reversible by reopening.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import pathlib
from pathlib import Path
from uuid import UUID

from orthus.schemas.canonical import WikiTask, WikiTaskResolution
from sqlalchemy import select

from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import documents
from orthus.wiki import store
from orthus.wiki.task_hygiene import (
    MAX_OPEN_QUESTIONS_PER_SOURCE,
    claims_equivalent_for_conflict,
    is_structured_row_source,
    normalize_open_question,
    open_question_hygiene_reason,
)


@dataclass(frozen=True)
class TaskRef:
    scope: str
    owner_id: UUID | None
    task: WikiTask


@dataclass(frozen=True)
class CleanupCandidate:
    ref: TaskRef
    reason: str
    decision: str = "dismissed"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    root = Path(args.root or settings.wiki_store_path)
    refs = list(_iter_tasks(root, scope=args.scope, owner_id=args.owner_id))
    candidates = _cleanup_candidates(refs)

    counts = Counter(candidate.reason for candidate in candidates)
    print(f"wiki_store={root}")
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'} scope={args.scope}")
    print(f"tasks_scanned={len(refs)} cleanup_candidates={len(candidates)}")
    for reason, count in counts.most_common():
        print(f"  {reason}: {count}")
    for candidate in candidates[: args.show]:
        task = candidate.ref.task
        owner = str(candidate.ref.owner_id) if candidate.ref.owner_id else "-"
        print(
            f"  sample {candidate.reason} scope={candidate.ref.scope} owner={owner} "
            f"slug={task.slug} desc={task.description[:120]!r}"
        )

    if not args.apply:
        print("dry-run only; pass --apply --resolved-by <uuid> to write")
        return 0

    resolved_by = UUID(args.resolved_by) if args.resolved_by else None
    if resolved_by is None:
        raise SystemExit("--resolved-by is required with --apply")
    backup_dir = Path(args.backup_dir) if args.backup_dir else _default_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    applied = 0
    for candidate in candidates:
        _backup_task(root, backup_dir, candidate.ref)
        _resolve_candidate(root, candidate, resolved_by=resolved_by)
        applied += 1
    print(f"applied={applied} backup_dir={backup_dir}")
    return 0


def _cleanup_candidates(refs: list[TaskRef]) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    generated_open: dict[tuple[str, UUID | None, str], list[TaskRef]] = defaultdict(list)

    for ref in refs:
        task = ref.task
        if task.resolved:
            continue
        if task.kind == "conflict":
            reason = _false_conflict_reason(task)
            if reason:
                candidates.append(CleanupCandidate(ref=ref, reason=reason))
            continue
        if task.kind != "open_question" or not _is_generated_source_task(task):
            continue
        source_slug = task.related[0]
        if _is_structured_row_open_question(task):
            candidates.append(
                CleanupCandidate(ref=ref, reason="open_question:structured_row_source")
            )
            continue
        reason = open_question_hygiene_reason(task.description, source_slug=source_slug)
        if reason:
            candidates.append(CleanupCandidate(ref=ref, reason=f"open_question:{reason}"))
            continue
        generated_open[(ref.scope, ref.owner_id, source_slug)].append(ref)

    for group in generated_open.values():
        seen: set[str] = set()
        kept = 0
        for ref in sorted(group, key=lambda item: (item.task.created_at, item.task.slug)):
            key = normalize_open_question(ref.task.description)
            if key in seen:
                candidates.append(CleanupCandidate(ref=ref, reason="open_question:duplicate"))
                continue
            seen.add(key)
            kept += 1
            if kept > MAX_OPEN_QUESTIONS_PER_SOURCE:
                candidates.append(
                    CleanupCandidate(ref=ref, reason="open_question:excess_per_source")
                )

    return sorted(
        candidates,
        key=lambda item: (
            item.ref.scope,
            str(item.ref.owner_id) if item.ref.owner_id else "",
            item.reason,
            item.ref.task.slug,
        ),
    )


def _iter_tasks(root: Path, *, scope: str, owner_id: str | None) -> list[TaskRef]:
    refs: list[TaskRef] = []
    if scope in {"company", "all"}:
        for slug in store.list_slugs("task", root=root, scope="company"):
            task = store.load_task(slug, root=root, scope="company")
            if task:
                refs.append(TaskRef(scope="company", owner_id=None, task=task))
    if scope in {"personal", "all"}:
        owner_dirs = []
        if owner_id:
            owner_dirs = [UUID(owner_id)]
        else:
            personal_root = root / "personal"
            if personal_root.is_dir():
                owner_dirs = [
                    UUID(path.name)
                    for path in sorted(personal_root.iterdir())
                    if path.is_dir() and _looks_uuid(path.name)
                ]
        for owner in owner_dirs:
            for slug in store.list_slugs("task", root=root, scope="personal", owner_id=owner):
                task = store.load_task(slug, root=root, scope="personal", owner_id=owner)
                if task:
                    refs.append(TaskRef(scope="personal", owner_id=owner, task=task))
    return refs


def _is_generated_source_task(task: WikiTask) -> bool:
    return (
        task.slug.startswith("open-question-src-")
        and len(task.related) >= 1
        and task.related[0].startswith("src-")
    )


def _is_structured_row_open_question(task: WikiTask) -> bool:
    """True when the task's originating source document is a structured DB row.

    Looks up task.related[0] (the source slug) in the wiki-store sources dir to
    read `source_ref` (the document UUID), then queries the DB for source /
    source_db_name to call is_structured_row_source. Returns False on any lookup
    failure so the dry-run never drops tasks it can't confirm.
    """
    if not task.related:
        return False
    source_slug = task.related[0]
    return _source_slug_is_structured_row(source_slug)


_source_row_cache: dict[str, bool] = {}


def _source_slug_is_structured_row(source_slug: str) -> bool:
    """Cached structured-row check for a source slug using wiki-store + DB."""
    if source_slug in _source_row_cache:
        return _source_row_cache[source_slug]
    result = _lookup_source_slug_structured(source_slug)
    _source_row_cache[source_slug] = result
    return result


def _lookup_source_slug_structured(source_slug: str) -> bool:
    """Load the source markdown to get source_ref (doc UUID), then query DB."""
    from orthus.settings import get_settings as _get_settings
    from orthus.wiki import store as _store

    settings = _get_settings()
    wiki_root = pathlib.Path(settings.wiki_store_path)
    # Try company scope first, then all personal owners.
    for scope in ["company"]:
        src = _store.load_source(source_slug, root=wiki_root, scope=scope)
        if src is not None:
            return _check_doc_structured(src.source_ref)
    personal_root = wiki_root / "personal"
    if personal_root.is_dir():
        for owner_dir in sorted(personal_root.iterdir()):
            if not owner_dir.is_dir() or not _looks_uuid(owner_dir.name):
                continue
            try:
                owner_id = UUID(owner_dir.name)
            except ValueError:
                continue
            src = _store.load_source(
                source_slug, root=wiki_root, scope="personal", owner_id=owner_id
            )
            if src is not None:
                return _check_doc_structured(src.source_ref)
    return False


def _check_doc_structured(source_ref: str) -> bool:
    """Query the DB documents table for source / source_db_name."""
    try:
        from uuid import UUID as _UUID

        doc_id = _UUID(source_ref)
    except (ValueError, AttributeError):
        return False
    try:
        with session() as s:
            row = s.execute(
                select(documents.c.source, documents.c.source_db_name).where(
                    documents.c.doc_id == doc_id
                )
            ).first()
        if row is None:
            return False
        return is_structured_row_source(row[0], row[1])
    except Exception:
        return False


def _false_conflict_reason(task: WikiTask) -> str | None:
    description = task.description
    parsed = _extract_existing_incoming(description)
    if parsed and claims_equivalent_for_conflict(parsed[0], parsed[1]):
        return "conflict:false_positive_normalized"
    return None


def _extract_existing_incoming(description: str) -> tuple[str, str] | None:
    markers = [
        ("existing:", "; incoming:", ". Not overwritten"),
        ("기존 내용:", "; 새 내용:", ". 기존 유지"),
    ]
    for left, middle, right in markers:
        if left not in description or middle not in description:
            continue
        before, _, rest = description.partition(left)
        del before
        existing, _, rest = rest.partition(middle)
        incoming, _, _tail = rest.partition(right)
        return (_strip_repr(existing), _strip_repr(incoming))
    return None


def _strip_repr(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _resolve_candidate(root: Path, candidate: CleanupCandidate, *, resolved_by: UUID) -> None:
    task = candidate.ref.task
    updated = task.model_copy(
        update={
            "resolved": True,
            "resolution": WikiTaskResolution(
                decision=candidate.decision,
                note=f"wiki_task_hygiene:{candidate.reason}",
                resolved_by=resolved_by,
                resolved_at=datetime.now(UTC),
                produced_claim_slugs=[],
            ),
        }
    )
    store.write_task(
        updated,
        user_id=resolved_by,
        root=root,
        scope=candidate.ref.scope,
        owner_id=candidate.ref.owner_id,
        project="company",
    )


def _backup_task(root: Path, backup_dir: Path, ref: TaskRef) -> None:
    src = _task_path(root, ref)
    if not src.is_file():
        return
    if ref.scope == "company":
        dst = backup_dir / "company" / "tasks" / src.name
    else:
        assert ref.owner_id is not None
        dst = backup_dir / "personal" / str(ref.owner_id) / "tasks" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _task_path(root: Path, ref: TaskRef) -> Path:
    if ref.scope == "company":
        return root / "company" / "tasks" / f"{ref.task.slug}.md"
    assert ref.owner_id is not None
    return root / "personal" / str(ref.owner_id) / "tasks" / f"{ref.task.slug}.md"


def _default_backup_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / ".orthus" / "backups" / f"wiki-task-hygiene-{stamp}"


def _looks_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="wiki-store root; defaults to current ORTHUS_WIKI_STORE")
    parser.add_argument("--scope", choices=["company", "personal", "all"], default="all")
    parser.add_argument("--owner-id", help="limit personal cleanup to one owner UUID")
    parser.add_argument("--apply", action="store_true", help="mark candidates resolved")
    parser.add_argument("--resolved-by", help="operator/service UUID recorded in resolution")
    parser.add_argument("--backup-dir", help="backup task markdown here before --apply")
    parser.add_argument("--show", type=int, default=20, help="sample rows to print")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
