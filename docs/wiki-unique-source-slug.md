# Wiki Source Slug Uniqueness — Design

Status: design (read-only). Author: architect. Date: 2026-06-11.
Scope: fix per-document SOURCE slug collisions in the LLM wiki self-author
pipeline so that **every corpus document maps to exactly one wiki source
page**, while keeping **claim/page slugs content-derived and shared** (the
consolidation merge semantics that make many sources merge into one wiki
PAGE must not change).

This document is the implementation plan for an executor. Code is not
modified here.

---

## 1. The bug, in one paragraph (already diagnosed)

`distill_document` (`orthus/wiki/distill.py:153`) derives a source slug
purely from the document title via `source_slug_from_title(title)`
(`orthus/wiki/slugs.py:15-16`), which returns `src-<kebab(title)>`. Two
documents with near-identical titles (`v2.0.0 (2026-04-20)` ×6,
`(Untitled)` ×3, per-person actors-list pages, mail "Gmail: [GitHub] Claude
is requesting" ×2, Claude session "Claude: …agent-safari/8942b3b1" ×3)
collide on the same source slug. The store keys the markdown file by slug
alone (`store._path` at `orthus/wiki/store.py:510-514`), and the Postgres
identity is `(slug, scope, owner_id)` with `NULLS NOT DISTINCT` (migration
`0004_tenant_scope.py:39-42`, mirror at `orthus/tables.py:300-307`). So each
later distill **overwrites the previous source page**, only one `doc_id`
survives in the source's `source_ref`, and the next compile cycle's
"authored?" probe — `_authored_source_refs` at `orthus/wiki/author.py:117-123`
— sees only one doc_id and treats the other colliding docs as unauthored.
They are re-distilled on every cycle: wasted LLM compute (forever) and, for
genuinely-distinct same-title docs, lost coverage in the wiki.

This is exactly the bug; we are not re-diagnosing it. The design below
fixes it without disturbing the claim/page convergence layer.

---

## 2. Invariants we must preserve (PR-rejecting if violated)

1. **Claim and concept-page slugs stay content-derived and shared.** Many
   sources legitimately merge into one wiki PAGE; `consolidate` folds
   `claim.related_pages` into pages and seeds page-by-slug. Do not touch
   `_claim_slug`, `_kebab` for pages, `_seed_page`, or the `pages_by_slug`
   merge loop in `orthus/wiki/consolidate.py:34-167`.
2. **Markdown remains SoR; Postgres is the index.** Source markdown files
   under `wiki-store/{company,personal/<owner>}/sources/<slug>.md` stay
   authoritative; DB `wiki_pages` mirrors them (`store._persist` at
   `orthus/wiki/store.py:779-836`).
3. **The grounding/answer path is unchanged.** Retrieval already excludes
   `kind == "source"` rows from answer surfacing
   (`orthus/wiki/retrieve.py:484` excludes source-kind pages when expanding
   contaminated-provenance; the WikiSourceRef payload returns `page`/`claim`
   kinds via `_candidate_from_row`/`retrieve` at `retrieve.py:165-199`).
   Source pages are provenance bookkeeping, not user-facing answer pages.
4. **No new central write path, no raw-chunk RAG, no schema migration
   unless strictly required.** Slug column is `Text`
   (`orthus/tables.py:288`), shared regex `WIKI_SLUG_RE` permits length 1–200
   characters in `[\w가-힣/_-]` (`orthus/wiki/slug.py:7`). The new slug must
   fit that envelope; no Alembic migration is required.

---

## 3. Slug-scheme change (the actual fix)

### Current (`orthus/wiki/slugs.py:15-16`)

```python
def source_slug_from_title(title: str) -> str:
    return f"src-{kebab(title)}"
```

### Proposed

```python
def source_slug_from_title(title: str, doc_id: UUID | str) -> str:
    short = str(doc_id).replace("-", "")[:8]   # 8 hex chars from UUID v4
    return f"src-{kebab(title)}-{short}"
```

Properties:

- **One source per document, always.** Two docs with identical titles get
  distinct slugs because their UUIDs differ. Re-distilling the same doc is
  still idempotent because the doc_id is stable: same input → same slug.
- **Stays inside `WIKI_SLUG_RE` (`[\w가-힣/_-]{1,200}`).** Hex chars are
  `\w`; `kebab()` already strips to `[a-z0-9가-힣-]`. Total length =
  `len("src-") + len(kebab(title)) + len("-") + 8`. `kebab(title)` is
  bounded by the title (documents.title is `text` and titles in the corpus
  are short); even a 180-char title fits under 200.
- **Reversible identity.** The doc_id fragment is the canonical link back
  to `documents.doc_id`. `WikiSource.source_ref` keeps the full UUID
  (already does at `distill.py:220`), so `_authored_source_refs` continues
  to compare full UUIDs — no precision loss.

### Why a doc_id suffix (and not a content hash, sequence, or random)?

| Candidate | Rejected because |
|---|---|
| `sha8(markdown body)` | Same doc with a single edit changes its source slug, leaving orphan source pages every edit; breaks `skip_authored` resume because the slug-from-disk and slug-from-distill diverge per content change. |
| Title + monotonic sequence | Requires a DB lookup for "next n for this title"; not deterministic from a single doc; breaks the pure-function contract of `source_slug_from_title`. |
| Title + random | Re-distill creates a new orphan every cycle. Worst possible churn. |
| Title + doc_id (chosen) | Deterministic per doc; stable across content edits; one-line change; collision-free by construction (UUID v4). |

### What changes in callers

Three call sites of `source_slug_from_title(title)` exist (`grep` evidence):

1. `orthus/wiki/distill.py:153` — pass `doc_id` (already in scope as the
   first arg to `distill_document`).
2. `orthus/wiki/retrieve.py:364` and `:449` — both build a set of source
   slugs from `documents.title` while doing the **provenance reachability
   expansion** (since/until/source filter, and the contaminated-company
   blocklist). These callers need the doc_id too. Concretely, change the
   query to select `documents.doc_id` alongside `documents.title` and call
   `source_slug_from_title(title, doc_id)` per row. The set semantics are
   preserved; the slugs just become unique per doc instead of per title.
3. `orthus/projects.py:140` — `_retag_wiki` rebuilds source-slug set from
   `documents.title` for db-name retag. Same fix: also select `doc_id` and
   call the new signature.

And the helpers used by tests (`tests/integration/test_wiki_retrieve_filters.py`)
import `source_slug_from_title` directly; they must update to pass the
doc_id of the seeded document. This is a typical narrow test change —
collision tests get an additional assertion (see §6).

`kebab()` and `WIKI_SLUG_RE` are unchanged. `WikiSlug` validator
(`orthus/wiki/slug.py`) still passes because the new shape uses only
allowed chars and stays under 200.

### Why claim/page slugs are explicitly NOT touched

`_claim_slug(page_slug, claim_text)` at `distill.py:59-61` is
`{page_slug}-{sha8(claim_text)}`. `page_slug` is `_kebab(page_slug or
title)` at `distill.py:171`. The whole point of consolidate's
`pages_by_slug` loop (`consolidate.py:146-161`) is that two different
sources can yield claims whose `related_pages` contains the same page slug
— that is **how** wiki concept pages accumulate evidence/sources/backlinks
from many docs. If page slugs were doc-id-suffixed the merge would never
happen and every source would seed its own private concept page. Hard NO.

So the contract is exactly:

- Source identity: **per document** (after this fix). One markdown file
  per doc.
- Claim identity: per (concept page, normalized claim text). Identical
  claims merge across sources (current behavior, kept).
- Page identity: per concept. Pages accumulate `sources=[...]` referencing
  many source slugs (current behavior, kept).

---

## 4. Files to edit (executor checklist input)

All paths absolute under `<repo>`.

| File | Change |
|---|---|
| `orthus/wiki/slugs.py` | Add `doc_id: UUID \| str` argument to `source_slug_from_title`; append `-<doc_id[:8]>`. Keep `kebab()` untouched. |
| `orthus/wiki/distill.py:25,153` | Update import (no path change). Pass `doc_id` at line 153. No other change — `WikiSource.source_ref=str(doc_id)` already in place at line 220 stays correct. |
| `orthus/wiki/retrieve.py:33,364,449` | Select `documents.c.doc_id` alongside title; pass per-row doc_id into the new signature. |
| `orthus/projects.py:140` | Same: include `doc_id` in the SELECT, build `{source_slug_from_title(title, doc_id) for (title, doc_id) in rows}`. Drop the inline `f"src-{_kebab(t)}"`. |
| `tests/integration/test_wiki_retrieve_filters.py:18,47,137` | Update call sites to pass the seeded `doc_id`. |
| `tests/integration/test_wiki_authoring.py` | New regression test for the collision case (§6). |
| `tests/unit/test_wiki_store_codec.py` | No change required — those tests use literal `src-vacation-policy` strings to exercise the codec round-trip; they don't call `source_slug_from_title`. |

No new schema migration. No new module. No FE change.

---

## 5. Blast radius (what else touches source slugs)

Confirmed by `grep -rn "source_slug_from_title|src-"` over the repo:

- **Provenance projection in answers**: `WikiSourceRef.provenance` carries
  source slugs (`orthus/schemas/canonical.py:90-99`, populated in
  `retrieve.py:188-198`). The slug strings just get longer; consumers
  (`orthus/wiki/qa.py`, `orthus/api/routes/ask.py`,
  `derive_wiki_links` at `canonical.py:107-119`) treat them as opaque
  identifiers. No format assumption breaks.
- **Federation read plane**: `GET /wiki/pages/{slug:path}`
  (`orthus/api/routes/federation.py:57`) takes the slug as opaque. Personal
  FE federated read of company pages is by **page** slug (concept page),
  not source slug — concept page slugs do not change.
- **FE `/wiki` routes**: only reference page slugs (`web/app/wiki/...`).
  Spot-check confirms no `src-` literal in `web/`. Source pages are
  internal provenance, not navigated to by users.
- **`projects.py` re-tag**: covered in §3; the comment "src-<kebab(title)>"
  at `projects.py:16` should be updated to "src-<kebab(title)>-<doc_id8>"
  in the docstring at the same time as the implementation change.
- **Postgres uniqueness**: `(slug, scope, owner_id)` with
  `NULLS NOT DISTINCT` (migration `0004_tenant_scope.py:39-42`,
  `tables.py:300-307`). New longer slugs trivially satisfy this — the
  longer slug column is `Text`, no length cap. Index size grows marginally
  (~12 bytes per source row × ~1816 rows = trivial).
- **`WIKI_SLUG_RE` length cap of 200** (`orthus/wiki/slug.py:7`). Worst-case
  new slug length = 4 ("src-") + ≤180 (kebab-title) + 1 + 8 = 193. Fits.
  If we want belt-and-suspenders, the executor can clip
  `kebab(title)` to 180 chars before the join; current corpus titles are
  far shorter than that.
- **`_authored_source_refs` (`author.py:117-123`)**: this is the function
  whose probe must now converge. After the fix, each doc has its own
  source markdown file → `source_ref` for every doc is in the set → next
  cycle's `skip_authored` skips all already-distilled docs. The 36 churning
  docs will distill **once** and then go quiet. Verified by reading the
  function: it iterates `list_slugs("source", ...)`, loads each, collects
  `source.source_ref`. Currently with the collision, multiple docs map to
  one slug → set has one doc_id; under the fix, each doc gets its own
  slug → set has every doc_id.
- **`consolidate` write order** (`consolidate.py:133-167`): source written
  before claims/pages. With unique source slugs, the source write becomes
  insert-only on first author (no overwrite of another doc's source);
  subsequent re-distills of the **same** doc still idempotently rewrite
  the same source file (slug stable per doc_id). Page merge logic
  unaffected — concept pages still accumulate `sources=[...]` from many
  source slugs.

**Net blast radius**: source-page identity widens (1 file per doc instead
of 1 per title-shape), consumers of `provenance` slug strings are
unaffected (opaque), and the convergence loop fixes itself.

---

## 6. Migration & backfill plan

### State on disk (verified)

- `~/.orthus/nodes/company/wiki-store/company/sources/*.md` — 1816 files.
- `~/.orthus/nodes/company/wiki-store/personal/<owner>/sources/*.md` —
  present (personal scope under the company node, P8 owner-row layout).
- DB mirror in `wiki_pages` rows with `kind='source'`.

### Recommended path: one-time clean reauthor (`make wiki-rebuild --clean`)

This is the cheapest correct path and matches the documented operator
runbook for wiki rebuilds.

1. Take down or quiesce the affected node's wiki writer (no concurrent
   `author_from_document` while rebuild runs).
2. Run `python -m orthus.wiki.rebuild --clean --concurrency 4
   --continue-on-error` per node. The `_clean_wiki_layer`
   (`orthus/wiki/rebuild.py:24-43`) deletes `wiki_links`, `wiki_chunks`,
   `wiki_chunk` embeddings, `wiki_pages` rows, and wipes the scope
   subtrees under `wiki-store/`. The corpus (`documents` + corpus chunks
   + corpus embeddings) is untouched.
3. Reauthor every document under the new slug scheme. Cost: one full
   LLM-distill pass over the corpus (~1816 docs company + N personal,
   parallel distill writes serial). Operator-cost discussion: this is
   identical to a normal `make wiki-rebuild --clean` cycle; it has been
   done before (see `make wiki-rebuild` docstring in Makefile / AGENTS.md).
4. After rebuild, the inbound `skip_authored` probe converges: a follow-up
   `--skip-authored` cycle should report **0 redistilled**. That is the
   evidence the fix is in (§7).

Rationale for clean-reauthor over in-place backfill:

- An in-place backfill would need to (a) walk every existing source `.md`,
  (b) read its `source_ref` (the doc_id), (c) compute the new slug, (d)
  rewrite the markdown file under the new path, (e) update
  `wiki_pages.slug` and any `wiki_links.dst_slug` pointing at the old
  source slug, (f) delete the stale `.md` and its `wiki_pages`/`wiki_chunks`
  rows. That is a non-trivial offline migration script that has to be
  audited and replayed per scope/owner — for ~1816 rows it costs more
  engineering than one rebuild pass.
- The collided sources today already have **wrong content** for all but one
  of the colliding docs (last-write-wins). Backfilling preserves wrong
  data; reauthor fixes it. Reauthor is the only path that actually
  recovers the lost-coverage docs (e.g. the "actors list split into
  per-person docs" case).
- `--clean` deletes orphan source `.md` files automatically. In-place
  backfill must explicitly clean orphans after rewrite. Easy to get wrong.

### When in-place backfill could be preferable

If the operator cannot accept a full LLM rebuild cost (e.g. a quota
window), an alternative is a **rename-only** backfill script — leave
content as-is, just rewrite `slug` field in frontmatter + file path + DB
`slug`/`wiki_links.dst_slug`. This preserves the 1 surviving doc's content
per collision group; **the other colliding docs remain missing from the
wiki** until a follow-up `--skip-authored` rebuild fills them in. This
half-migration is acceptable as a triage step if reauthor cost is the
blocker; not recommended as the primary path.

The clean reauthor is the recommended primary path.

### Orphan cleanup

`_clean_wiki_layer` already wipes the wiki-store scope subtrees and DB
mirror, so no separate orphan sweep is needed when going through `--clean`.

---

## 7. Test plan

New regression test in
`<repo>/tests/integration/test_wiki_authoring.py`:

```text
test_two_same_title_docs_get_distinct_source_slugs_and_both_authored:
  1. Seed two documents with identical title "Release v2.0.0",
     distinct doc_ids, both scope=company.
  2. Run author_from_document for doc A, then for doc B.
  3. Assert:
       store.list_slugs("source") returns 2 distinct slugs.
       Both slugs start with "src-release-v2-0-0-".
       store.load_source(slugA).source_ref == str(docA.doc_id).
       store.load_source(slugB).source_ref == str(docB.doc_id).
  4. _authored_source_refs() contains BOTH doc_ids.
  5. Concept-page merge still works:
       Both sources contribute to one shared concept page; the page's
       `sources` list contains BOTH slugs; the page's `evidence`
       /`backlinks` includes claims from BOTH sources where
       page_slug was shared.
```

A second test asserts claim merge is unaffected:

```text
test_same_claim_text_from_two_sources_merges_one_page:
  Seed two docs with different titles but a claim whose
  related_pages contains the same kebab page slug. Confirm one wiki
  page is written with both source slugs in its `sources` list and
  both claim slugs in its `evidence`/`backlinks`. (This guards
  consolidate.py:146-161 from regressing on this design change.)
```

A third unit test asserts slug-scheme determinism and length:

```text
test_source_slug_is_deterministic_and_unique_per_doc_id:
  Same (title, doc_id) → same slug.
  Same title, different doc_ids → different slugs.
  All produced slugs satisfy WIKI_SLUG_RE.
```

No FE / E2E test required; the change is invisible to user-facing wiki
pages.

---

## 8. Implementation checklist (ordered, for the executor)

1. `orthus/wiki/slugs.py` — extend `source_slug_from_title` signature with
   `doc_id`. Keep `kebab()` unchanged.
2. `orthus/wiki/distill.py:153` — pass `doc_id` to the helper.
3. `orthus/wiki/retrieve.py:364,449` — update SELECTs to include
   `documents.c.doc_id`; build the set with the per-row pair.
4. `orthus/projects.py:140` — same; also update the module docstring at
   `projects.py:16` so it matches the new slug shape.
5. Add the three tests in §7. Run `make test` (uses `orthus_test` DB).
6. Run `make fmt`.
7. Operator (separately, gated): per node, `python -m orthus.wiki.rebuild
   --clean --concurrency 4 --continue-on-error`. Then once more with
   `--skip-authored` and assert it reports **0 documents** redistilled.
   That is the convergence proof.
8. Spot-check the 6 collision groups from the bug report — e.g. confirm
   `wiki-store/company/sources/` contains 6 distinct `src-v2-0-0-*-<hex>`
   files for the v2.0.0 release-note docs.

---

## 9. References

- `<repo>/orthus/wiki/slugs.py:15-16` — current
  source-slug derivation.
- `<repo>/orthus/wiki/distill.py:153,220` — distill call
  site and `source_ref=str(doc_id)`.
- `<repo>/orthus/wiki/consolidate.py:34-48,133-167` —
  page seed + merge loop that must stay content-keyed.
- `<repo>/orthus/wiki/store.py:510-518,665-690,779-836,853-876`
  — slug→path, write_source, _persist, list_slugs/load_source.
- `<repo>/orthus/wiki/author.py:117-123` — the
  `_authored_source_refs` probe that must converge.
- `<repo>/orthus/wiki/retrieve.py:364,449,484` —
  provenance reachability builds source-slug sets by title.
- `<repo>/orthus/projects.py:16,140` — `_retag_wiki`
  source-slug set, also built by title.
- `<repo>/orthus/wiki/slug.py:7` —
  `WIKI_SLUG_RE = [\w가-힣/_-]{1,200}` length envelope.
- `<repo>/migrations/postgres/versions/0004_tenant_scope.py:39-42`
  + `<repo>/orthus/tables.py:284-308` — composite unique
  `(slug, scope, owner_id) NULLS NOT DISTINCT`.
- `<repo>/orthus/wiki/rebuild.py:24-82` — `--clean` and
  `--skip-authored` mechanics used by the migration plan.
- `<repo>/orthus/schemas/canonical.py:90-99` —
  `WikiSourceRef` (opaque consumer of slug strings).
