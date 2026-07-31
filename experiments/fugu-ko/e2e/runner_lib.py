"""Shared machinery for the unified E2E benchmark harness (`harness_e2e.py`).

Everything here is experiment-local (`experiments/fugu-ko/` only) and touches no
production `orthus/` code. It provides:

- manifest loading + §12 input-hash verification (`manifest_schema.md`)
- a scratch-DB fixture loader (seeds `orthus_test` per L2 item, `l2/DESIGN.md` §1.5)
- test-settings reset (mirrors `tests/conftest.py::clean` for the fields the L2
  flows read) so a fixture patch starts from a known baseline
- the `model.fallback == 0` invariant counter (queries the audit_log SoR the
  production `FallbackChat` writes — `docs/model-orchestration.md`) + a
  confident-zero detector
- statistics: exact McNemar (paired) generalized off `embedding/significance.py`,
  power/MDE off `embedding/power.py`, and a NEW bootstrap-CI (10k resamples,
  none existed — §1b of the plan uses bootstrap)
- structural / dotted-path assertion helpers for L2 `expected.kind == structural`
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
import uuid
from dataclasses import dataclass
from math import comb, sqrt
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/e2e
FUGU = HERE.parent                                # experiments/fugu-ko
REPO = FUGU.parent.parent                         # repo root
for p in (str(REPO), str(FUGU)):
    if p not in sys.path:
        sys.path.insert(0, p)

TIER_A_PATH = HERE / "tier_a.jsonl"
TIER_B_PATH = HERE / "tier_b.jsonl"
L2_DIR = HERE / "l2"
FIXTURES_DIR = HERE / "fixtures"
RAW_DIR = FUGU / "analysis" / "raw"

# Tags that mean "no frozen input yet — the user must fill it against the live
# company corpus" (l2/DESIGN.md §2.1). These are counted + logged, never run.
_PENDING_TAGS = {
    "pending_user_fill",
    "needs_user_corpus_fill",
    "needs_user_fill",
}

Z_ALPHA = 1.959963985  # two-sided 0.05
Z_BETA = 0.8416212336  # 80% power


# --------------------------------------------------------------------------- #
# Manifest loading + hashing (manifest_schema.md §12/§13)
# --------------------------------------------------------------------------- #
def canonical_input_sha256(input_obj: Any) -> str:
    """§12 canonicalization: sorted keys, no whitespace, literal unicode, UTF-8."""
    blob = json.dumps(input_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class Item:
    """One manifest line + derived routing metadata."""

    raw: dict
    id: str
    layer: str
    task: str
    entry_point: str
    input: dict
    expected: dict
    scoring: str
    tier: str
    tags: list[str]
    invariants: list[str]

    @property
    def kind(self) -> str:
        return str(self.expected.get("kind", ""))

    def has_tag(self, *names: str) -> bool:
        return any(t in self.tags for t in names)


def _to_item(rec: dict) -> Item:
    return Item(
        raw=rec,
        id=rec["id"],
        layer=rec["layer"],
        task=rec["task"],
        entry_point=rec["entry_point"],
        input=rec.get("input", {}),
        expected=rec.get("expected", {}),
        scoring=rec.get("scoring", ""),
        tier=rec.get("tier", ""),
        tags=list(rec.get("tags", [])),
        invariants=list(rec.get("invariants", [])),
    )


def _is_pending(rec: dict) -> bool:
    frozen = rec.get("frozen") or {}
    if frozen.get("input_sha256") in (None, "", "null"):
        return True
    if any(t in _PENDING_TAGS for t in rec.get("tags", [])):
        return True
    # placeholder inputs sometimes carry a sentinel value
    inp = rec.get("input", {})
    return bool(inp.get("_pending") or inp.get("needs_user_corpus_fill"))


def load_manifest_files(
    paths: list[Path],
    *,
    strict_hash: bool = False,
) -> tuple[list[Item], list[str], list[str]]:
    """Load jsonl manifests into Item records.

    Returns (items, pending_ids, hash_mismatch_ids). Pending items (no frozen
    input yet) are skipped and COUNTED, never silently dropped. A hash mismatch
    is a hard error under strict_hash; otherwise it is skipped + reported (a
    smoke should surface it, not crash the whole run).
    """
    items: list[Item] = []
    pending: list[str] = []
    mismatches: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if _is_pending(rec):
                pending.append(rec.get("id", "<no-id>"))
                continue
            declared = (rec.get("frozen") or {}).get("input_sha256")
            actual = canonical_input_sha256(rec.get("input", {}))
            if declared and actual != declared:
                if strict_hash:
                    raise ValueError(
                        f"input_sha256 mismatch for {rec['id']}: "
                        f"declared {declared[:12]} != actual {actual[:12]} "
                        "(input drifted without id retirement — manifest_schema.md §12)"
                    )
                mismatches.append(rec["id"])
                continue
            items.append(_to_item(rec))
    return items, pending, mismatches


def default_manifest_paths(tier: str, layer: str) -> list[Path]:
    """Which jsonl files feed a (tier, layer) selection.

    Tier A lives in `tier_a.jsonl` (L1, per Phase 1) plus the not-yet-merged L2
    `l2/g*.jsonl` (which carry BOTH tiers until Phase 3 splits them). Tier B is
    the split-out holdout once it exists.
    """
    paths: list[Path] = []
    if layer in ("L1", "all"):
        if tier in ("A", "all"):
            paths.append(TIER_A_PATH)
        if tier in ("B", "all"):
            paths.append(TIER_B_PATH)
    if layer in ("L2", "all"):
        # g*.jsonl hold both tiers; the loader filters by item.tier afterward.
        paths.extend(sorted(L2_DIR.glob("g*.jsonl")))
    return paths


# --------------------------------------------------------------------------- #
# DB + settings (scratch orthus_test)
# --------------------------------------------------------------------------- #
def dsn_database_name(dsn: str) -> str:
    """Best-effort database-name extraction from a SQLAlchemy-style DSN string.

    Stdlib `urllib.parse` only (no sqlalchemy import) — every other function
    in this module imports sqlalchemy/orthus lazily inside the function body so
    `runner_lib` stays importable without those deps (manifest loading,
    stats); this guard is exercised as a module-level self-test at import
    time (below), so it must not gain a hard dependency the rest of the file
    deliberately avoids at import time.
    """
    try:
        from urllib.parse import urlsplit

        return urlsplit(dsn).path.lstrip("/").lower()
    except Exception:
        return ""


def is_safe_truncate_dsn(dsn: str) -> bool:
    """Fail-closed allowlist: TRUNCATE + fixture-seeding may only target a DSN
    whose database name contains "test" or "staging".

    Mirrors the protected-DB pattern in `scripts/.../snapshot_to_staging.sh`
    (which refuses `orthus`/`orthus_test` as an overwrite *target* and requires
    "staging" in the target node name), inverted into an allowlist here since
    the dangerous direction for this harness is seeding/wiping a DB that was
    NOT meant to be scratch space (e.g. someone sourced `node.env` for
    `company` and `orthus_company` is what `ORTHUS_PG_DSN` now resolves to).
    Unparseable / empty names are unsafe by default.
    """
    name = dsn_database_name(dsn)
    return "test" in name or "staging" in name


# Inline self-test — always true, exercised at import time so a future
# refactor that weakens the guard fails loudly instead of silently widening
# what counts as a safe TRUNCATE target.
assert is_safe_truncate_dsn(
    "postgresql+psycopg://orthus:orthus@localhost:5433/orthus_test"
) is True
assert is_safe_truncate_dsn(
    "postgresql+psycopg://orthus:orthus@localhost:5433/company-staging"
) is True
assert is_safe_truncate_dsn(
    "postgresql+psycopg://orthus:orthus@localhost:5433/orthus_company"
) is False
assert is_safe_truncate_dsn(
    "postgresql+psycopg://orthus:orthus@localhost:5433/orthus"
) is False
assert is_safe_truncate_dsn("not a dsn") is False


def resolved_pg_dsn() -> str:
    """The current `orthus.settings` `pg_dsn`, or "" if settings can't resolve."""
    try:
        from orthus.settings import get_settings

        return get_settings().pg_dsn
    except Exception:
        return ""


def truncate_guard_ok() -> bool:
    """True iff the resolved `orthus.settings` `pg_dsn` is safe to TRUNCATE.

    Fail-closed: any error resolving settings/DSN counts as unsafe.
    """
    try:
        return is_safe_truncate_dsn(resolved_pg_dsn())
    except Exception:
        return False


LIVE_DB_WARNING = (
    "live DB detected — L2 fixture items skipped, L1 read-only items only"
)


def db_reachable() -> bool:
    try:
        from sqlalchemy import text

        from orthus.db import session

        with session() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# Company-scope synthetic actor L1 items dispatch as (harness.py convention).
# L1 has no fixture-seeding step (that's an L2-only concept), but several
# production entry points FK their audit/run rows to `users.user_id` (e.g.
# `query_structured` -> `assistant/pipeline.py::insert_run` -> `query_runs`).
# Without a backing row this raises IntegrityError instead of running/scoring.
L1_HARNESS_UID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def ensure_l1_harness_user() -> None:
    """Idempotently seed the `users` row L1 dispatch's synthetic actor needs.

    Best-effort: any table each item's fixture/truncate cycle may have wiped
    (L2 dispatch truncates+reseeds per item) is restored the next time an L1
    item runs, since `run_model` calls this once per model before its item
    loop. Silent no-op if the DB is unreachable — callers already handle that
    via each item's own try/except.
    """
    try:
        from sqlalchemy import insert, select

        from orthus.db import session
        from orthus.tables import users

        with session() as s:
            exists = s.execute(
                select(users.c.user_id).where(users.c.user_id == L1_HARNESS_UID)
            ).first()
            if exists:
                return
            s.execute(
                insert(users).values(
                    user_id=L1_HARNESS_UID, display_name="e2e-harness"
                )
            )
            s.commit()
    except Exception:
        pass


def truncate_all_tables() -> None:
    """Empty every application table (mirrors conftest `_truncate_data_tables`).

    Fail-closed DSN guard (defect-1 fix): a no-op with a one-line warning
    unless `truncate_guard_ok()` — the resolved DSN's database name contains
    "test" or "staging". This is defense-in-depth for any caller that skips
    the harness-level pre-check (`dispatch_l2` in `harness_e2e.py` already
    checks `truncate_guard_ok()` itself and skips the item instead of ever
    reaching here on a live DB).
    """
    if not truncate_guard_ok():
        print(f"WARNING: {LIVE_DB_WARNING}", file=sys.stderr)
        return

    from sqlalchemy import text

    from orthus.db import session
    from orthus.tables import metadata

    names = ", ".join(f'"{t.name}"' for t in metadata.tables.values())
    with session() as s:
        s.execute(text(f"TRUNCATE TABLE {names}"))
        s.commit()


def reset_test_settings() -> None:
    """Restore the singleton Settings to the L2 test baseline.

    A trimmed copy of `tests/conftest.py::clean` — only the fields the g1-g4
    flows and their guards actually read. `embedding` stays whatever the run
    env pinned (mock in the offline smoke). A fixture's `settings` block is
    applied on top of this by `apply_fixture`.
    """
    from orthus.settings import get_settings

    s = get_settings()
    s.node_kind = "company"
    s.node_id = "company"
    s.auth_mode = "demo"
    s.owner_scope_enabled = False
    s.secret_backend = "memory"
    s.kg_enabled = False
    s.kg_owner_scope_enabled = False
    s.collector_api_enabled = False
    s.agent_task_enabled = True
    s.agent_gateway_actions_enabled = True
    s.ask_decompose_enabled = False
    s.ask_command_split_enabled = False
    s.ask_decompose_command_guard = True
    s.ask_semantic_cache_enabled = False
    # mail defaults: everything off / individual until a fixture opts in.
    for attr, val in {
        "email_sender": "none",
        "mail_send_enabled": False,
        "mail_ingest_enabled": False,
        "mail_pull_ingest_enabled": False,
        "mail_multi_account_enabled": False,
        "mail_nova_kind": "individual",
        "mail_acme_kind": "individual",
        "mail_reply_draft_enabled": False,
        "mail_agent_task_delegation_enabled": False,
        "mail_ingest_service_user_id": "00000000-0000-4000-8000-000000000001",
    }.items():
        if hasattr(s, attr):
            setattr(s, attr, val)


def apply_fixture(fixture: dict) -> dict:
    """Truncate, apply the fixture's `settings` block, seed its `rows`.

    Returns the fixture's `actor` dict so the caller can build the auth override.
    Runs AFTER `reset_test_settings()`.

    Fail-closed DSN guard (defect-1 fix): raises instead of seeding rows into a
    non-test/staging DB. Callers should pre-check `truncate_guard_ok()` and
    skip the item instead of reaching this (`dispatch_l2` in `harness_e2e.py`
    does); this raise is a defense-in-depth backstop so a future caller that
    skips the pre-check cannot silently insert fixture rows into a live DB
    (unlike `truncate_all_tables()`, a no-op here would be worse than a raise —
    seeding without truncating first is actively misleading, not just skipped).
    """
    if not truncate_guard_ok():
        raise RuntimeError(
            f"refusing to seed fixture rows: {LIVE_DB_WARNING} "
            "(pre-check truncate_guard_ok() before calling apply_fixture)"
        )

    from orthus.settings import get_settings

    truncate_all_tables()
    reset_test_settings()
    s = get_settings()
    for key, val in (fixture.get("settings") or {}).items():
        if hasattr(s, key):
            setattr(s, key, val)
    _seed_rows(fixture.get("rows") or {})
    return fixture.get("actor") or {}


# per-table field aliasing / defaulting so fixture JSON (authored to DESIGN.md
# §1.5's logical shape) maps onto the real table columns.
def _normalize_row(table_name: str, row: dict, settings: Any) -> dict:
    row = dict(row)
    if table_name == "agent_chat_sessions" and "user_id" in row and "owner_id" not in row:
        row["owner_id"] = row.pop("user_id")
    if table_name == "collector_tokens":
        row.setdefault("token_hash", uuid.uuid4().hex)
        row.setdefault("scopes", ["commands"])
        row.setdefault("node_id", getattr(settings, "node_id", "company"))
    if table_name == "documents":
        row.setdefault("schema_version", 1)
        row.setdefault("block_json", [{}])
        row.setdefault("markdown", "")
    if table_name == "agent_work_items":
        row.setdefault("node_id", getattr(settings, "node_id", "company"))
        row.setdefault("node_kind", getattr(settings, "node_kind", "company"))
    if table_name == "collector_commands":
        row.setdefault("node_id", getattr(settings, "node_id", "company"))
    return _resolve_sentinel_values(row)


# Documented fixture sentinel (g3-send-rate-limited.json, g3-drafted-reply-
# resolved.json note field): a literal "recent" string in a timestamp value
# means "the harness inserts datetime.now(UTC) minus a few minutes" so a
# single live request hits an existing-row / rate-limit branch directly.
def _resolve_sentinel_values(row: dict) -> dict:
    from datetime import datetime, timedelta, timezone

    out = dict(row)
    for k, v in row.items():
        if v == "recent":
            out[k] = datetime.now(timezone.utc) - timedelta(minutes=5)
    return out


def _topo_sort_tables(table_names: Any) -> list[str]:
    """Order fixture table keys so FK parents insert before their children.

    Fixture JSON authors write `rows` keys in whatever order reads best for
    the scenario (e.g. `agent_work_decisions` before `agent_work_items`,
    `auth_identities` before `users`) — `dict` preserves that order, and a
    naive `for table_name in rows` insert hits real FK violations. This walks
    each table's FK graph restricted to tables present in the same fixture, so
    any current or future FK-having table pair is seeded in a safe order
    automatically — no hardcoded per-table guess.

    Uses live DB reflection (`_table_fk_targets`), not `orthus.tables.metadata`:
    several tables (e.g. `auth_identities.user_id` -> `users.user_id`) carry a
    real Postgres FK constraint that was never declared on the SQLAlchemy
    `Table` object, so `Table.foreign_keys` misses it entirely.
    """
    names = list(table_names)
    name_set = set(names)
    deps: dict[str, set[str]] = {}
    for n in names:
        targets = _table_fk_targets(n)
        deps[n] = {t for t in targets if t in name_set and t != n}

    ordered: list[str] = []
    seen: set[str] = set()

    def visit(n: str, stack: set[str]) -> None:
        if n in seen or n in stack:
            return  # already placed, or a cycle — leave insertion order as-is
        stack = stack | {n}
        for d in deps.get(n, ()):
            visit(d, stack)
        seen.add(n)
        ordered.append(n)

    for n in names:
        visit(n, set())
    return ordered


_FK_TARGETS_CACHE: dict[str, set[str]] = {}


def _table_fk_targets(table_name: str) -> set[str]:
    """Referenced-table names for `table_name`'s FKs, per live DB reflection.

    Process-lifetime cache — the test DB's schema is fixed for the run.
    """
    if table_name in _FK_TARGETS_CACHE:
        return _FK_TARGETS_CACHE[table_name]
    targets: set[str] = set()
    try:
        from sqlalchemy import inspect

        from orthus.db import session

        with session() as s:
            insp = inspect(s.get_bind())
            for fk in insp.get_foreign_keys(table_name):
                referred = fk.get("referred_table")
                if referred:
                    targets.add(referred)
    except Exception:
        pass  # unreflectable (e.g. DB unreachable) -> fixture insert order kept
    _FK_TARGETS_CACHE[table_name] = targets
    return targets


def _seed_rows(rows: dict) -> None:
    from sqlalchemy import insert

    from orthus.db import session
    from orthus.settings import get_settings
    from orthus.tables import metadata

    settings = get_settings()
    with session() as s:
        for table_name in _topo_sort_tables(rows.keys()):
            records = rows.get(table_name)
            if table_name == "wiki_store":
                _seed_wiki_store(records)
                continue
            table = metadata.tables.get(table_name)
            if table is None:
                continue  # unknown logical table (e.g. handled elsewhere)
            cols = {c.name for c in table.columns}
            for rec in records or []:
                norm = _normalize_row(table_name, rec, settings)
                values = {k: v for k, v in norm.items() if k in cols}
                _fill_missing_pks(table, values)
                s.execute(insert(table).values(**values))
        s.commit()


def _fill_missing_pks(table: Any, values: dict) -> None:
    """Generate a uuid4 for any UUID primary-key column the fixture omitted.

    Fixtures pin ids where identity matters (users, sessions, tokens the body
    references) but leave surrogate PKs (identity_id, some token_id) blank —
    those still need a value or the insert violates NOT NULL.
    """
    import uuid as _uuid

    for col in table.primary_key.columns:
        if col.name in values and values[col.name] not in (None, ""):
            continue
        if col.default is not None or col.server_default is not None:
            continue
        pytype = getattr(col.type, "python_type", None)
        try:
            is_uuid = pytype is _uuid.UUID
        except Exception:
            is_uuid = False
        if is_uuid or "UUID" in str(col.type).upper():
            values[col.name] = _uuid.uuid4()


def _seed_wiki_store(spec: Any) -> None:
    """Best-effort markdown wiki-store seeding for g1-J grounding fixtures.

    Grounding items are model-discriminating (live only); under the offline
    smoke they are deferred, so full fidelity is not required for the smoke to
    pass. Kept minimal + defensive: if the wiki store API shape does not match,
    the item is simply exercised for plumbing and reported deferred.
    """
    try:
        from orthus.wiki import store as wiki_store  # noqa: F401
    except Exception:
        return
    # Intentionally left as a no-op substrate hook: g1-page grounding needs the
    # live authoring pipeline, which the live bench runs. The smoke does not
    # score these, so seeding is deferred to the live pass.
    return


# --------------------------------------------------------------------------- #
# Invariants: model.fallback == 0  +  confident-zero
# --------------------------------------------------------------------------- #
def count_fallback_spans() -> int:
    """Count `model.fallback` audit spans persisted so far.

    `FallbackChat.complete` (orthus/models/orchestration.py) opens
    `audit("model.fallback")` on every fallback, which writes an `enter` row to
    the audit_log SoR. Counting rows is monkeypatch-free and works whether the
    fallback ladder was reached via ASSIGNMENTS toggling or a monkeypatch.
    """
    try:
        from sqlalchemy import func, select

        from orthus.db import session
        from orthus.tables import audit_log

        with session() as s:
            return int(
                s.execute(
                    select(func.count())
                    .select_from(audit_log)
                    .where(audit_log.c.node == "model.fallback")
                    .where(audit_log.c.phase == "enter")
                ).scalar_one()
            )
    except Exception:
        return 0


def is_confident_zero(task: str, output: Any, *, expected_nonempty: bool) -> bool:
    """Degenerate structured/route result where a non-empty one was expected.

    A known L1 routing-family regression (manifest_schema.md §14
    `no_confident_zero`): an empty/None label paired with a confident decision.
    """
    if output is None:
        return expected_nonempty
    if isinstance(output, str) and output.strip() == "":
        return True
    if isinstance(output, (list, dict)) and len(output) == 0 and expected_nonempty:
        return True
    return False


# --------------------------------------------------------------------------- #
# Structural / dotted-path assertions (manifest_schema.md §7.3)
# --------------------------------------------------------------------------- #
def dotted_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return cur


def structural_assert(actual: dict, assert_spec: dict) -> tuple[bool, list[str]]:
    """Every key in `assert_spec` must match `actual`. `_contains` /
    `_contains_any` suffixes do list-membership checks (§7.3); a `_min`
    suffix does a numeric floor check against the projected base key
    (l2/DESIGN.md §4.4 `part_count_min`)."""
    failures: list[str] = []
    for key, want in assert_spec.items():
        if key.endswith("_min") and key not in actual:
            base = key[: -len("_min")]
            got = dotted_get(actual, base)
            if not isinstance(got, (int, float)) or got < want:
                failures.append(f"{base}: {got!r} < min {want!r}")
        elif key.endswith("_contains_any"):
            base = key[: -len("_contains_any")]
            got = dotted_get(actual, base)
            got_list = got if isinstance(got, list) else []
            wants = want if isinstance(want, list) else [want]
            if not any(w in got_list for w in wants):
                failures.append(f"{base}: {got_list!r} contains none of {wants!r}")
        elif key.endswith("_contains"):
            base = key[: -len("_contains")]
            got = dotted_get(actual, base)
            got_list = got if isinstance(got, list) else []
            wants = want if isinstance(want, list) else [want]
            for w in wants:
                if w not in got_list:
                    failures.append(f"{base}: {got_list!r} missing {w!r}")
        else:
            got = dotted_get(actual, key)
            if got != want:
                failures.append(f"{key}: {got!r} != {want!r}")
    return (not failures), failures


# --------------------------------------------------------------------------- #
# Set-overlap metric scoring (manifest_schema.md §7.2)
# --------------------------------------------------------------------------- #
def score_metric(output_set: list, expected: dict) -> tuple[bool, dict]:
    reference = expected.get("reference") or []
    ref = set(map(str, reference))
    got = set(map(str, output_set or []))
    tp = len(ref & got)
    recall = tp / len(ref) if ref else 1.0
    precision = tp / len(got) if got else (1.0 if not ref else 0.0)
    fp_rate = (len(got - ref) / len(got)) if got else 0.0
    ok = True
    if expected.get("recall_min") is not None and recall < expected["recall_min"]:
        ok = False
    if expected.get("precision_min") is not None and precision < expected["precision_min"]:
        ok = False
    if expected.get("fp_rate_max") is not None and fp_rate > expected["fp_rate_max"]:
        ok = False
    return ok, {"recall": recall, "precision": precision, "fp_rate": fp_rate}


# --------------------------------------------------------------------------- #
# Statistics — McNemar (paired) + power/MDE + bootstrap CI
# --------------------------------------------------------------------------- #
def exact_mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar (binomial). b, c = discordant-pair counts.

    Ported verbatim from embedding/significance.py so the E2E harness and the
    retrieval experiment agree on the test.
    """
    n = b + c
    if n == 0:
        return 1.0
    lo = min(b, c)
    tail = sum(comb(n, i) for i in range(lo + 1)) / (2**n)
    return min(1.0, 2 * tail)


def mcnemar_from_correct(
    a_correct: dict[str, bool], b_correct: dict[str, bool]
) -> dict:
    """Paired McNemar over two models' per-item correctness dicts (keyed by id).

    Returns discordant counts, the p-value, and which model each discordant
    pair favored. `b` = A-only-correct, `c` = B-only-correct (embedding
    significance.py convention).
    """
    ids = sorted(set(a_correct) & set(b_correct))
    b = sum(1 for i in ids if a_correct[i] and not b_correct[i])
    c = sum(1 for i in ids if b_correct[i] and not a_correct[i])
    return {
        "n_paired": len(ids),
        "a_only": b,
        "b_only": c,
        "discordant": b + c,
        "p_value": exact_mcnemar(b, c),
        "significant": exact_mcnemar(b, c) < 0.05,
    }


def min_split_for_sig(n: int) -> int | None:
    for k in range(n // 2, n + 1):
        if exact_mcnemar(n - k, k) < 0.05:
            return k
    return None


def n_discordant_for_power(pi: float) -> float:
    d = abs(pi - 0.5)
    if d < 1e-9:
        return float("inf")
    return (Z_ALPHA * 0.5 + Z_BETA * sqrt(pi * (1 - pi))) ** 2 / d**2


def mde_pi(n: int) -> float:
    lo, hi = 0.5, 0.999
    for _ in range(200):
        mid = (lo + hi) / 2
        if n_discordant_for_power(mid) > n:
            lo = mid
        else:
            hi = mid
    return hi


def bootstrap_ci(
    values: list[float],
    *,
    statistic: Callable[[list[float]], float] = None,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    seed: int = 1234,
) -> tuple[float, float]:
    """Percentile bootstrap CI (10k resamples) for a per-item statistic.

    §1b of the plan calls for bootstrap CIs and none existed in the repo
    (embedding/power.py only had the normal-approx McNemar CI). Default
    statistic is the mean, so passing 0/1 correctness gives an accuracy CI.
    """
    if statistic is None:
        statistic = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(values)
    stats: list[float] = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        stats.append(statistic(sample))
    stats.sort()
    lo = stats[int((alpha / 2) * n_resamples)]
    hi = stats[min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))]
    return (lo, hi)


def bootstrap_paired_diff_ci(
    a_correct: dict[str, bool],
    b_correct: dict[str, bool],
    *,
    n_resamples: int = 10000,
    seed: int = 1234,
) -> tuple[float, float]:
    """Bootstrap CI on the paired accuracy difference (A - B) over shared ids."""
    ids = sorted(set(a_correct) & set(b_correct))
    diffs = [
        (1.0 if a_correct[i] else 0.0) - (1.0 if b_correct[i] else 0.0) for i in ids
    ]
    return bootstrap_ci(diffs, n_resamples=n_resamples, seed=seed)


def percentiles(latencies_ms: list[int]) -> tuple[int, int]:
    """(p50, p95) like harness.py::summarize."""
    if not latencies_ms:
        return (0, 0)
    lat = sorted(latencies_ms)
    n = len(lat)
    return (lat[n // 2], lat[min(n - 1, int(n * 0.95))])
