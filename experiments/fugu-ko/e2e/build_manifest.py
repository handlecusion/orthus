#!/usr/bin/env python3
"""Deterministic Tier A manifest builder.

Converts the 11 Tier-A golden asset files named in `inventory.json`
(`"tier": "A"`) into `experiments/fugu-ko/e2e/tier_a.jsonl` records that
follow `experiments/fugu-ko/e2e/manifest_schema.md` exactly.

Run (from repo root, project venv — needs `psycopg` for T3 ground-truth
number sets against the live `orthus_company` DB; falls back to
gate-pass-only scoring if the DB is unreachable):

    .venv/bin/python3 experiments/fugu-ko/e2e/build_manifest.py

Scope: this script only reads `golden/*.json` and `inventory.json` and only
writes `tier_a.jsonl` — it does not touch production orthus code, tables, or
flags (experiment isolation, `docs/fugu-ko-orchestration-plan.md` §6.3).

Design decisions worth knowing before reading the per-task builders below
(also printed as warnings at build time and reported to the caller):

- `t7_holdout.json`, `e3_control.json`, `e3_missed.json` are all measuring
  the exact same production entry point as `t7.json`
  (`orthus/router/decompose.py::should_decompose` — confirmed by reading
  `e3_prefilter.py`'s own docstring). `manifest_schema.md` §4.1's task
  registry has no `e3` code and explicitly forbids inventing one without
  adding a registry row first, so all four files are minted under task
  code `t7` (its registry row already lists both `t7.json` and
  `t7_holdout.json` as legacy sources for the same code). `e3`/`missed_probe`
  /`control_probe`/`t7_holdout` identity is preserved via `tags`, not `task`.
  `inventory.json` itself already uses `task: "t7"` for `t7_holdout.json`;
  only `e3_control.json`/`e3_missed.json` (`task: "e3"` in inventory) are
  actually remapped here.
- `t8.json` (`synthesize`) golden items only carry the parent compound
  question (`q`). The real `synthesize()` signature needs pre-built
  `sub_answers`/`sub_questions`, which the production measurement harness
  (`t8_synth.py`) generates at run time via a *fixed-model* split+leaf-answer
  step — that intermediate state is not part of the frozen golden file, so
  it cannot be captured in `input` here. `input.request` therefore only
  carries `question`; tag `requires_frozen_subs` flags this to the scoring
  harness (best-effort mapping, not a clean 1:1 signature capture).
- T3 ground truth is computed by literally re-running `t3_gold.py`'s
  `SPECS`/`gold_numbers()` logic against the live `orthus_company` DB at
  build time (see `_load_t3_gold`) — those 18/28 items get a verified
  `result` intset (`gold_verified` tag); the remaining 10 items are
  excluded from ground truth by `t3_gold.py` itself (ambiguous target)
  and get `result: null` (`gate_only` tag) — mirroring "게이트통과만 집계"
  in `t3_gold.py`'s own comment.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # experiments/fugu-ko/e2e
FUGU = HERE.parent  # experiments/fugu-ko
GOLDEN = FUGU / "golden"
OUT = HERE / "tier_a.jsonl"
INVENTORY = HERE / "inventory.json"

TIER = "A"

REQUIRED_KEYS = {
    "id", "layer", "task", "entry_point", "input", "expected", "scoring",
    "tier", "provenance", "tags", "frozen",
}


# ---------------------------------------------------------------------------
# frozen_at build tag
# ---------------------------------------------------------------------------

def _git_build_tag() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=FUGU, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if out:
            return f"build:{out}"
    except Exception:  # noqa: BLE001
        pass
    return "build:0000000"


FROZEN_AT = _git_build_tag()


# ---------------------------------------------------------------------------
# §12 canonicalization — sha256(json.dumps(input, sort_keys=True,
# separators=(",",":"), ensure_ascii=False).encode("utf-8"))
# ---------------------------------------------------------------------------

def input_sha256(input_obj: dict) -> str:
    blob = json.dumps(input_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# T3 ground truth — mirrors t3_gold.py::SPECS / gold_numbers() exactly.
# ---------------------------------------------------------------------------

T3_SPECS: dict[str, tuple] = {
    "t3-01": ("협업업무표", "groupby", "상태", None),
    "t3-02": ("협업업무표", "count", None, "properties->>'상태'='완료'"),
    "t3-03": ("협업업무표", "groupby", "담당자", None),
    "t3-04": ("협업업무표", "groupby", "우선순위", None),
    "t3-05": ("협업업무표", "count", None, "properties->>'상태'='보류'"),
    "t3-06": ("Nova 개발 로드맵", "groupby", "상태", None),
    "t3-07": ("Nova 개발 로드맵", "groupby", "우선순위", None),
    "t3-09": ("Nova 개발 로드맵", "groupby", "오너", None),
    "t3-10": ("🎯 NOVA 영입 후보 리스트", "count", None, None),
    "t3-11": ("🎯 NOVA 영입 후보 리스트", "groupby", "Tier", None),
    "t3-12": ("🎯 NOVA 영입 후보 리스트", "groupby", "발송 상태", None),
    "t3-13": ("회사 미팅 기록", "count", None, None),
    "t3-14": ("회사 미팅 기록", "groupby", "파트너사", None),
    "t3-15": ("AI관련툴", "count", None, None),
    "t3-16": ("AI관련툴", "groupby", "카테고리", None),
    "t3-17": ("파트너", "groupby", "분야", None),
    "t3-18": ("배포 공지", "groupby", "공지 상태", None),
    "t3-r09": ("배포 공지", "count", None, None),
}
T3_EMOJI_DB = "🎯 NOVA 영입 후보 리스트"
T3_DSN = "postgresql://orthus:orthus@localhost:5433/orthus_company"

# --- D9 proportional extension (analysis/d9-extension-prereg.md §3) --------
# New t3 items are gold-verified against the frozen D8 snapshot DB
# `orthus_company_0706` (same gold_numbers logic, new SEED=20260723 surfaces).
# All (db, kind, group_key, where) combos are disjoint from T3_SPECS.
T3_D9_SPECS: dict[str, tuple] = {
    "t3-x01": ("배우 오디션 기록", "groupby", "결과", None),
    "t3-x02": ("시사회 초청 명단", "groupby", "참석 여부", None),
    "t3-x03": ("📅 촬영 일정표", "count", None, None),
    "t3-x04": ("외주 계약 ", "groupby", "진행 상태", None),
    "t3-x05": ("정산 내역 ", "count", None, "properties->>'지급 상태'='지급 완료'"),
    "t3-x06": ("보도자료 배포처", "groupby", "매체 유형", None),
    "t3-x07": ("편집본 버전 관리", "count", None, None),
    "t3-x08": ("🎧 사운드 소스 관리", "groupby", "정리 상태", None),
    "t3-x09": ("장면 콘티 검수", "groupby", "우선도", None),
    "t3-x10": ("의상 소품 재고 ", "count", None, "properties->>'대여 여부'='대여 중'"),
    "t3-x11": ("🎼 배경음악 라이선스", "groupby", "라이선스 종류", None),
}
T3_D9_DSN = "postgresql://orthus:orthus@localhost:5433/orthus_company_0706"
T3_D9_EMOJI_DBS = {"📅 촬영 일정표", "🎧 사운드 소스 관리", "🎼 배경음악 라이선스"}
D9_TAG = "d9_ext"


def _load_t3_gold(
    specs: dict[str, tuple] | None = None, dsn: str = T3_DSN,
) -> dict[str, list[int] | None]:
    """Live-query the given DB for T3 ground-truth number sets.

    Falls back to `None` per id (item still emitted, tagged
    `gold_unavailable_at_build` instead of `gold_verified`) if the DB or
    the `psycopg` driver isn't reachable at build time — `gate_pass` still
    stands on its own for those items.
    """
    if specs is None:
        specs = T3_SPECS
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        print(
            "  ! psycopg not importable — T3 ground-truth numbers unavailable "
            "(gate_pass-only expected.value for SPECS items)",
            file=sys.stderr,
        )
        return {i: None for i in specs}
    try:
        conn = psycopg.connect(dsn, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        print(
            f"  ! T3 DB unreachable ({type(e).__name__}: {e}) — ground-truth "
            "numbers unavailable (gate_pass-only expected.value for SPECS items)",
            file=sys.stderr,
        )
        return {i: None for i in specs}
    out: dict[str, list[int] | None] = {}
    with conn, conn.cursor() as cur:
        for qid, (db, kind, gkey, where) in specs.items():
            w = "scope='company' AND db_name=%s" + (f" AND {where}" if where else "")
            if kind == "count":
                cur.execute(f"SELECT count(*) FROM notion_rows WHERE {w}", (db,))
                nums = {int(cur.fetchone()[0])}
            else:
                cur.execute(
                    f"SELECT count(*) FROM notion_rows WHERE {w} GROUP BY properties->>%s",
                    (db, gkey),
                )
                nums = {int(r[0]) for r in cur.fetchall()}
            out[qid] = sorted(nums)
    conn.close()
    return out


# ---------------------------------------------------------------------------
# golden loader
# ---------------------------------------------------------------------------

def _items(fname: str) -> list[dict]:
    return json.loads((GOLDEN / fname).read_text(encoding="utf-8"))["items"]


def _items_multi(fnames: list[str], key: str) -> list[dict]:
    """Concatenate items across `fnames` and dedup by normalized `key`.

    Base file(s) are listed first so their items always win a collision;
    normalization is `str.strip()` and the FIRST occurrence is kept. Item
    schemas are shared across base/holdout files (verified), so the per-task
    builders read the merged list exactly as they read a single base file.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for fname in fnames:
        for it in _items(fname):
            k = it[key].strip()
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
    return out


def _ext_items(fname: str) -> list[dict]:
    """D9 extension golden file items — empty when the file doesn't exist."""
    path = GOLDEN / fname
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["items"]


def _with_ext(base: list[dict], ext_fname: str, key: str) -> list[dict]:
    """Append D9 extension items to an already-merged base list.

    The D9 extension (`*_d9ext.json`) and the holdout/aug merge (`_items_multi`)
    are two independent golden expansions that landed on separate branches; both
    are kept. Collisions on normalized `key` drop the extension item so the
    incumbent base/holdout id stays put — the same first-occurrence-wins rule
    `_items_multi` uses, which is what keeps the pre-existing manifest records
    byte-identical.
    """
    seen: set[str] = {it[key].strip() for it in base}
    out = list(base)
    for it in _ext_items(ext_fname):
        k = it[key].strip()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _rec(task: str, entry_point: str, request: dict, expected: dict, scoring: str,
         tags: list[str], source_id: str, item_tags: list[str] | None = None) -> dict:
    """Assemble one manifest record.

    `item_tags` are golden item-level `tags` (e.g. `gen_*` provenance) merged
    (dedup, order-preserving) into the builder-synthesized `tags` so they flow
    to `tier_a.jsonl` for §4.2 post-hoc audit. Existing golden items carry no
    `tags` field, so `item_tags` is `None` for them and output is byte-identical
    to the pre-merge builder (`[*tags, golden_id_...]`).
    """
    merged = list(tags)
    for t in (item_tags or []):
        if t not in merged:
            merged.append(t)
    merged.append(f"golden_id_{source_id}")
    return {
        "task": task,
        "entry_point": entry_point,
        "input": {"request": request},
        "expected": expected,
        "scoring": scoring,
        "tags": merged,
    }


# ---------------------------------------------------------------------------
# T2 — wiki_qa (judge, pairwise vs baseline)
# ---------------------------------------------------------------------------

def build_t2() -> list[dict]:
    recs = []
    for it in _items("t2.json"):
        req = {"question": it["q"], "k": 5, "scope": "company", "learn": False, "record_gaps": False}
        expected = {"kind": "judge", "rubric": "evidence-pairwise-winrate", "reference": None}
        tags = ["t2", "wiki_qa", "pairwise_vs_baseline"]
        recs.append(_rec("t2", "orthus/wiki/qa.py::ask", req, expected, "judge", tags, it["id"]))
    return recs


# ---------------------------------------------------------------------------
# T3 — structured (deterministic: gate_pass + real-DB intset match)
# ---------------------------------------------------------------------------

def build_t3() -> list[dict]:
    gold = _load_t3_gold()
    recs = []
    # aug_t3.json items carry their OWN frozen `expected`
    # ({"kind":"exact","value":{"gate_pass":true,"result":<sorted int set>}})
    # + reproducible `spec`, computed against the live orthus_company DB at
    # generation time — they do NOT live in T3_SPECS and are not recomputed
    # here (mirrors build_tier_b's inline-gold path). Base t3/t3_holdout items
    # have no `expected` field and keep the live `_load_t3_gold` path unchanged.
    for it in _items_multi(["t3.json", "t3_holdout.json", "aug_t3.json"], "q"):
        qid = it["id"]
        req = {"question": it["q"], "scope": "company"}
        tags = ["t3", "structured"]
        if it.get("intent"):
            tags.append(f"intent_{it['intent']}")
        item_expected = it.get("expected")
        if isinstance(item_expected, dict) and item_expected.get("kind"):
            expected = item_expected
            tags.append("gold_embedded")
        else:
            if qid in T3_SPECS:
                g = gold.get(qid)
                tags.append("gold_verified" if g is not None else "gold_unavailable_at_build")
            else:
                g = None
                tags.append("gate_only")
            expected = {"kind": "exact", "value": {"gate_pass": True, "result": g}}
        if it.get("db") == T3_EMOJI_DB:
            tags.append("emoji-db")
        if it.get("src") == "query_runs":
            tags.append("prod_log_derived")
        recs.append(_rec("t3", "orthus/structured/query.py::query_structured", req, expected,
                          "deterministic", tags, qid, item_tags=it.get("tags")))

    ext = _ext_items("t3_d9ext.json")
    if ext:
        gold_d9 = _load_t3_gold(T3_D9_SPECS, T3_D9_DSN)
        for it in ext:
            qid = it["id"]
            assert qid in T3_D9_SPECS, f"t3 d9ext item {qid} has no spec"
            req = {"question": it["q"], "scope": "company"}
            tags = ["t3", "structured"]
            if it.get("intent"):
                tags.append(f"intent_{it['intent']}")
            g = gold_d9.get(qid)
            tags.append("gold_verified" if g is not None else "gold_unavailable_at_build")
            if it.get("db") in T3_D9_EMOJI_DBS:
                tags.append("emoji-db")
            tags.append(D9_TAG)
            expected = {"kind": "exact", "value": {"gate_pass": True, "result": g}}
            recs.append(_rec("t3", "orthus/structured/query.py::query_structured", req, expected,
                              "deterministic", tags, qid))
    return recs


# ---------------------------------------------------------------------------
# T5 — routing (deterministic exact)
# ---------------------------------------------------------------------------

def _routing_extra_items() -> list[dict]:
    """Genuinely Route-labeled `{id, q, expected}` items from three files whose
    on-disk shape is NOT the `{"items": [...]}` envelope `_items_multi` reads:

    - `routing_holdout.json`: top-level `{"main": [...], "control": [...]}`.
      Only `main` (290) is drop-in `expected`-labeled; `control` (40) uses a
      DIFFERENT field `rule_route` (not `expected`) and is EXCLUDED.
    - `routing_graph_golden.json`: top-level `{"main": [...]}` (63, graph).
    - `routing_holdout_tn.json`: a flat list (40, structured), all usable.
    """
    def _load(fname: str):
        return json.loads((GOLDEN / fname).read_text(encoding="utf-8"))

    tn = _load("routing_holdout_tn.json")
    return (
        list(_load("routing_holdout.json")["main"])
        + list(_load("routing_graph_golden.json")["main"])
        + list(tn if isinstance(tn, list) else tn["items"])
    )


def build_t5() -> list[dict]:
    recs = []
    # base-first: the existing 4 `{"items": [...]}` files win any `q` collision;
    # the 3 extra Route-labeled files (main/graph subsets only) are appended and
    # deduped by normalized `q` under the same first-occurrence-wins rule.
    base = _items_multi(
        ["t5.json", "t5_holdout.json", "t5_v2_holdout.json", "t5_holdout_draft.json"], "q"
    )
    seen: set[str] = {it["q"].strip() for it in base}
    merged = list(base)
    # routing extras first (existing ids stay put), then aug_t5 items appended
    # LAST so any base/extra `q` collision keeps the incumbent.
    for it in [*_routing_extra_items(), *_items("aug_t5.json")]:
        k = it["q"].strip()
        if k in seen:
            continue
        seen.add(k)
        merged.append(it)
    for it in _with_ext(merged, "t5_d9ext.json", "q"):
        req = {"question": it["q"]}
        expected = {"kind": "exact", "value": it["expected"]}
        tags = ["t5", "routing", f"expected_{it['expected']}"]
        if it["id"].startswith("t5-x"):
            tags.append(D9_TAG)
        recs.append(_rec("t5", "orthus/router/route.py::classify", req, expected,
                          "deterministic", tags, it["id"], item_tags=it.get("tags")))
    return recs


# ---------------------------------------------------------------------------
# T6 — intent (deterministic exact, 7-way)
# ---------------------------------------------------------------------------

def build_t6() -> list[dict]:
    recs = []
    base_t6 = _items_multi(
        ["t6.json", "t6_holdout.json", "t6_holdout_draft.json", "aug_t6.json"], "q"
    )
    for it in _with_ext(base_t6, "t6_d9ext.json", "q"):
        req = {"question": it["q"], "allow_commands": True}
        expected = {"kind": "exact", "value": it["expected"]}
        tags = ["t6", "intent", f"expected_{it['expected']}"]
        if it["id"].startswith("t6-x"):
            tags.append(D9_TAG)
        recs.append(_rec("t6", "orthus/router/route.py::classify_intent", req, expected,
                          "deterministic", tags, it["id"], item_tags=it.get("tags")))
    return recs


# ---------------------------------------------------------------------------
# T7 family — decompose gate (deterministic exact boolean); t7.json,
# t7_holdout.json, e3_control.json, e3_missed.json all share task code "t7"
# (see module docstring). Aggregate ceilings (recall>=80%, mis-split<=5% for
# E3; gate accuracy for t7/t7_holdout) are computed set-wide by the scoring
# harness over the tag groups below, NOT per item — each item here only
# encodes its own single expected gate boolean.
# ---------------------------------------------------------------------------

def _t7_gate_record(it: dict, ext_tier: int | None = None) -> tuple[dict, dict, list[str]]:
    req = {"question": it["q"]}
    if ext_tier is not None:
        req["ext_tier"] = ext_tier
    compound = bool(it.get("compound"))
    expected = {"kind": "exact", "value": compound}
    tags = ["t7", "decompose"]
    if compound:
        parts = max(2, int(it.get("expected_parts", 2)))
        tags.append(f"split_min_parts_{parts}")
    return req, expected, tags


def build_t7_family() -> list[dict]:
    recs = []
    entry_point = "orthus/router/decompose.py::should_decompose"

    for it in _items_multi(["t7.json", "t7_holdout_draft.json"], "q"):
        req, expected, tags = _t7_gate_record(it)
        tags.append("base_golden")
        recs.append(_rec("t7", entry_point, req, expected, "deterministic", tags, it["id"]))

    for it in _items("t7_holdout.json"):
        req, expected, tags = _t7_gate_record(it, ext_tier=3)
        tags += ["t7_holdout", "prefilter_ext_tier_3", "aggregate_scored_set_wide"]
        recs.append(_rec("t7", entry_point, req, expected, "deterministic", tags, it["id"]))

    for it in _items("e3_control.json"):
        req, expected, tags = _t7_gate_record(it)
        tags += ["e3", "control_probe", "adversarial", "aggregate_scored_set_wide"]
        trap = it.get("trap") or "none"
        tags.append(f"trap_{trap.replace('+', '_')}")
        for component in trap.split("+"):
            if component != "none":
                tags.append(f"trap_component_{component}")
        recs.append(_rec("t7", entry_point, req, expected, "deterministic", tags, it["id"]))

    for it in _items("e3_missed.json"):
        req, expected, tags = _t7_gate_record(it)
        tags += ["e3", "missed_probe", "aggregate_scored_set_wide"]
        if it.get("type"):
            tags.append(f"type_{it['type']}")
        recs.append(_rec("t7", entry_point, req, expected, "deterministic", tags, it["id"]))

    # aug_t7.json emitted LAST (after every existing t7-family block) so the id
    # sequence of base/holdout/e3 items is untouched by augmentation, and any
    # `q` collision with an already-emitted item keeps the incumbent. aug items
    # ride the plain gate path (no ext_tier, no e3) — tag `base_golden` only,
    # never the forbidden set-wide tags
    # (missed_probe / control_probe / aggregate_scored_set_wide).
    seen_q: set[str] = {r["input"]["request"]["question"].strip() for r in recs}
    for it in _items("aug_t7.json"):
        if it["q"].strip() in seen_q:
            continue
        seen_q.add(it["q"].strip())
        req, expected, tags = _t7_gate_record(it)
        tags.append("base_golden")
        recs.append(_rec("t7", entry_point, req, expected, "deterministic", tags, it["id"],
                          item_tags=it.get("tags")))

    # D9 extension: scored per-item exactly like the base golden set (no
    # aggregate_scored_set_wide / probe tags, no ext_tier in the request).
    # Emitted after the aug_t7 block, and skipped on a `q` collision so the
    # already-emitted incumbent keeps its id.
    for it in _ext_items("t7_d9ext.json"):
        if it["q"].strip() in seen_q:
            continue
        seen_q.add(it["q"].strip())
        req, expected, tags = _t7_gate_record(it)
        tags.append(D9_TAG)
        recs.append(_rec("t7", entry_point, req, expected, "deterministic", tags, it["id"]))

    return recs


# ---------------------------------------------------------------------------
# T8 — synthesize (judge, pairwise vs baseline; see docstring re: frozen subs)
# ---------------------------------------------------------------------------

def build_t8() -> list[dict]:
    recs = []
    for it in _items("t8.json"):
        req = {"question": it["q"]}
        expected = {"kind": "judge", "rubric": "evidence-pairwise-winrate", "reference": None}
        tags = ["t8", "synthesize", "pairwise_vs_baseline", "requires_frozen_subs"]
        recs.append(_rec("t8", "orthus/router/decompose.py::synthesize", req, expected,
                          "judge", tags, it["id"]))
    return recs


# ---------------------------------------------------------------------------
# T9 — graph_bind (deterministic exact: intent primary, subjects secondary)
# ---------------------------------------------------------------------------

def build_t9() -> list[dict]:
    recs = []
    for it in _with_ext(
        _items_multi(["t9_graph_bind.json", "aug_t9.json"], "q"), "t9_d9ext.json", "q"
    ):
        req = {"question": it["q"]}
        expected = {"kind": "exact", "value": {"intent": it["intent"], "subjects": list(it["subjects"])}}
        tags = ["t9", "graph_bind", f"intent_{it['intent']}"]
        if it["id"].startswith("g-x"):
            tags.append(D9_TAG)
        recs.append(_rec("t9", "orthus/router/graph.py::bind_graph_params", req, expected,
                          "deterministic", tags, it["id"], item_tags=it.get("tags")))
    return recs


# ---------------------------------------------------------------------------
# T10 — delegation_extract (deterministic exact; FP-safety classifier).
# Negative-control items (is_delegation=false) use value: null per
# manifest_schema.md §7.1's own t10 convention.
# ---------------------------------------------------------------------------

def build_t10() -> list[dict]:
    recs = []
    for it in _with_ext(
        _items_multi(["t10_delegation.json", "t10_holdout2.json", "aug_t10.json"], "text"),
        "t10_d9ext.json",
        "text",
    ):
        req = {"text": it["text"]}
        is_del = bool(it["is_delegation"])
        if is_del:
            expected = {"kind": "exact", "value": {"assignee": it.get("assignee", ""), "mode": it.get("mode", "")}}
            tags = ["t10", "delegation_extract", f"mode_{it.get('mode', '')}", "no_instruction_field_in_golden"]
        else:
            expected = {"kind": "exact", "value": None}
            tags = ["t10", "delegation_extract", "delegation_fp_probe", "negative_control", "adversarial"]
        if it["id"].startswith("d-x"):
            tags.append(D9_TAG)
        recs.append(_rec("t10", "orthus/agentwork/delegation.py::extract_delegation_intent", req,
                          expected, "deterministic", tags, it["id"], item_tags=it.get("tags")))
    return recs


BUILDERS = [build_t2, build_t3, build_t5, build_t6, build_t7_family, build_t8, build_t9, build_t10]


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def assemble() -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for builder in BUILDERS:
        for r in builder():
            grouped.setdefault(r["task"], []).append(r)

    records: list[dict] = []
    for task, items in grouped.items():
        base_idx = 0
        ext_idx = 1000  # d9_ext items live in the 1001+ id band (prereg §3)
        for r in items:
            if D9_TAG in r["tags"]:
                ext_idx += 1
                idx = ext_idx
            else:
                base_idx += 1
                idx = base_idx
            input_obj = r["input"]
            rec = {
                "id": f"{TIER}-{task}-{idx:04d}",
                "layer": "L1",
                "task": task,
                "entry_point": r["entry_point"],
                "input": input_obj,
                "expected": r["expected"],
                "scoring": r["scoring"],
                "tier": TIER,
                "provenance": "golden",
                "tags": r["tags"],
                "frozen": {"input_sha256": input_sha256(input_obj), "frozen_at": FROZEN_AT},
            }
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# validate + report
# ---------------------------------------------------------------------------

def validate_and_report(records: list[dict]) -> None:
    import random

    print(f"\n[build_manifest] total records: {len(records)}")

    per_task: dict[str, int] = {}
    for r in records:
        per_task[r["task"]] = per_task.get(r["task"], 0) + 1
    print("[build_manifest] per-task counts:")
    for task, n in per_task.items():
        print(f"    {task}: {n}")

    # keys + kind/scoring consistency
    kind_to_scoring = {"exact": "deterministic", "metric": "deterministic",
                        "structural": "deterministic", "judge": "judge"}
    ids_seen: set[str] = set()
    for r in records:
        assert set(r.keys()) == REQUIRED_KEYS, f"{r.get('id')}: key set mismatch {set(r.keys())}"
        assert r["id"] not in ids_seen, f"duplicate id {r['id']}"
        ids_seen.add(r["id"])
        kind = r["expected"]["kind"]
        assert r["scoring"] == kind_to_scoring[kind], f"{r['id']}: scoring/kind mismatch"
        # round-trip JSON validity
        json.loads(json.dumps(r, ensure_ascii=False))
    print(f"[build_manifest] all {len(records)} records: valid JSON, required keys present, ids unique. OK")

    # recompute frozen.input_sha256 for 3 random records
    sample = random.Random(1234).sample(records, min(3, len(records)))
    for r in sample:
        recomputed = input_sha256(r["input"])
        ok = recomputed == r["frozen"]["input_sha256"]
        print(f"[build_manifest] hash self-check {r['id']}: {'OK' if ok else 'MISMATCH'} "
              f"({recomputed[:12]}... vs {r['frozen']['input_sha256'][:12]}...)")
        assert ok, f"{r['id']}: input_sha256 self-check failed"

    # cross-check total vs inventory Tier A item_counts
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    tier_a_assets = [a for a in inv["assets"] if a["tier"] == "A"]
    expected_total = sum(a["item_count"] for a in tier_a_assets)
    print(f"\n[build_manifest] inventory Tier A assets: {len(tier_a_assets)}, "
          f"sum(item_count) = {expected_total}")
    if expected_total == len(records):
        print(f"[build_manifest] COUNT MATCH: {len(records)} == {expected_total}")
    else:
        print(f"[build_manifest] COUNT MISMATCH: manifest {len(records)} != inventory {expected_total} "
              "— see per-asset breakdown below")
        for a in tier_a_assets:
            print(f"    {a['asset_file']:28} task={a['task']:5} item_count={a['item_count']}")


def _load_frozen_lines() -> dict[str, str]:
    """Existing tier_a.jsonl lines keyed by id — frozen items are replayed
    byte-identically on rebuild (manifest_schema.md §2 stability contract:
    committed ids never change; only genuinely new ids are minted fresh)."""
    if not OUT.exists():
        return {}
    frozen: dict[str, str] = {}
    for line in OUT.read_text(encoding="utf-8").splitlines():
        if line.strip():
            frozen[json.loads(line)["id"]] = line
    return frozen


def main() -> None:
    prior = _load_frozen_lines()
    records = assemble()
    lines: list[str] = []
    replayed = 0
    for rec in records:
        old_line = prior.get(rec["id"])
        if old_line is not None:
            old = json.loads(old_line)
            # drift gate: a frozen id may never change its input (§12/§13).
            assert old["frozen"]["input_sha256"] == input_sha256(rec["input"]), (
                f"{rec['id']}: input drifted vs frozen manifest — retire the id "
                "instead of mutating it (manifest_schema.md §2)"
            )
            lines.append(old_line)
            replayed += 1
        else:
            lines.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    with OUT.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[build_manifest] wrote {len(records)} lines -> {OUT} "
          f"({replayed} frozen lines replayed byte-identically, "
          f"{len(records) - replayed} newly minted)")
    validate_and_report([json.loads(line) for line in lines])


if __name__ == "__main__":
    main()
