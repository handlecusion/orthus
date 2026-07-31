# Fugu-KO E2E Benchmark Manifest Schema

> Scope: `experiments/fugu-ko/` only. This document specifies the per-item
> record schema for the unified benchmark manifest used by the 2단
> (오케스트레이션 이득 측정) evaluation harness. It does not touch production
> orthus code, tables, or flags (see `docs/fugu-ko-orchestration-plan.md` §6.3
> — experiment isolation is a hard constraint).
>
> Files:
> - `experiments/fugu-ko/e2e/tier_a.jsonl` — Tier A (regression, CI-facing).
> - `experiments/fugu-ko/e2e/tier_b.jsonl` — Tier B (independent holdout,
>   competition evidence).
> - `experiments/fugu-ko/e2e/freeze.lock.json` — companion drift-detection
>   lock file, one entry per tier (§8).
> - `experiments/fugu-ko/e2e/fixtures/` — checked-in DB-state fixtures
>   referenced by L2 items that need pre-existing rows (§5.2).
>
> Both manifests are **JSON Lines**: exactly one JSON object per line, UTF-8,
> no trailing commas, no comments. A line is one *item*.

---

## 1. Base record shape

Every item is an object with exactly these top-level keys (no additional
keys; unknown keys are a manifest-lint failure):

```json
{
  "id": "<string>",
  "layer": "L1 | L2",
  "task": "<string>",
  "entry_point": "<string>",
  "input": { "...": "..." },
  "expected": { "kind": "exact | metric | structural | judge", "...": "..." },
  "scoring": "deterministic | judge",
  "tier": "A | B",
  "provenance": "golden | independent_holdout | new_blind | reused_concluded",
  "tags": ["<string>", "..."],
  "frozen": { "input_sha256": "<hex64>", "frozen_at": "<string>" },
  "invariants": ["<string>", "..."]
}
```

`invariants` is the only optional key (§7). Every other key is required on
every item, in both tiers.

---

## 2. `id`

**Format:** `<TIER>-<task>-<NNNN>`

- `<TIER>` — uppercase, matches the item's own `tier` field (`A` or `B`).
- `<task>` — the item's own `task` field value verbatim (lowercase task
  code, §4).
- `<NNNN>` — 4-digit, zero-padded, base-10 sequence number, **scoped per
  `(tier, task)` pair**, starting at `0001`. Tier A and Tier B keep
  independent counters even for the same task code (`A-t3-0007` and
  `B-t3-0007` are unrelated items, not a pair).

Examples: `A-t3-0007`, `B-g1-0003`.

**Stability contract:** once an `id` is assigned and the manifest is
committed, it **never changes and is never reused** — not even if the item
is edited, re-tagged, or its `provenance` is reclassified. `id` is the join
key between a manifest line and every downstream score row, judge
transcript, and run-log record; a re-run must be able to match today's
`A-t3-0007` result to last week's `A-t3-0007` result to compute a delta. If
an item's `input` changes in a way that alters what is being measured,
retire the old `id` (move it to a `retired/` note, do not delete the line
outright until the tier is re-cut) and mint a new `id` with the next
sequence number instead of mutating the existing one in place. Deleting or
renumbering an `id` invalidates every historical run that referenced it —
treat the sequence as append-only.

---

## 3. `layer`

| Value | Meaning | What it measures |
|---|---|---|
| `L1` | Model-slot decision | A single production function call that consumes `get_chat_model()` (or a task-specific slot from `docs/model-orchestration.md`). Tests one LLM call's output in isolation. |
| `L2` | Product E2E flow | A full request through a production API route that may internally chain several L1 decisions, deterministic gates, and persistence. Tests the *composed* outcome (policy_outcome, wiki_task creation, gate reason codes), not any single model call. |

L1 items answer "did this model slot make the right call, in isolation?".
L2 items answer "did the end-to-end product behavior come out right, given
whatever model slots and gates fired along the way?". A regression can pass
every L1 item for a task and still fail the L2 flow that composes it (wrong
gate wiring), and vice versa (L1 wrong, but a downstream deterministic gate
fail-closes to the same correct L2 outcome) — both layers are kept because
neither subsumes the other.

---

## 4. `task` — registry

`task` is always one short lowercase code from the fixed registry below.
Codes are stable identifiers, not free text — do not invent new codes
without adding a row here first. Legacy `experiments/fugu-ko/golden/tN*.json`
files are the primary conversion source for L1 items where a code lines up
(noted below); items minted directly into the e2e manifest do not have to
originate from those files.

### 4.1 L1 codes (model-slot decision)

| code | canonical L1 task name | production `entry_point` | legacy golden source (if any) |
|---|---|---|---|
| `t2` | `wiki_qa` | `orthus/wiki/qa.py::ask` | `golden/t2.json`, `golden/t2_holdout.json` |
| `t3` | `structured` | `orthus/structured/query.py::query_structured` | `golden/t3.json` + `t3_*_holdout.json` variants |
| `t5` | `routing` | `orthus/router/route.py::classify` | `golden/t5.json`, `golden/t5_holdout.json` |
| `t6` | `intent` | `orthus/router/route.py::classify_intent` | `golden/t6.json`, `golden/t6_holdout.json` |
| `t7` | `decompose` | `orthus/router/decompose.py::should_decompose` | `golden/t7.json`, `golden/t7_holdout.json` |
| `t8` | `synthesize` | `orthus/router/decompose.py::synthesize` | `golden/t8.json` |
| `t9` | `graph_bind` | `orthus/router/graph.py::bind_graph_params` | `golden/t9_graph_bind.json` |
| `t10` | `delegation_extract` | `orthus/agentwork/delegation.py::extract_delegation_intent` | `golden/t10_delegation.json`, `t10_holdout2.json` |
| `t11` | `distill` | `orthus/wiki/distill.py::distill_document` | (sampled corpus docs, not a `golden/*.json`) |
| `t12` | `email_draft` | `orthus/mail/compose.py::draft_email` | subset of `t12_generation.py` runs |
| `t15` | `gap_suggest` | `orthus/wiki/gap.py::generate_suggestion` | subset of `t12_generation.py` runs |
| `t16` | `claim_headline` | `orthus/wiki/backfill_claim_headline.py::backfill_claim_headlines` | subset of `t12_generation.py` runs |

`t1`, `t4`, `t13`, `t14` are intentionally absent from this registry: `t1`
was never assigned in the legacy experiment scripts; `t4` is the decompose
*prefilter signal* sweep (`docs/decompose-prefilter-ext.md`), a harness
parameter study rather than a model-slot task; `t13`/`t14` are distill
claim-cap variants of `t11`, not distinct tasks. `email_draft` /
`gap_suggest` / `claim_headline` were historically measured together under
one `t12_generation.py` harness run but get **three separate codes** here
because each is a distinct production `entry_point` and each needs its own
`id` sequence — collapsing them under one code would make `id` ambiguous
(two different tasks both minting `A-t12-0001`).

### 4.2 L2 codes (product E2E flow)

| code | flow | production `entry_point` |
|---|---|---|
| `g1` | ingest → wiki | `POST /documents/{doc_id}/publish` (`orthus/api/routes/documents.py::publish_document`) — explicit publish is the only path that triggers corpus indexing + LLM wiki authoring (AGENTS.md P3.4b); draft save alone does not qualify as this flow. |
| `g2` | agent-work chat orchestrator | `POST /agent-work/chats/{id}/orchestrate` (`orthus/api/routes/agent_work.py::orchestrate_chat_route`) |
| `g3` | mail ingest → candidate | `POST /mail/ingest` (`orthus/api/routes/mail.py::post_mail_ingest`) — covers inbound-mail-triggered reply-draft / delegation-candidate creation (P7.1, agent-task-delegation slice 4). |
| `g4` | delegation gate | `POST /agent-work/delegate` (`orthus/api/routes/agent_work.py::delegate_agent_task`), which invokes the deterministic policy gate `orthus/agentwork/service.py::decide_agent_work_item` internally. |

---

## 5. `entry_point`

**Rule:** `entry_point` must name a **live production callable that the
manifest item actually exercises when scored** — never a test shim, a mock,
a copy-pasted reimplementation, or a harness-only wrapper. If the harness
needs to inject a fixed worker model or a frozen actor, it does so via
parameters/fixtures *around* the real call (`chat_model=` injection,
request headers, a scratch DB), not by substituting a different function.
This is what makes T3's `sqlglot` gate, T5/T6/T7's decision boundaries, and
every L2 policy-gate outcome directly traceable to `git blame` on the named
file/line — a manifest item that scores against a reimplementation is
measuring the reimplementation, not orthus.

### 5.1 L1 form

`module/path.py::function_name`, using the repo-root-relative module path
(matches `orthus/...` throughout, per §4.1). Private/underscore-prefixed
helpers are not valid `entry_point` targets — pick the public function the
production code path actually calls (e.g. `router/route.py::classify`, not
`router/route.py::_rule_based_route`).

### 5.2 L2 form

`METHOD /path/template` (path parameters kept as `{name}` placeholders, not
resolved values — resolution happens per-item in `input`), e.g.
`POST /agent-work/chats/{id}/orchestrate`. Internal functions the route
chains through (a policy gate, a second-verify pass) may be *named in the
registry description* (§4.2) for traceability but are not themselves valid
`entry_point` values for an L2 item — the item scores the route's observable
response/state change, which is what a real client sees.

---

## 6. `input`

**Contract:** `input` is the exact, complete payload the harness passes to
`entry_point` — nothing implicit, nothing pulled from a mutable live DB at
score time. This is what "frozen" (§9) hashes, and what makes a Tier A/B
result reproducible months later against a different commit.

### 6.1 L1 shape

```json
"input": { "request": { "<kwarg_name>": "<value>", "...": "..." } }
```

`request` keys are exactly the named, non-injected parameters of
`entry_point`'s signature (e.g. for `classify(question, *, chat_model=None)`
→ `{"question": "..."}`). Parameters the harness itself supplies per sweep
(`chat_model`, `user_id`, `settings`) are never part of `input` — they vary
across the same item by design (that's the whole point of paired execution
across workers, per `docs/fugu-ko-orchestration-plan.md` §4.4) and belong to
run configuration, not the frozen item.

### 6.2 L2 shape

```json
"input": {
  "request": { "method": "POST", "path": "/agent-work/delegate", "body": { "...": "..." }, "query": {} },
  "fixture": { "id": "<fixture-name>", "path": "e2e/fixtures/<file>.json", "sha256": "<hex64>" }
}
```

- `request` is the logical HTTP call: method, path (with any `{param}`
  placeholders already resolved to concrete values for this item), body,
  and query params. **Auth material (bearer tokens, HMAC signatures,
  session cookies) is never embedded here** — the harness attaches its own
  test-actor credentials at dispatch time, per the repo's no-plaintext-secret
  rule (AGENTS.md 절대 규칙). `body`/`query` may still name a logical actor
  (e.g. `assignee`, `runner`) where the route itself takes one as a field.
- `fixture` is **required whenever `entry_point` reads pre-existing state**
  that the request body alone does not create (an existing chat session
  with prior messages for `g2`, an existing `agent_work_item`/wiki page for
  a decision-flow item, a document already indexed for a re-publish case).
  It points at a checked-in, version-controlled seed file under
  `e2e/fixtures/` (SQL insert script or JSON seed consumed by a documented
  loader in `experiments/fugu-ko/e2e/`) that the harness applies to a
  **scratch/test DB** immediately before dispatching the request — never
  against the shared dev/prod DB's live, mutable rows. `fixture.sha256` is
  the sha256 of the fixture file's bytes, checked by the same drift gate as
  `frozen.input_sha256` (§9).
- `fixture` is **omitted** when the entry point's own request body is
  sufficient to reconstruct all needed state (e.g. `g3` mail-ingest: the
  raw inbound-mail payload is itself the only state the flow needs — the
  route creates its own rows from scratch).

---

## 7. `expected`

`expected.kind` has exactly four values. `scoring` (§8) is intentionally a
coarser, redundant field derived from `kind` so CI/tooling can filter by
scoring cost without inspecting nested structure — a manifest linter must
reject any item where the mapping below doesn't hold.

| `expected.kind` | `scoring` it implies | used for |
|---|---|---|
| `exact` | `deterministic` | closed-set outputs: route/intent enums, boolean gates, small structured objects |
| `metric` | `deterministic` | set-overlap or rate-style correctness against a reference set/threshold |
| `structural` | `deterministic` | L2 field/shape assertions against the route's response or resulting row state |
| `judge` | `judge` | open-ended generation quality (wiki answers, drafts, summaries) |

### 7.1 `exact`

```json
{ "kind": "exact", "value": <route | intent | boolean | small object> }
```

`value`'s shape is whatever the production function/route returns for that
task — document it per task, not per item:

- `t5` (`routing`): `value` ∈ `"structured" | "wiki" | "graph"`.
- `t6` (`intent`): `value` is one of the enum members returned by
  `classify_intent` (`router/route.py`).
- `t7` (`decompose`, gate-boolean items): `value` ∈ `true | false`.
- `t9` (`graph_bind`): `value` is `{"intent": "<relation|conflict|provenance|entity>", "subjects": ["<noun phrase>", "..."]}` — `intent` scored as exact match (primary metric, per existing `golden/t9_graph_bind.json` convention), `subjects` scored as set equality (secondary metric).
- `t10` (`delegation_extract`, positive items): `value` is the expected
  extracted structured object (assignee/instruction fields).
  **Negative-control items** (text that must *not* produce a delegation
  candidate — the false-positive probes referenced in AGENTS.md's "적대적
  홀드아웃…함정 24개" delegation note) use `value: null`, meaning "no
  candidate object should be produced."
- `t3` (`structured`): `value` is `{"gate_pass": true, "result": <expected row set, order-insensitive unless the query has ORDER BY>}` — this packages both the deterministic sqlglot-gate outcome and the executed result, mirroring the existing structured/query.py regression convention (`tests/unit/test_validate.py`).

### 7.2 `metric`

```json
{ "kind": "metric", "reference": [ "<expected element>", "..." ], "recall_min": 0.8, "precision_min": null, "fp_rate_max": null }
```

Used when a single item's output is itself a *set* (a decompose leaf split,
an entity/subject extraction) and correctness is "did you cover the
expected elements without inventing too many extra ones," not exact string
match. `reference` is the expected set for **this one item**; `recall_min`/
`precision_min`/`fp_rate_max` are optional thresholds (omit ones that don't
apply) that this item's own computed recall/precision/FP-rate against
`reference` must clear to pass. All thresholds present must be cleared —
a partial pass is still a fail.

**Aggregate ceilings are not per-item.** Numbers like "missed ≥ 80% recall"
or "mis-split ≤ 5%" for the E3 prefilter sweep, or a delegation false-positive
*rate* ceiling across a whole probe set, are properties of a **tagged
subset of items across a run**, not of one line. Encode the individual
items normally (each with its own `exact` or `metric` expected value — e.g.
each E3 negative-control item still just says "this input must not
decompose": `{"kind":"exact","value":false}`), tag them consistently
(`tags: ["e3", "missed_probe"]` / `["delegation_fp_probe"]`), and let the
scoring harness compute the aggregate rate over all items sharing that tag,
compared against the ceiling recorded in the harness's run config (outside
this per-item schema). This keeps the manifest itself model-agnostic and
threshold-free at the line level.

### 7.3 `structural` (L2 only)

```json
{
  "kind": "structural",
  "assert": {
    "mode": "agent_work",
    "policy_outcome": "draft_for_review",
    "reason_codes_contains": ["policy_bucket_email_reply"],
    "wiki_task_kind": "open_question"
  }
}
```

`assert` is a flat object. Each key is either a field name on the route's
response object / resulting row (or a dotted path for nested access, e.g.
`payload.retry_guard.auto_retry_allowed`), or that same path suffixed
`_contains`/`_contains_any` when the actual field is a list and only
membership (not full-list equality) is required. A `structural` item passes
only if **every** key in `assert` matches. This is the L2-preferred
encoding — per point 6, L2 items use `structural` wherever the outcome is a
closed enum/reason-code/typed-payload check (which is most of P3's
`auto_execute | draft_for_review | request_more_data | reject` gate space);
`judge` is reserved for the minority of L2 outcomes where the correctness
criterion is genuinely open-ended text quality (e.g. judging the generated
reply-draft body inside a `g3` item, not the gate outcome around it).

### 7.4 `judge`

```json
{ "kind": "judge", "rubric": "evidence-pairwise-winrate", "reference": null }
```

- `rubric` names a named rubric implemented in `experiments/fugu-ko/judge/`
  (e.g. the existing pairwise-comparison + position-shuffle + self-preference
  defense described in `docs/fugu-ko-orchestration-plan.md` §4.2). New
  rubric names must be added there before use here.
- `reference` is optional: `null` when the rubric is a pure pairwise
  comparison between worker outputs (T2's design — no fixed "correct
  answer" to anchor against), or a fixed reference string/object when the
  rubric scores a single output against a known-good baseline (e.g. an
  absolute 1–5 rubric run, kept as trend-only per §4.3).
- Used for: `t2` (`wiki_qa`), `t8` (`synthesize`), `t12`/`t15`/`t16`
  (generation trio), and `t11` (`distill`) qualitative claim-quality checks.
  `t11` items measuring hallucination/contamination *rate* instead use
  `metric` (`fp_rate_max` against a human-labeled contamination reference),
  not `judge` — the presence/absence of a fabricated claim is closer to a
  labeled classification than an open rubric.

---

## 8. `scoring`

`"deterministic" | "judge"` — see the mapping table in §7. This field
exists so a CI-facing subset (Tier A, `scoring == "deterministic"` only) can
run cheaply and fast on every push, while `judge`-scored items (which cost
LLM-judge tokens and run slower) are reserved for scheduled/manual Tier A
runs and full Tier B evidence passes. A manifest-lint step must fail the
build if any item's `scoring` disagrees with what its `expected.kind`
implies.

---

## 9. `tier`

| Value | Role |
|---|---|
| `A` | Regression set. Deterministic-first (heavier on `exact`/`metric`/`structural`, lighter on `judge`). Runs in CI on every relevant push. Existing/known items; safe to re-run cheaply and often. |
| `B` | Independent holdout. Competition evidence — the numbers that go in the 8월 제출 보고서. Never reused as build/training signal (mirrors the plan's "2단부터 신규 문항으로 test split 분리" rule, §4.1 of the orchestration plan). |

An item's `tier` never changes after it is committed. If a Tier B item's
role changes (its evidence value is spent — it has been published or
consumed as a build signal), it is re-tagged `provenance: reused_concluded`
in place; it does not move to Tier A.

---

## 10. `provenance`

| Value | Meaning |
|---|---|
| `golden` | Carried over from an existing `experiments/fugu-ko/golden/*.json` set (or the plan's T3/T5 regression reuse), converted into this schema without altering its judged content. |
| `independent_holdout` | Newly authored specifically to be unseen by whatever selected the current rule/model configuration — the standard "measure without the hand that picked the rule" set (this is the discipline the plan's §11.3b DF-series re-measurement enforced after the n=16 golden-set optimism bias was caught). |
| `new_blind` | Freshly authored, not derived from any prior golden/holdout set, author had no visibility into current per-task scores when writing it (strongest bias defense — used for adversarial/negative-control authoring, e.g. delegation false-positive traps). |
| `reused_concluded` | Was `independent_holdout` or `new_blind` in an earlier measurement round; that round has concluded and been reported, so the item is now safe to reuse as a regression check (Tier A) without re-litigating its original evidence claim. Must retain its original `id`. |

---

## 11. `tags`

Freeform array of short lowercase strings, no fixed vocabulary — used for
filtering (`--tags e3`), reporting slices, and aggregate-metric grouping
(§7.2). Recommended patterns, not enforced:

- Task-flavor tags mirroring the legacy golden sets: `"emoji-db"`,
  `"confident-zero"`, `"conflict"`.
- Flow-role tags for L2: `"g1"`, `"g1-conflict"` (redundant with `task` but
  useful when slicing across tiers or joining with other flows' conflict
  cases).
- Aggregate-metric group tags (§7.2): `"e3"`, `"missed_probe"`,
  `"delegation_fp_probe"`.
- Provenance-adjacent context tags where useful: `"adversarial"`,
  `"negative_control"`.

---

## 12. `frozen`

```json
{ "frozen": { "input_sha256": "<hex64>", "frozen_at": "<string>" } }
```

- **`input_sha256`**: sha256 hex digest (lowercase, 64 chars) computed over
  the item's `input` field, canonicalized as: JSON-serialize with sorted
  object keys, no extra whitespace, and Unicode characters kept literal
  (not `\uXXXX`-escaped) — Python equivalent:
  `hashlib.sha256(json.dumps(item["input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()`.
  Any generator/validator for this manifest must use exactly this
  canonicalization — a different whitespace or escaping choice produces a
  different hash for byte-identical content, which would spuriously trip
  the drift gate (§13).
- **`frozen_at`**: either an ISO-8601 UTC timestamp with `Z` suffix (e.g.
  `"2026-07-20T09:15:00Z"`, set when the item was hand-authored/edited), or
  a build-tag string `"build:<7-hex-git-sha>"` (e.g. `"build:8f3a1c2"`) when
  the item was minted in bulk by a manifest-build script, naming the commit
  that produced it. Both forms are opaque audit trail only — never used in
  scoring.

**Why freeze at all:** `input` must not silently drift between the moment
an item's `expected` was authored/judged-calibrated and the moment it is
scored months later. `input_sha256` lets the harness detect at load time
that an item's `input` was edited without a corresponding `id` retirement
(§2) — a hash mismatch against the checked-in value is a hard load-time
error, not a warning.

---

## 13. `freeze.lock` / 🚦 drift gate

`experiments/fugu-ko/e2e/freeze.lock.json` aggregates every item's
`frozen.input_sha256` into one manifest-level hash per tier:

```json
{
  "tier_a": { "count": 143, "manifest_sha256": "<hex64>", "generated_at": "2026-07-20T09:20:00Z" },
  "tier_b": { "count": 58,  "manifest_sha256": "<hex64>", "generated_at": "2026-07-20T09:20:00Z" }
}
```

`manifest_sha256` = sha256 over the UTF-8 string formed by joining
`"<id>:<input_sha256>"` for every item in that tier, **sorted by `id`**,
newline-joined. This is the single number CI checks — the 🚦 gate
(mirroring the `docs/fugu-ko-orchestration-plan.md` §10 gate-table
convention: no reproducible command, no gate pass): regenerate
`freeze.lock.json` from the current `tier_a.jsonl`/`tier_b.jsonl` and diff
against the checked-in file. Any difference — a changed `input`, an added
or removed item, a count mismatch — fails the gate and blocks merge until
either the manifest change is intentional (re-run the lock generator and
commit the new lock alongside the manifest diff, in the same PR, with the
diff visible in review) or reverted. This is what prevents an item's
`input` from quietly drifting out from under an already-recorded
`expected` judgment.

---

## 14. `invariants` (optional)

```json
{ "invariants": ["model_fallback_zero", "no_confident_zero"] }
```

An open, freeform string array — not a closed enum — but two canonical
values exist today and new ones should follow the same shape (a single
run-level guarantee this item's execution must not violate, independent of
whether its own `expected` scored correct):

- `"model_fallback_zero"` — this item's execution must not have silently
  fallen back to `get_chat_model()`'s default slot instead of the worker
  the harness explicitly assigned/injected for this sweep. An item can
  score a correct `expected.value` while its underlying call quietly used
  the wrong model (e.g. an API key/timeout failure triggered the
  `docs/model-orchestration.md` fallback ladder) — that run is contaminated
  for per-worker comparison purposes even though the number that landed in
  `expected` looks fine, so the invariant is checked and enforced
  separately from — and in addition to — the item's own scoring.
- `"no_confident_zero"` — the classifier/route/intent output must not be a
  confidently-wrong, degenerate answer (e.g. an empty/zero-length rationale
  paired with a high-confidence label) — a known regression class for L1
  routing-family tasks.

`invariants` is checked by the scoring harness as an **additional pass/fail
gate layered on top of** `expected` scoring: an item with a satisfied
`expected` but a violated invariant still fails the run for that item.
Items without an `invariants` key are scored on `expected` alone.

---

## 15. GPT-4o-mini rows are baseline-only

Manifest items themselves are **model-agnostic** — no item names a model;
the same `id` is run against every worker in the pool per the plan's paired
execution rule (§4.4 of the orchestration plan). When the harness produces
per-model result rows against a manifest item, any result row where the
executing model is `gpt-4o-mini` (or any other GPT-family model) is a
**reference/comparison point only** — it is never eligible to be written
back as a production-slot assignment, never counted toward Tier B
competition-evidence "which model should `ORTHUS_LLM` be" conclusions, and
never used as the `reference` value inside an `expected.kind: "judge"` item
(a GPT-scored reference would reintroduce exactly the vendor dependency
`docs/model-orchestration.md` §11–15 documents as banned, owner-confirmed
2026-07-14). It exists in run output purely as the historical/frontier
comparison baseline referenced throughout that document
(`대조군: 현행 프로덕션 모델`, plan §3.1). Manifest tooling that consumes
per-model result rows to pick a winner must hard-exclude any row whose
model id matches `gpt-*` before computing eligibility, regardless of how
well that row scored.

---

## 16. Example records

Two `L1` and two `L2` example lines (each is a single, valid JSON object —
shown pretty-printed here for readability; the actual `.jsonl` file keeps
each on one line). `input_sha256` values below are real sha256 digests of
the shown `input` object under the §12 canonicalization rule.

**L1 — exact (`t5` routing):**

```json
{"id":"A-t5-0012","layer":"L1","task":"t5","entry_point":"router/route.py::classify","input":{"request":{"question":"이번 달 신규 계약 목록 보여줘"}},"expected":{"kind":"exact","value":"structured"},"scoring":"deterministic","tier":"A","provenance":"golden","tags":["routing","structured-term"],"frozen":{"input_sha256":"f04d3f2f1d465b6a0e9ef5325a99b4993ffaa8e382434de148667dd1c47855a1","frozen_at":"2026-07-20T09:00:00Z"}}
```

**L1 — metric, negative-control (`t10` delegation extraction, FP probe):**

```json
{"id":"B-t10-0004","layer":"L1","task":"t10","entry_point":"orthus/agentwork/delegation.py::extract_delegation_intent","input":{"request":{"question":"어제 회의록 요약 좀 해줘"}},"expected":{"kind":"exact","value":null},"scoring":"deterministic","tier":"B","provenance":"new_blind","tags":["delegation_fp_probe","negative_control","adversarial"],"frozen":{"input_sha256":"5af351e46e8aa47d6a24dd6f27815f54cbf715701e5b6b675455eefb77255d53","frozen_at":"build:8f3a1c2"},"invariants":["model_fallback_zero"]}
```

**L2 — structural (`g2` agent-work orchestrator, conflict → review):**

```json
{"id":"A-g2-0002","layer":"L2","task":"g2","entry_point":"POST /agent-work/chats/{id}/orchestrate","input":{"request":{"method":"POST","path":"/agent-work/chats/{session_id}/orchestrate","body":{"message":"환불 정책이랑 배송 정책 중 뭐가 맞는지 판단해줘, 둘이 충돌하는듯"}},"fixture":{"id":"g2-conflict-claim-session","path":"e2e/fixtures/g2-conflict-claim-session.json","sha256":"6f1c2a9e0b7d4f3a8c5e1b9d2f6a3c7e8b0d4f1a9c6e3b7d2f8a5c1e9b4d6f3a"}},"expected":{"kind":"structural","assert":{"mode":"agent_work","policy_outcome":"draft_for_review","wiki_task_kind":"conflict"}},"scoring":"deterministic","tier":"A","provenance":"golden","tags":["g2","conflict"],"frozen":{"input_sha256":"a7ddaa546ac13f7ca40b804b285f28a4155529806bb53ca9b6f3404af75183ca","frozen_at":"2026-07-20T09:05:00Z"}}
```

**L2 — structural (`g4` delegation gate, owner actor → auto-execute):**

```json
{"id":"B-g4-0001","layer":"L2","task":"g4","entry_point":"POST /agent-work/delegate","input":{"request":{"method":"POST","path":"/agent-work/delegate","body":{"runner":"codex","mode":"knowledge","assignee":"","instruction":"이번 주 노바 로드맵 문서 정리해서 위키에 반영 후보로 올려줘"}},"fixture":{"id":"g4-owner-actor-personal-node","path":"e2e/fixtures/g4-owner-actor-personal-node.json","sha256":"0a4e7c1b8f3d6a9c2e5b0f7a4d1c8e3b6a9f2c5d0e7b4a1c8f3d6a9e2c5b0f7a"}},"expected":{"kind":"structural","assert":{"mode":"agent_task","policy_outcome":"auto_execute"}},"scoring":"deterministic","tier":"B","provenance":"independent_holdout","tags":["g4","owner-actor"],"frozen":{"input_sha256":"d4b511a88095b70905703c04a1fa3099a14777281b2c5bdfc85a1380d5108f21","frozen_at":"2026-07-20T09:10:00Z"},"invariants":["model_fallback_zero"]}
```
