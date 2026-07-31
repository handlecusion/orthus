"""User-editable project assignment per Notion db_name (P2, docs/p2-fe-and-sources.md).

`set_project_override` is the apply-on-edit path the FE drives: it upserts the
override (so the choice survives reseeds — ingest reads `project_overrides` first)
AND re-tags the content already in the DB for that db_name, in one transaction, so
the change is visible immediately without waiting for a reseed.

Re-tag scope = structured(notion_rows) AND wiki/corpus (scope "B"):
  - notion_rows           WHERE db_name = X
  - documents             WHERE source_db_name = X
  - corpus_chunks         for those docs (doc_id join)
  - embeddings            kind='corpus_chunk', ref_id = those doc_ids
  - wiki_pages            1:1-traceable to X (see _retag_wiki) + their wiki_chunk embeddings

WIKI TRACEABILITY RULE + LIMIT (best-effort, DB-only):
  A wiki source page has slug `src-<kebab(doc.title)>-<doc_id8>` (see orthus.wiki.distill); a
  concept/claim page records where it came from via `wiki_links` (rel derived_from /
  supports) pointing at those source slugs. We compute the set S of source slugs for
  the docs with source_db_name = X, then re-tag:
    (a) the source pages whose slug ∈ S (1:1 to X by construction), and
    (b) any non-source page whose provenance source slugs are ALL ∈ S (and non-empty)
        — i.e. it derives solely from X.
  A page that AGGREGATES sources from multiple db_names is intentionally LEFT AS-IS:
  there is no single correct project for it here, and a full reseed re-resolves every
  page's project at author time. This keeps re-tag from mislabeling cross-db pages.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.connectors.project_map import PROJECTS, resolve_project
from orthus.db import session
from orthus.tables import (
    corpus_chunks,
    documents,
    embeddings,
    notion_rows,
    project_overrides,
    wiki_links,
    wiki_pages,
)
from orthus.wiki.slugs import source_slug_from_title as _source_slug_from_title

# Link relations a page/claim uses to record where its knowledge came from.
_PROVENANCE_RELS = ("derived_from", "supports")


def set_project_override(db_name: str, project: str) -> dict[str, int]:
    """Set the user override for `db_name` to `project` and re-tag existing content.

    Upserts `project_overrides` (so the choice persists across reseeds) and re-tags
    notion_rows / documents / their corpus chunks+embeddings / 1:1-traceable wiki
    pages+chunks for that db_name, all in one transaction. Returns per-store counts
    of rows updated. Raises ValueError on an invalid project."""
    if project not in PROJECTS:
        raise ValueError(f"invalid project {project!r}; must be one of {PROJECTS}")

    now = datetime.now(UTC)
    with session() as s:
        # 1) persist the override.
        ins = pg_insert(project_overrides).values(db_name=db_name, project=project, updated_at=now)
        s.execute(
            ins.on_conflict_do_update(
                index_elements=[project_overrides.c.db_name],
                set_={"project": ins.excluded.project, "updated_at": now},
            )
        )

        # 2) structured rows for this db_name.
        nr_res = s.execute(
            update(notion_rows).where(notion_rows.c.db_name == db_name).values(project=project)
        )

        # 3) documents originating from this db_name + their corpus chunks/embeddings.
        doc_ids = [
            r[0]
            for r in s.execute(
                select(documents.c.doc_id).where(documents.c.source_db_name == db_name)
            ).all()
        ]
        # updated_at bump 필수 — KG 증분 sync watermark가 updated_at 기반이라
        # 생략하면 재태깅이 다음 full kg-rebuild까지 그래프에 반영되지 않는다
        # (docs/kg-model.md §3).
        doc_res = s.execute(
            update(documents)
            .where(documents.c.source_db_name == db_name)
            .values(project=project, updated_at=func.now())
        )
        cc_count = 0
        cemb_count = 0
        if doc_ids:
            cc_count = s.execute(
                update(corpus_chunks)
                .where(corpus_chunks.c.doc_id.in_(doc_ids))
                .values(project=project)
            ).rowcount
            cemb_count = s.execute(
                update(embeddings)
                .where(
                    embeddings.c.kind == "corpus_chunk",
                    embeddings.c.ref_id.in_(doc_ids),
                )
                .values(project=project)
            ).rowcount

        # 4) wiki: re-tag 1:1-traceable pages + their wiki_chunk embeddings.
        wiki_counts = _retag_wiki(s, db_name, doc_ids, project)

        s.commit()

    return {
        "notion_rows": nr_res.rowcount,
        "documents": doc_res.rowcount,
        "corpus_chunks": cc_count,
        "corpus_embeddings": cemb_count,
        **wiki_counts,
    }


def _retag_wiki(s, db_name: str, doc_ids: list, project: str) -> dict[str, int]:
    """Re-tag wiki content 1:1-traceable to `db_name` (see module docstring rule).

    Returns {'wiki_pages', 'wiki_embeddings'} counts. `doc_ids` are the documents
    with source_db_name = db_name (used only to know whether any source even exists)."""
    if not doc_ids:
        return {"wiki_pages": 0, "wiki_embeddings": 0}

    # Source slugs for this db_name's docs: src-<kebab(title)>-<doc_id8> (orthus.wiki.distill).
    rows = s.execute(
        select(documents.c.title, documents.c.doc_id).where(documents.c.source_db_name == db_name)
    ).all()
    source_slugs = {_source_slug_from_title(title, doc_id) for title, doc_id in rows}
    if not source_slugs:
        return {"wiki_pages": 0, "wiki_embeddings": 0}

    # (a) the source pages themselves (1:1 to X by construction).
    page_ids: set = set(
        r[0]
        for r in s.execute(
            select(wiki_pages.c.page_id).where(
                wiki_pages.c.kind == "source",
                wiki_pages.c.slug.in_(source_slugs),
            )
        ).all()
    )

    # (b) non-source pages whose provenance source slugs are ALL ∈ source_slugs
    #     (and non-empty) — i.e. derived solely from this db_name. Pages that mix
    #     in another db_name's source are left as-is (cross-db; reseed reconciles).
    prov_rows = s.execute(
        select(wiki_links.c.src_page_id, wiki_links.c.dst_slug).where(
            wiki_links.c.rel.in_(_PROVENANCE_RELS),
            wiki_links.c.dst_slug.like("src-%"),
        )
    ).all()
    by_page: dict = {}
    for src_page_id, dst_slug in prov_rows:
        by_page.setdefault(src_page_id, set()).add(dst_slug)
    for pid, slugs in by_page.items():
        if slugs and slugs <= source_slugs:
            page_ids.add(pid)

    if not page_ids:
        return {"wiki_pages": 0, "wiki_embeddings": 0}

    page_id_list = list(page_ids)
    wp_count = s.execute(
        update(wiki_pages)
        .where(wiki_pages.c.page_id.in_(page_id_list))
        .values(project=project, updated_at=func.now())  # KG sync watermark (kg-model §3)
    ).rowcount
    wemb_count = s.execute(
        update(embeddings)
        .where(
            embeddings.c.kind == "wiki_chunk",
            embeddings.c.ref_id.in_(page_id_list),
        )
        .values(project=project)
    ).rowcount
    return {"wiki_pages": wp_count, "wiki_embeddings": wemb_count}


def clear_project_override(db_name: str) -> tuple[str, dict[str, int]]:
    """Remove the user override for `db_name` and revert content to auto-classification.

    Deletes the `project_overrides` row, recomputes the canonical project for every
    notion_rows row with that db_name using `resolve_project` (db_name heuristic), and
    re-tags notion_rows / documents / corpus chunks+embeddings / 1:1-traceable wiki pages
    in one transaction, matching the same scope as `set_project_override`.

    Returns (resolved_project, per-store update counts). The resolved project is derived
    from the db_name heuristic; if rows disagree (should not happen, all share db_name)
    the precedence nova > atlas > company is used."""
    # Recompute: all rows in this db_name share the same db_name heuristic result.
    resolved = resolve_project(db_name=db_name)

    with session() as s:
        # 1) Remove the override so future reseeds use the heuristic.
        s.execute(delete(project_overrides).where(project_overrides.c.db_name == db_name))

        # 2) Re-tag structured rows.
        nr_res = s.execute(
            update(notion_rows).where(notion_rows.c.db_name == db_name).values(project=resolved)
        )

        # 3) Documents + corpus chunks/embeddings.
        doc_ids = [
            r[0]
            for r in s.execute(
                select(documents.c.doc_id).where(documents.c.source_db_name == db_name)
            ).all()
        ]
        doc_res = s.execute(
            update(documents)
            .where(documents.c.source_db_name == db_name)
            .values(project=resolved, updated_at=func.now())  # KG sync watermark (kg-model §3)
        )
        cc_count = 0
        cemb_count = 0
        if doc_ids:
            cc_count = s.execute(
                update(corpus_chunks)
                .where(corpus_chunks.c.doc_id.in_(doc_ids))
                .values(project=resolved)
            ).rowcount
            cemb_count = s.execute(
                update(embeddings)
                .where(
                    embeddings.c.kind == "corpus_chunk",
                    embeddings.c.ref_id.in_(doc_ids),
                )
                .values(project=resolved)
            ).rowcount

        # 4) Wiki: re-tag 1:1-traceable pages + their wiki_chunk embeddings.
        wiki_counts = _retag_wiki(s, db_name, doc_ids, resolved)

        s.commit()

    return resolved, {
        "notion_rows": nr_res.rowcount,
        "documents": doc_res.rowcount,
        "corpus_chunks": cc_count,
        "corpus_embeddings": cemb_count,
        **wiki_counts,
    }
