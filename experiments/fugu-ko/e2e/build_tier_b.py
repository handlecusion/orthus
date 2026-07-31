#!/usr/bin/env python3
"""Deterministic Tier B (independent-holdout) manifest builder.

Converts the L1 Tier-B independent holdouts named in `inventory.json`
(`tier=="B"` AND `reexecute==true`, EXCLUDING the 3 draft supersets) into
`experiments/fugu-ko/e2e/tier_b.jsonl`, following `manifest_schema.md`.

Reuses `build_manifest.py`'s per-task request shapes + §12 canonicalization
(`input_sha256`) + T3 live-DB gold logic verbatim (imported, never modified).
Does NOT touch production orthus code, `tier_a.jsonl`, `build_manifest.py`,
`l2/*`, `manifest_schema.md`, `STATE.md`, or `golden/*`.

Full manifest = tier_a.jsonl (L1 A) + l2/g*.jsonl (all L2) + tier_b.jsonl
(L1 B, THIS FILE). L2 items are NOT duplicated here; Tier-B L1 ids use
`B-<task>-` (t2/t3/t5/t6/t10) — disjoint from L2's `B-g<n>-`.

Run (repo root, project venv — psycopg needed for t3_holdout live gold):

    .venv/bin/python3 experiments/fugu-ko/e2e/build_tier_b.py

Design decisions (also printed at build time):

- Small independent holdouts wrapped FULLY: t2_holdout(30), t3_holdout(41),
  t3_v2_holdout(4), t5_holdout(28), t5_v2_holdout(20), routing_holdout_tn(40),
  t6_holdout(20), t10_holdout2(32). Massive T3 d8_holdout(1438) wrapped FULLY
  as the canonical T3 Tier-B holdout (§1b D10 pre-registered LOCK n=1,438,
  McNemar p=5.6e-187 is on exactly this set -> instant statistical backing).
- t3_unseen_holdout(463) / t3_fresh_holdout(1063) are NOT per-item wrapped:
  they are sampled-on-demand at bench time (plan §2); d8 is the canonical set
  and the power calc confirms n=1,438 is adequate (see SUPPLEMENTARY note +
  the power table). No deterministic sub-sample is needed.
- t3_d8 / t3_v2 store gold INLINE (each item carries `gold` + `spec`) -> the
  intset is FROZEN directly from the file. t3_holdout stores NO inline gold ->
  recomputed live from `t3_holdout.HOLDOUT_SPECS` against `orthus_company`
  (mirrors build_manifest `_load_t3_gold` for base t3). If the DB is
  unreachable the item is emitted with
  `expected={"kind":"exact","source":"live_scorer:t3_holdout.py",
  "pending_live_compute":true}` and tagged `gold_unavailable_at_build`.
- routing_holdout_tn is filed under registry code `t5` (task="routing" ->
  t5=routing in schema §4.1; each item's raw `expected` is a route label
  "structured"). Its false-positive/genuine-zero identity (consumed by
  e6_cascade.py as a TN control) is preserved in `tags` + `no_confident_zero`
  invariant, mirroring build_manifest's e3->t7 remap precedent.
- t10_holdout2 scorer `t10_delegation.py` HARDCODES golden/t10_delegation.json
  with no `--golden` override -> every t10 item is tagged
  `scorer_golden_override_required` + `golden_file_t10_holdout2` so the harness
  passes `--golden golden/t10_holdout2.json`.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/e2e
FUGU = HERE.parent                                # experiments/fugu-ko
GOLDEN = FUGU / "golden"
OUT = HERE / "tier_b.jsonl"
LOCK = HERE / "freeze.lock"
INVENTORY = HERE / "inventory.json"
TIER_A = HERE / "tier_a.jsonl"
L2_DIR = HERE / "l2"

sys.path.insert(0, str(FUGU))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FUGU / "embedding"))

import build_manifest as bm          # noqa: E402  (input_sha256, FROZEN_AT, T3_DSN, T3_EMOJI_DB)
import power as pw                    # noqa: E402  (embedding/power.py — generalized below)

TIER = "B"
PROV = "independent_holdout"
REQUIRED_KEYS = {
    "id", "layer", "task", "entry_point", "input", "expected", "scoring",
    "tier", "provenance", "tags", "frozen",
}

input_sha256 = bm.input_sha256
FROZEN_AT = bm.FROZEN_AT


# ---------------------------------------------------------------------------
# golden loaders
# ---------------------------------------------------------------------------

def _load(fname: str) -> dict | list:
    return json.loads((GOLDEN / fname).read_text(encoding="utf-8"))


def _items(fname: str) -> list[dict]:
    d = _load(fname)
    return d if isinstance(d, list) else d["items"]


# ---------------------------------------------------------------------------
# T3 holdout live gold — mirrors build_manifest._load_t3_gold, keyed by
# t3_holdout.HOLDOUT_SPECS instead of base T3_SPECS.
# ---------------------------------------------------------------------------

def _load_t3_holdout_gold() -> dict[str, list[int] | None]:
    from t3_holdout import HOLDOUT_SPECS
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        print("  ! psycopg not importable — t3_holdout gold unavailable (pending_live_compute)", file=sys.stderr)
        return {i: None for i in HOLDOUT_SPECS}
    try:
        conn = psycopg.connect(bm.T3_DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"  ! t3_holdout DB unreachable ({type(e).__name__}: {e}) — pending_live_compute", file=sys.stderr)
        return {i: None for i in HOLDOUT_SPECS}
    out: dict[str, list[int] | None] = {}
    with conn, conn.cursor() as cur:
        for qid, (db, kind, gkey, where) in HOLDOUT_SPECS.items():
            w = "scope='company' AND db_name=%s" + (f" AND {where}" if where else "")
            if kind == "count":
                cur.execute(f"SELECT count(*) FROM notion_rows WHERE {w}", (db,))
                nums = {int(cur.fetchone()[0])}
            else:
                cur.execute(f"SELECT count(*) FROM notion_rows WHERE {w} GROUP BY properties->>%s", (db, gkey))
                nums = {int(r[0]) for r in cur.fetchall()}
            out[qid] = sorted(nums)
    conn.close()
    return out


# ---------------------------------------------------------------------------
# per-source record emitters (task, entry_point, request, expected, tags,
# invariants) — request shapes copied verbatim from build_manifest builders.
# ---------------------------------------------------------------------------

def _rec(task, entry_point, request, expected, scoring, tags, invariants, source_id):
    return {
        "task": task, "entry_point": entry_point,
        "input": {"request": request}, "expected": expected, "scoring": scoring,
        "tags": [*tags, f"golden_id_{source_id}"],
        "invariants": invariants,
    }


def emit_t2_holdout():
    out = []
    for it in _items("t2_holdout.json"):
        req = {"question": it["q"], "k": 5, "scope": "company", "learn": False, "record_gaps": False}
        exp = {"kind": "judge", "rubric": "evidence-pairwise-winrate", "reference": None}
        tags = ["t2", "wiki_qa", "holdout", "grounded_judge", "pairwise_vs_baseline"]
        out.append(_rec("t2", "orthus/wiki/qa.py::ask", req, exp, "judge", tags, ["model_fallback_zero"], it["id"]))
    return out


def emit_t3_holdout(gold):
    from t3_holdout import HOLDOUT_SPECS
    out = []
    for it in _items("t3_holdout.json"):
        qid = it["id"]
        req = {"question": it["q"], "scope": "company"}
        tags = ["t3", "structured", "holdout"]
        if it.get("intent"):
            tags.append(f"intent_{it['intent']}")
        emoji = it.get("db") == bm.T3_EMOJI_DB
        if emoji:
            tags.append("emoji-db")
        g = gold.get(qid)
        if g is None:
            exp = {"kind": "exact", "source": "live_scorer:t3_holdout.py", "pending_live_compute": True}
            tags.append("gold_unavailable_at_build")
        else:
            exp = {"kind": "exact", "value": {"gate_pass": True, "result": g}}
            tags.append("gold_verified")
        inv = ["model_fallback_zero"] + (["no_confident_zero"] if (emoji or g in ([], [0])) else [])
        out.append(_rec("t3", "orthus/structured/query.py::query_structured", req, exp, "deterministic", tags, inv, qid))
    return out


def emit_t3_inline(fname, extra_tags):
    """t3_v2 / t3_d8 — gold + spec inline; freeze the intset directly."""
    out = []
    for it in _items(fname):
        req = {"question": it["q"], "scope": "company"}
        result = sorted({int(x) for x in it["gold"]})
        exp = {"kind": "exact", "value": {"gate_pass": True, "result": result}}
        tags = ["t3", "structured", "holdout", "gold_embedded", *extra_tags]
        if it.get("tag"):
            tags.append(f"tag_{it['tag']}")
        genuine_zero = result in ([], [0])
        if genuine_zero:
            tags.append("genuine_zero")
        inv = ["model_fallback_zero"] + (["no_confident_zero"] if genuine_zero else [])
        out.append(_rec("t3", "orthus/structured/query.py::query_structured", req, exp, "deterministic", tags, inv, it["id"]))
    return out


def emit_t5_route(fname, extra_tags):
    out = []
    for it in _items(fname):
        req = {"question": it["q"]}
        exp = {"kind": "exact", "value": it["expected"]}
        tags = ["t5", "routing", "holdout", f"expected_{it['expected']}", *extra_tags]
        out.append(_rec("t5", "orthus/router/route.py::classify", req, exp, "deterministic", tags,
                        ["model_fallback_zero", "no_confident_zero"], it["id"]))
    return out


def emit_routing_tn():
    out = []
    for it in _load("routing_holdout_tn.json"):  # top-level list
        req = {"question": it["q"]}
        exp = {"kind": "exact", "value": it["expected"]}  # "structured"
        tags = ["t5", "routing", "holdout", f"expected_{it['expected']}", "routing_tn",
                "false_positive_control", "genuine_zero", "cross_plane_guard",
                "e6_cascade_reused", "aggregate_scored_set_wide", "negative_control"]
        out.append(_rec("t5", "orthus/router/route.py::classify", req, exp, "deterministic", tags,
                        ["model_fallback_zero", "no_confident_zero"], it["id"]))
    return out


def emit_t6_holdout():
    out = []
    for it in _items("t6_holdout.json"):
        req = {"question": it["q"], "allow_commands": True}
        exp = {"kind": "exact", "value": it["expected"]}
        tags = ["t6", "intent", "holdout", f"expected_{it['expected']}", "llm_decided"]
        out.append(_rec("t6", "orthus/router/route.py::classify_intent", req, exp, "deterministic", tags,
                        ["model_fallback_zero", "no_confident_zero"], it["id"]))
    return out


def emit_t10_holdout2():
    out = []
    for it in _items("t10_holdout2.json"):
        req = {"text": it["text"]}
        override = ["scorer_golden_override_required", "golden_file_t10_holdout2"]
        if bool(it["is_delegation"]):
            exp = {"kind": "exact", "value": {"assignee": it.get("assignee", ""), "mode": it.get("mode", "")}}
            tags = ["t10", "delegation_extract", "holdout", f"mode_{it.get('mode', '')}",
                    "no_instruction_field_in_golden", *override]
        else:
            exp = {"kind": "exact", "value": None}
            tags = ["t10", "delegation_extract", "holdout", "delegation_fp_probe",
                    "negative_control", "adversarial", *override]
        out.append(_rec("t10", "orthus/agentwork/delegation.py::extract_delegation_intent", req, exp,
                        "deterministic", tags, ["model_fallback_zero"], it["id"]))
    return out


# Fixed emission order -> deterministic per-(tier,task) id sequence.
def assemble():
    gold = _load_t3_holdout_gold()
    ordered = [
        emit_t2_holdout(),
        emit_t3_holdout(gold),
        emit_t3_inline("t3_v2_holdout.json", ["prod_log_derived", "real_log_anchor", "v2"]),
        emit_t3_inline("t3_d8_holdout.json", ["d8", "canonical_t3_holdout", "distinct_spec_weighted"]),
        emit_t5_route("t5_holdout.json", ["llm_decided"]),
        emit_t5_route("t5_v2_holdout.json", ["prod_log_derived", "real_log_anchor", "v2"]),
        emit_routing_tn(),
        emit_t6_holdout(),
        emit_t10_holdout2(),
    ]
    grouped: dict[str, list[dict]] = {}
    for block in ordered:
        for r in block:
            grouped.setdefault(r["task"], []).append(r)

    records = []
    for task, items in grouped.items():
        for idx, r in enumerate(items, start=1):
            rec = {
                "id": f"{TIER}-{task}-{idx:04d}",
                "layer": "L1", "task": task, "entry_point": r["entry_point"],
                "input": r["input"], "expected": r["expected"], "scoring": r["scoring"],
                "tier": TIER, "provenance": PROV, "tags": r["tags"],
                "frozen": {"input_sha256": input_sha256(r["input"]), "frozen_at": FROZEN_AT},
            }
            if r["invariants"]:
                rec["invariants"] = r["invariants"]
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# freeze.lock — per-tier manifest_sha256 across tier_x.jsonl + l2 built items.
# manifest_sha256 = sha256("<id>:<input_sha256>\n"... sorted by id) (§13).
# pending items (frozen.input_sha256 null / __pending_user_fill__) excluded+counted.
# ---------------------------------------------------------------------------

def _is_pending(r):
    fz = (r.get("frozen") or {}).get("input_sha256")
    req = (r.get("input") or {}).get("request")
    return fz is None or (isinstance(req, dict) and req.get("__pending_user_fill__") is True)


def _read_jsonl(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _tier_hash(pairs):
    body = "\n".join(f"{i}:{h}" for i, h in sorted(pairs))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_freeze_lock(tier_b_records):
    files = {"tier_a.jsonl": _read_jsonl(TIER_A), "tier_b.jsonl": tier_b_records}
    for p in sorted(L2_DIR.glob("g*.jsonl")):
        files[f"l2/{p.name}"] = _read_jsonl(p)

    per_file = {}
    tier_pairs = {"A": [], "B": []}
    tier_pending = {"A": 0, "B": 0}
    for name, rows in files.items():
        a = b = pa = pb = 0
        for r in rows:
            t = r["tier"]
            if _is_pending(r):
                tier_pending[t] += 1
                (pa, pb) = (pa + (t == "A"), pb + (t == "B"))
                continue
            tier_pairs[t].append((r["id"], r["frozen"]["input_sha256"]))
            (a, b) = (a + (t == "A"), b + (t == "B"))
        per_file[name] = {"total": len(rows), "tier_a_hashed": a, "tier_b_hashed": b,
                          "tier_a_pending": pa, "tier_b_pending": pb,
                          "provenance": sorted({r["provenance"] for r in rows})}

    return {
        "note": "🚦 drift gate. manifest_sha256 per manifest_schema.md §13 = sha256 over "
                "'<id>:<input_sha256>' for every non-pending item in that tier (tier_x.jsonl + "
                "l2 tier-x built items), sorted by id, newline-joined. Pending "
                "(frozen.input_sha256 null / __pending_user_fill__) excluded + counted.",
        "generated_at": "<BUILD_TIMESTAMP_PLACEHOLDER>",
        "frozen_at_build": FROZEN_AT,
        "tier_a": {"count": len(tier_pairs["A"]), "pending_excluded": tier_pending["A"],
                   "manifest_sha256": _tier_hash(tier_pairs["A"])},
        "tier_b": {"count": len(tier_pairs["B"]), "pending_excluded": tier_pending["B"],
                   "manifest_sha256": _tier_hash(tier_pairs["B"])},
        "per_file": per_file,
        "supplementary_not_wrapped": {
            "policy": "Sampled-on-demand at bench time (plan §2). d8_holdout is the canonical "
                      "T3 Tier-B set; power calc confirms n=1,438 adequate (no sub-sample needed).",
            "t3_unseen_holdout.json": {"n": 463, "wrapped": False},
            "t3_fresh_holdout.json": {"n": 1063, "wrapped": False},
            "t3_d8_holdout.json": {"n": 1438, "wrapped": True, "canonical": True,
                                   "lock": "§1b D10 n=1,438 McNemar p=5.6e-187"},
        },
    }


# ---------------------------------------------------------------------------
# power table — generalize embedding/power.py to marginal accuracies.
# ---------------------------------------------------------------------------

def discordant_from_marginals(p_hi, p_lo):
    """Independence model (conservative for correlated classifiers): returns
    (psi, pi) = (discordant rate, P[discordant favors winner]). Under
    independence c-b == p_hi-p_lo exactly, so this reproduces the marginal gap."""
    c = p_hi * (1 - p_lo)                # domestic correct, baseline wrong
    b = (1 - p_hi) * p_lo                # domestic wrong, baseline correct
    psi = c + b
    pi = c / psi if psi else 0.5
    return psi, pi


def target_n_for_power(p_hi, p_lo):
    psi, pi = discordant_from_marginals(p_hi, p_lo)
    if abs(pi - 0.5) < 1e-9 or psi <= 0:
        return float("inf"), psi, pi
    n_disc = pw.n_discordant_for_power(pi)
    return n_disc / psi, psi, pi


def mde_gap_at_n(n_total, p_lo):
    """Smallest domestic accuracy gap (pp) detectable at 80% power given n_total
    items and baseline accuracy p_lo (independence model)."""
    lo, hi = 0.0, 1 - p_lo
    for _ in range(200):
        mid = (lo + hi) / 2
        tgt, _, _ = target_n_for_power(min(0.999999, p_lo + mid), p_lo)
        if tgt > n_total:
            lo = mid
        else:
            hi = mid
    return hi


def power_table():
    """Returns list of dict rows for the report. Effect sizes from §1b."""
    rows = []

    # T3 assembled (production query_structured path): Solar 98.8 vs gpt 97.2
    tgt, psi, pi = target_n_for_power(0.988, 0.972)
    rows.append({"task": "t3 structured (ASSEMBLED, d8)", "assets": "d8", "n": 1438,
                 "effect": "Solar 98.8 vs gpt 97.2 (+1.6pp)", "target_n": round(tgt),
                 "psi": round(psi, 4), "pi": round(pi, 3),
                 "powered": tgt <= 1438,
                 "note": "§1b D10 LOCK (Solar+glue vs 현행 52.4) already p=5.6e-187 @ n=1438"})

    # T3 bare-model axis (reference): Solar 73.5 vs gpt 52.4
    tgt, psi, pi = target_n_for_power(0.735, 0.524)
    rows.append({"task": "t3 structured (BARE, ref)", "assets": "holdout/d8", "n": 41,
                 "effect": "Solar 73.5 vs gpt 52.4 (+21.1pp); holdout Solar 82.9%",
                 "target_n": round(tgt), "psi": round(psi, 4), "pi": round(pi, 3),
                 "powered": tgt <= 41,
                 "note": "holdout n=41 < target; but scored path is assembled; d8 covers bare too"})

    # T3 v2 anchor
    rows.append({"task": "t3 structured (v2 anchor)", "assets": "v2", "n": 4,
                 "effect": "real-log spec anchor", "target_n": "n/a",
                 "psi": "n/a", "pi": "n/a", "powered": False,
                 "note": "spot-check anchor only, not powered"})

    # decompose — CITED from §1b (t7_holdout is Tier A, NOT wrapped in tier_b)
    rows.append({"task": "decompose (CITED §1b, not wrapped)", "assets": "H3 fresh", "n": 120,
                 "effect": "Solar-fs 90.8 vs baseline (n=160: 91.2 vs gpt 92.5 = 1.3pp, p=0.80)",
                 "target_n": "huge (tie)", "psi": "n/a", "pi": "n/a", "powered": False,
                 "note": "domestic-vs-baseline is a TIE/non-inferiority claim, not superiority; "
                         "few-shot net gain Solar-fs vs -zs +5.8pp p=0.039 sig (H3)"})

    # T5 routing holdout+v2 (route exact). §1b: domestic pairwise n.s.; use MDE.
    mde = mde_gap_at_n(48, 0.85)
    rows.append({"task": "t5 routing (holdout+v2)", "assets": "holdout+v2", "n": 48,
                 "effect": "domestic pairwise non-sig (§1b); no measured gap",
                 "target_n": "MDE", "psi": "n/a", "pi": "n/a", "powered": False,
                 "note": f"n=48 detects only gaps >= ~{mde*100:.0f}pp @ 80% power (base~85%) -> "
                         "underpowered for small routing effects (expected null)"})

    # routing_tn — FP genuine-zero guard (filed under t5)
    rows.append({"task": "t5 routing_tn (FP guard)", "assets": "routing_tn", "n": 40,
                 "effect": "genuine-zero FP-rate ceiling (safety), target 0 false non-zero",
                 "target_n": "rule-of-3", "psi": "n/a", "pi": "n/a", "powered": False,
                 "note": "0 FP in 40 -> 95% UB ~= 3/40 = 7.5%; aggregate FP-rate, not McNemar"})

    # T6 intent holdout
    mde = mde_gap_at_n(20, 0.85)
    rows.append({"task": "t6 intent (holdout)", "assets": "holdout", "n": 20,
                 "effect": "domestic pairwise non-sig (§1b); no measured gap",
                 "target_n": "MDE", "psi": "n/a", "pi": "n/a", "powered": False,
                 "note": f"n=20 detects only gaps >= ~{mde*100:.0f}pp @ 80% power -> underpowered"})

    # T2 wiki_qa holdout (pairwise win-rate)
    rows.append({"task": "t2 wiki_qa (holdout)", "assets": "holdout", "n": 30,
                 "effect": "grounded pairwise win-rate (judge)", "target_n": "n/a",
                 "psi": "n/a", "pi": "n/a", "powered": False,
                 "note": "decisive pairs = wins+losses (ties drop n); underpowered unless large "
                         "win gap; Tier-B policy raises human spot-check weight (plan §5)"})

    # T10 delegation FP holdout
    rows.append({"task": "t10 delegation (holdout2)", "assets": "holdout2", "n": 32,
                 "effect": "FP rate (safety-critical), target 0", "target_n": "rule-of-3",
                 "psi": "n/a", "pi": "n/a", "powered": False,
                 "note": "24 negative FP probes; 0 FP -> 95% UB ~= 3/24 = 12.5%; safety metric, "
                         "not accuracy McNemar"})

    # T8 synthesize — known n=8 qualitative-only
    rows.append({"task": "t8 synthesize (known)", "assets": "—", "n": 8,
                 "effect": "pairwise judge", "target_n": "n/a", "psi": "n/a", "pi": "n/a",
                 "powered": False, "note": "n=8 qualitative-only (already flagged, plan §2)"})

    return rows


# ---------------------------------------------------------------------------
# contamination / hygiene
# ---------------------------------------------------------------------------

_FEWSHOT = [
    "환불 정책은 어떻게 되고 배송 정책은 어때?",
    "온보딩 절차랑 리텐션 정책이랑 KPI 정의를 정리해줘",
    "아크메 환불 정책이 어떻게 돼?",
    "Nova와 아크메는 무슨 관계야?",
    "환불 정책은 어떻게 돼?", "배송 정책은 어때?",
    "온보딩 절차를 정리해줘.", "리텐션 정책을 정리해줘.", "KPI 정의를 정리해줘.",
]


def _q_of(rec):
    req = rec.get("input", {}).get("request", {})
    return req.get("question") or req.get("text")


def contamination_report(tier_b_records):
    tb_q = {_q_of(r): r["id"] for r in tier_b_records if _q_of(r)}
    ta = _read_jsonl(TIER_A)
    ta_q = {}
    for r in ta:
        q = _q_of(r)
        if q:
            ta_q.setdefault(q, []).append(r["id"])

    overlap_ta = {q: (tb_q[q], ta_q[q]) for q in tb_q if q in ta_q}
    fewshot_hit = [q for q in tb_q if q in set(_FEWSHOT)]

    # within-tier_b exact input dups (across tasks) — expect intentional v2 real-log reuse
    seen = {}
    dups = {}
    for r in tier_b_records:
        q = _q_of(r)
        if q is None:
            continue
        if q in seen:
            dups.setdefault(q, [seen[q]]).append(r["id"])
        else:
            seen[q] = r["id"]

    return {"tier_a_overlap": overlap_ta, "fewshot_hit": fewshot_hit, "within_tier_b_dups": dups}


# ---------------------------------------------------------------------------
# validate + report
# ---------------------------------------------------------------------------

def validate(records):
    kind_scoring = {"exact": "deterministic", "metric": "deterministic",
                    "structural": "deterministic", "judge": "judge"}
    ids = set()
    for r in records:
        base = set(r.keys()) - {"invariants"}
        assert base == REQUIRED_KEYS, f"{r['id']}: key mismatch {base ^ REQUIRED_KEYS}"
        assert r["id"] not in ids, f"dup id {r['id']}"
        ids.add(r["id"])
        assert r["id"].startswith("B-"), f"{r['id']}: bad prefix"
        assert r["tier"] == "B" and r["provenance"] == PROV
        assert r["scoring"] == kind_scoring[r["expected"]["kind"]], f"{r['id']}: scoring/kind mismatch"
        assert input_sha256(r["input"]) == r["frozen"]["input_sha256"], f"{r['id']}: hash self-check"
        json.loads(json.dumps(r, ensure_ascii=False))
    # disjoint from L2 B-g*
    l2_ids = set()
    for p in L2_DIR.glob("g*.jsonl"):
        l2_ids |= {r["id"] for r in _read_jsonl(p)}
    collide = ids & l2_ids
    assert not collide, f"id collision with L2: {collide}"
    return ids


def main():
    records = assemble()
    with OUT.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"[build_tier_b] wrote {len(records)} lines -> {OUT}")

    ids = validate(records)
    per_task = {}
    for r in records:
        per_task[r["task"]] = per_task.get(r["task"], 0) + 1
    print(f"[build_tier_b] total {len(records)}, unique ids {len(ids)==len(records)}, per-task:")
    for t in ("t2", "t3", "t5", "t6", "t10"):
        print(f"    {t}: {per_task.get(t, 0)}")

    lock = build_freeze_lock(records)
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[freeze.lock] tier_a: count={lock['tier_a']['count']} pending={lock['tier_a']['pending_excluded']} "
          f"sha256={lock['tier_a']['manifest_sha256']}")
    print(f"[freeze.lock] tier_b: count={lock['tier_b']['count']} pending={lock['tier_b']['pending_excluded']} "
          f"sha256={lock['tier_b']['manifest_sha256']}")

    print("\n[contamination]")
    c = contamination_report(records)
    print(f"  tier_a<->tier_b question overlap: {len(c['tier_a_overlap'])} "
          f"{'CLEAN' if not c['tier_a_overlap'] else c['tier_a_overlap']}")
    print(f"  decompose few-shot exemplar hits: {len(c['fewshot_hit'])} "
          f"{'CLEAN' if not c['fewshot_hit'] else c['fewshot_hit']}")
    print(f"  within-tier_b exact-input dups (intentional v2 reuse expected): {len(c['within_tier_b_dups'])}")
    for q, hitids in c["within_tier_b_dups"].items():
        print(f"    {hitids}  <- {q[:50]}")

    print("\n[power table]  (McNemar target n vs available n; effect sizes from §1b)")
    print(f"  {'task':34} {'n':>5} {'target_n':>10} {'powered':>8}  effect")
    for row in power_table():
        print(f"  {row['task']:34} {row['n']:>5} {str(row['target_n']):>10} "
              f"{str(row['powered']):>8}  {row['effect']}")
        print(f"  {'':34} {'':>5} {'':>10} {'':>8}  ↳ {row['note']}")


if __name__ == "__main__":
    main()
