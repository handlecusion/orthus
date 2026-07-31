"""P8.6 personal -> central migration tooling.

Moves a personal node's Postgres data into the central (company) node DB under
the strict owner-only row boundary (docs/p8-central-consolidation.md §3 / §6
P8.6 / §7). Wiki pages/chunks/links are NOT migrated: they are recompiled
centrally from the imported `documents` via `compile_personal_documents`
(P8.4). The flow is export (read-only on source) -> JSONL bundle -> import into
central -> parity report -> central compile.
"""
