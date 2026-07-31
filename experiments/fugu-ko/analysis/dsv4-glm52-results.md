# DeepSeek V4 Pro + GLM-5.2 — frontier-contender arms on the core experiments

Date: 2026-07-23. Slugs: `deepseek:deepseek-v4-pro` (api.deepseek.com, existing
`_build_deepseek_chat` slug-suffix override) and `glm:glm-5.2` (z.ai
`_GLM_BASE_URL` — verified live this session, no harness change needed). Both
are reasoning models; every runner path sends **no output-token cap** (small
budgets return empty content — the documented GLM incident trap). All runs
`--final-verify`. Not committed; no `.env` edits (keys read only).

Runner wiring added this session (uncommitted, ruff-clean):
- `m7_run.py` / `b2_run.py`: `deepseek-v4-pro` / `glm-5.2` endpoint cases
  mirroring the gpt-5.5 reasoning-model rule (no token cap; temperature=0
  accepted by both — verified via canaries; timeout 180s).
- `usage_capture_run.py` (new helper): monkeypatches the single `_post_json`
  choke point to record every response's `usage` block → exact call/token/cost
  accounting incl. `reasoning_tokens` (sidecars `analysis/raw/usage_*.jsonl`).
- `analysis/dsv4_glm52_l2_stats.py`, `analysis/dsv4_glm52_cost.py` (analysis
  helpers).

Every arm ran a canary first (3-item L2, R1+W1 M7, 3-item B2, task-slice
flow); all canaries clean (no empty-content, no wrong-model-id, no mock
routing; usage sidecars confirmed real vendor `usage` blocks with
`reasoning_tokens`). Flow-staging DB exclusivity respected: the in-flight Opus
flow arm was allowed to exit before any flow run here touched
`orthus_flowbench_staging`.

## 1. Layer-2 head-to-head (tier A, L1, t3/t5/t6/t7/t9/t10, live `orthus_company_0706`, n=145)

Same config as `analysis/b1-layer2-orchestration.md`; references are the frozen
B1 snapshots (`analysis/raw/b1/layer2/raw_*.jsonl`), composite reconstructed
from solar/exaone with slots {t3:solar, t5:exaone, t6:solar, t7:solar,
t9:solar, t10:exaone}. Raws: `analysis/raw/e2e_l2_dsv4_deepseek:deepseek-v4-pro.jsonl`,
`analysis/raw/e2e_l2_glm52_glm:glm-5.2.jsonl`; stats script
`analysis/dsv4_glm52_l2_stats.py`.

| model | t3(28) | t5(21) | t6(20) | t7(22) | t9(32) | t10(22) | TOTAL | acc | p50 | p95 |
|---|---|---|---|---|---|---|---|---|---|---|
| composite (국내 오케스트레이션) | 28/28 | 19/21 | 19/20 | 14/22 | 32/32 | 21/22 | **133/145** | 91.7% | 514ms | 831ms |
| gpt-5.3 | 28/28 | 19/21 | 19/20 | 13/22 | 32/32 | 21/22 | 132/145 | 91.0% | 1644ms | 2763ms |
| Claude Sonnet 4.6 | 28/28 | 19/21 | 19/20 | 12/22 | 32/32 | 21/22 | 131/145 | 90.3% | 1938ms | 3390ms |
| **GLM-5.2** | 28/28 | 19/21 | 19/20 | 10/22 | 32/32 | 20/22 | **128/145** | 88.3% | 5199ms | 33345ms |
| **DeepSeek V4 Pro** | 28/28 | 19/21 | 19/20 | 13/22 | 32/32 | 16/22 | **127/145** | 87.6% | 2528ms | 9032ms |

(p50/p95 over the scored set incl. deterministic 0-ms t7 probe-path rows;
LLM-dispatched-only latency in §5.)

- **⚠️ old-scorer caveat:** the old reference bench had DeepSeek V4 Pro
  114/145 and GLM-5.2 115/145 — but with the PRE-FIX t3 scorer. With the
  fixed number-set scorer both jump +13 (both t3 28/28). The old numbers are
  not comparable and were not reused anywhere in this doc's stats.
- Paired exact McNemar (n=145): **neither contender differs significantly
  from anything.** DSv4 vs composite 1↔7 discordant p=0.0703 (borderline,
  composite-favoring; bootstrap CI [−0.083, −0.007]); GLM vs composite 1↔6
  p=0.1250. vs gpt-5.3: DSv4 p=0.125, GLM p=0.125 (GLM 0↔4 — never wins a
  discordant pair against gpt-5.3, CI [−0.055, −0.007]). vs Sonnet: p=0.219 /
  p=0.250. vs baseline/solar/exaone: all p≥0.34 clean ties.
- Pattern: the composite's edge is where B1 found it — **t10** for DSv4
  (16/22, solar-pattern delegation-trap misses; DSv4 is a near solar-clone
  here: 127 vs 127, 2↔2 discordance) and **t7** for GLM (10/22, worst t7 in
  the table, while its t10 20/22 is frontier-grade).
- Run hygiene: DSv4 0 errors. GLM lost 1 item (A-t5-0010) to a z.ai 429
  during the window when the GLM B2 arm ran concurrently; a single-item re-run
  passed and was backfilled (all other rows from the main run). The stdout
  `RESULT: FAIL` banner on both runs is the known live-DB
  model-independent-items artefact — the frozen solar/sonnet reference runs
  show the identical banner and id list.

## 2. M7 agentic loop (20 tasks, deterministic fixture env)

Raws `raw/m7_deepseek-v4-pro.jsonl`, `raw/m7_glm-5.2.jsonl`; scored with the
frozen `m7_score.py`.

| model | completed | avg turns | llm calls | tool calls | fmt failures | wall p50/task | wall max |
|---|---|---|---|---|---|---|---|
| Claude Sonnet 4.6 | 20/20 | 3.35 | 67 | 70 | 0 | 11.3s | 20.7s |
| **DeepSeek V4 Pro** | **20/20** | 3.45 | 69 | 88 | 0 | 11.9s | 26.5s |
| **GLM-5.2** | **20/20** | 3.50 | 70 | 75 | 0 | 32.0s | 130.6s |
| solar | 14/20 | 3.57 | 69 | 72 | 3 | — | — |
| exaone | 14/20 | 4.21 | 95 | 80 | 0 | — | — |

Both contenders match Sonnet's perfect 20/20 (McNemar vs Sonnet: 0 discordant,
p=1.000; Sonnet's p=0.0312 advantage over solar/exaone therefore extends to
both). Native OpenAI function calling, zero format failures, all kinds passed
(read 8/8, write 8/8, recovery 4/4). The separator is wall-clock: DSv4 runs at
Sonnet speed; GLM is ~3× slower per task.

## 3. B2 contract compliance (280 items, 9-model rescore)

Rescored from scratch including the new raws →
`analysis/raw/b2_summary_9model.json` + `b2_rows_9model.jsonl`. Accuracy input
`analysis/raw/b2_accuracy_dsglm.json` keeps old-bench provenance for the
variance-ratio pairing axis (caveat embedded in the file; do not mix with §1's
fixed-scorer accuracies).

| model | strict | lenient | fence |
|---|---|---|---|
| Claude Sonnet 4.6 | 94.6% | 94.6% | 0.0% |
| solar | 94.3% | 94.3% | 0.0% |
| Claude Opus 4.5 | 93.9% | 95.4% | 1.4% |
| **GLM-5.2** | **93.9%** | 93.9% | 0.0% |
| gpt-4o-mini | 93.6% | 93.6% | 0.0% |
| **DeepSeek V4 Pro** | **92.5%** | 92.5% | 0.0% (0.4% unclass) |
| exaone | 79.3% | 79.3% | 0.0% |
| ax | 73.2% | 81.4% | 5.0% |
| Claude Haiku 4.5 | 5.4% | 11.1% | 6.1% |

Both land in the top compliance cluster; neither shows the Haiku code-fence
pathology. Weakest surfaces: GLM C5 92.5 / C6 95.0 (its C7 80.0 is actually
the best C7 in the table); DSv4 C2/C3 95.0, C7 67.5. C7 stays hard for
everyone (67.5–80.0). Adding the two models does not change the B2 main
verdict ("comparable — 주장 철회"; the contract-vs-accuracy variance-ratio CI
still spans 1).

Run hygiene: DSv4 280/280, 0 errors. GLM: 5 z.ai 429 losses mid-run
(concurrent GLM L2 arm contention; adapter retries exhausted) — the 5 items
were re-run serially and patched into the raw before scoring (final raw 280
rows, 0 errors).

## 4. Flow Bench (L2 g1–g4, `orthus_flowbench_staging`)

DeepSeek ran the full 137-item manifest. GLM ran under the documented cost
guard: a 10-item canary (10/10 PASS) projected ≈$0.27 for the full flow, and
DeepSeek's actual full-flow token profile repriced at GLM rates bounded it at
≈$0.32 — both ≪ $10, so the full GLM flow proceeded (actual captured cost
$0.10, §5).

| flow | solar | exaone | Sonnet 4.6 | gpt-5.3 | **DSv4 Pro** | **GLM-5.2** |
|---|---|---|---|---|---|---|
| g1 ingest→wiki | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| g2 chat orchestrator | 33/42 | 33/42 | 33/42 | 33/42 | 32/42 | 31/41 |
| g3 mail→reply/delegation | 18/20 | 18/20 | 19/20 | 16/20 | 18/20 | 12/12 |
| g4 delegation gate | 19/19 | 19/19 | 19/19 | 19/19 | 19/19 | 19/19 |
| **overall (scorable)** | 73/84 (86.9%) | 73/84 (86.9%) | 74/84 (88.1%) | 71/84 (84.5%) | **72/84 (85.7%)** | **65/75 (86.7%)** |

- **DeepSeek V4 Pro 72/84** — between gpt-5.3 (71) and solar/exaone (73):
  −1 on the g2 discriminating set (10/20 vs everyone's 11/20), 2
  delegation_extraction misses on g3. Guard-identity vs solar IDENTICAL
  (prebuilt guards 58/58). Hygiene: one g3 item (A-g3-0003) hit an in-route
  500 at 507s (single LLM call — reasoning tail); a 3-item re-run passed it
  at 306s and only that missing item was backfilled.
- **GLM-5.2 65/75 scorable — with a structural finding.** 9 items that every
  other model scored (1 g2 + 8 g3) came back HTTP 500 in a tight 85–97s
  latency cluster with exactly 1 LLM call each: **GLM's reasoning latency
  blows the production route's internal LLM timeout on the
  mail-reply/delegation path.** A g3 re-run slice reproduced the 500s at
  ~92s — repeatable behavior, not a transient loss, so the rows were left
  as-is (no backfill). Read strictly as flow completion, the honest
  lower-bound is **65/84 = 77.4%** (counting timeout-500s as incomplete
  flows) — the worst arm measured; on the surviving scorable set its rates
  match the pack (g2 discriminating 9/19 vs solar 11/20). Guard-identity vs
  solar: IDENTICAL.

## 5. Calls, tokens, cost, latency, incidents

Exact per-call `usage` capture incl. `reasoning_tokens`
(`usage_capture_run.py` → `analysis/raw/usage_*.jsonl`;
`analysis/dsv4_glm52_cost.py` aggregates). Prices fetched 2026-07-23 from
vendor docs: DSv4 Pro $0.435/M input (cache-miss), $0.003625/M (cache-hit),
$0.87/M output; GLM-5.2 (z.ai) $1.40/M input, $0.26/M cached, $4.40/M output.

| arm (incl. canaries/retries) | DSv4 calls | DSv4 in (cached) | DSv4 out (reason %) | DSv4 $ | GLM calls | GLM in (cached) | GLM out (reason %) | GLM $ |
|---|---|---|---|---|---|---|---|---|
| Layer-2 | 125 | 102,367 (33,664) | 37,890 (91%) | 0.063 | 132 | 117,613 (10,112) | 48,486 (89%) | 0.367 |
| M7 | 77 | 198,295 (178,432) | 17,046 (46%) | 0.024 | 79 | 189,264 (148,096) | 12,171 (40%) | 0.150 |
| B2 | 285 | 175,559 (138,880) | 122,980 (86%) | 0.124 | 280* | 189,832 (81,152) | 132,451 (79%) | 0.756* |
| Flow | 67 | 25,870 (14,848) | 72,373 (77%) | 0.068 | 45** | 16,818 (3,328) | 18,109 (89%) | 0.099** |
| **TOTAL (captured)** | **554** | 502,091 | 250,289 | **$0.28** | **536** | 513,527 | 211,217 | **$1.37** |

\* GLM B2: the 5 serial 429-retry calls ran outside the capture patch; add
≈+$0.013 at the run's per-call average.
\*\* GLM flow: the 12 timeout-500 route calls (9 main + 3 retry-slice) never
returned a response, so no `usage` was captured; if z.ai bills
generation-until-disconnect these are unaccounted — upper-bound ≈+$0.05–0.10.
Even with both corrections **GLM total ≈ $1.45–1.55**, DeepSeek **$0.28** —
nowhere near the prior $10 incident, because (a) scope was capped per the
guard and (b) both vendors' prompt caching absorbed most input (DSv4 cache-hit
input is $0.0036/M — its entire M7 run cost 2 cents).

- **Reasoning-token share dominates output**: 77–91% of both models' output
  tokens on L2/B2/flow surfaces are reasoning tokens (M7 tool-loop turns are
  the exception at ~40–46%). This is exactly the overage class behind the
  original GLM incident; the no-cap rule avoided empty-content zeros and the
  capture sidecar made the spend visible throughout.
- **Latency** (LLM-dispatched scored Layer-2 items): DSv4 p50 3.2s / p95
  11.1s; GLM p50 7.6s / **p95 37.5s**. The old-bench reference tails (V4 Pro
  p95 9.1s, GLM p95 12.0s) understate this workload — GLM's tail here is ~3×
  the old measurement, and it is the direct cause of the §4 route-timeout
  finding. M7 wall/task: DSv4 11.9s ≈ Sonnet 11.3s; GLM 32.0s (max 130.6s).
- **Quota/error incidents**, all investigated (silent-zero discipline):
  z.ai 429 ×6 scored-row losses (5 B2 + 1 L2; self-inflicted two-GLM-arm
  concurrency window; all recovered by serial re-run and patched, final raws
  complete). DeepSeek in-route 500 ×1 (g3, 507s reasoning tail; recovered on
  re-run). GLM in-route 500 ×9 (g3-heavy; reproducible ⇒ reported as a
  finding, not patched). No insufficient-quota, no auth failures, no
  empty-content events, 0 model-fallback spans across every run.

## 6. Verdict rows

- **DeepSeek V4 Pro**: with the fixed t3 scorer it is a solar-twin on
  Layer-2 (127/145; 2↔2 discordance vs solar), frontier-grade on agentic
  surfaces (M7 20/20 at Sonnet speed; flow 72/84), top-cluster contract
  compliance (92.5%), and by far the cheapest frontier arm measured ($0.28
  total, cache-dominated). Systematic weakness: t10 delegation-trap misses
  (16/22, solar-pattern).
- **GLM-5.2**: 128/145 Layer-2 (t10 20/22 frontier-grade; t7 10/22 worst in
  table), M7 20/20, B2 93.9% — but the heaviest latency tail of any model
  measured (L2 p95 37.5s; M7 max 131s), and that tail is not cosmetic: it
  **repeatably 500s the production mail-reply route** (9 flow items, 77.4%
  completion lower-bound, worst measured). Cost stayed trivial (≈$1.4–1.6
  total) under this scope's guard; the operational risk at current z.ai
  prices is latency, not $/token.
- Neither contender beats the domestic composite (133/145); both slot into
  the existing statistical tie band below it — DSv4 at the solar edge, GLM
  between baseline and exaone, with McNemar non-significant everywhere on
  n=145.
