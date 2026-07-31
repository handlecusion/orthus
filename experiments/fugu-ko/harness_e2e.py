"""Unified E2E benchmark harness — Tier A (L1) + L2 flows (g1-g4).

Runs every manifest item (`e2e/tier_a.jsonl` + `e2e/l2/g*.jsonl`) against a
worker pool, in-process against live production callables (never a reimpl):

- **L1** items call the production function directly with `chat_model=<pool model>`
  injected (harness.py pattern) — bypassing `get_chat_model_for`, so a direct
  injection can never silently fall back.
- **L2** items seed a scratch `orthus_test` DB from the item's fixture, patch
  settings, authenticate a test actor, and drive the real API route through
  `fastapi.testclient.TestClient` (g4-generator pattern). Model-independent
  regression guards run under MockChat/MockEmbedding; model-discriminating
  items swap the model.

Scoring is by `expected.kind` (exact/metric/structural/judge). Invariants
(`model.fallback == 0`, no confident-zero) are enforced as a run-level gate.
Large models (`bedrock:*`, `openai:gpt-4o`, `glm:*`, `deepseek`) refuse to run
without `--final-verify` (the hard dataset-confirmation gate, STATE.md).

B1 glue-ladder axis (`analysis/b1-prereg.md`): `--glue-level {0..4}` layers the
frozen deterministic glue (snap / repair / k=5 self-consistency / probe-0 +
null fallback) on top of the t3 path, reusing the existing implementations in
`train/judge_d8.py` + `train/repair.py` + `train/sc_pilot.py` (see
`glue_ladder.py`). Omitting the flag leaves every path here untouched.

Offline mock smoke (plumbing/regression gate, no live keys):
  ORTHUS_LLM=mock ORTHUS_EMBEDDING=mock ORTHUS_MODEL_ORCHESTRATION_ENABLED=false \\
  ORTHUS_PG_DSN=postgresql+psycopg://orthus:orthus@localhost:5433/orthus_test \\
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko \\
  python experiments/fugu-ko/harness_e2e.py --models mock --smoke
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_REPO_ROOT = HERE.parent.parent  # experiments/fugu-ko -> experiments -> repo root

# Load repo-root `.env` for EVERY env var this harness reads (domestic pool keys
# AND Phase-6 large-model keys: ORTHUS_LLM_BEDROCK_API_KEY / ORTHUS_LLM_API_KEY /
# ORTHUS_GLM_API_KEY / etc.), resolved by file path so it works regardless of the
# process CWD (LIVE_BENCH_RUNBOOK.md's documented CWD-relative-loading gotcha —
# `orthus.settings.get_settings()`'s own `env_file=".env"` is CWD-relative by
# pydantic-settings convention, unlike this explicit path). `override=False`
# (python-dotenv default) means a real shell-exported var always wins over the
# `.env` file, matching prior behavior for anyone who already exports keys.
# python-dotenv is not a new dependency here — it is already a hard transitive
# dependency of `pydantic-settings` (a top-level `orthus` dependency), so it is
# importable in this venv without touching pyproject.toml.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=False)

# The router semantic cache must be off before orthus.settings is first read, so
# L2 `/ask`-family paths never consult the cache (HARNESS_RECON §cache). Default
# is already False; we pin it explicitly so a stray env cannot flip it on.
os.environ.setdefault("ORTHUS_ASK_SEMANTIC_CACHE_ENABLED", "false")
os.environ["ORTHUS_ASK_SEMANTIC_CACHE_ENABLED"] = "false"

import sys  # noqa: E402

for _p in (str(HERE), str(HERE.parent.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import glue_ladder as gl  # noqa: E402
from e2e import runner_lib as rl  # noqa: E402

# B1 glue-ladder axis (`analysis/b1-prereg.md`). None => the ladder is entirely
# absent from the run and every code path below is byte-identical to the
# pre-B1 harness; only `--glue-level` sets it (in `main`).
_ACTIVE_LADDER: gl.GlueLadder | None = None

# --------------------------------------------------------------------------- #
# Large-model safety gate (STATE.md hard gate)
# --------------------------------------------------------------------------- #
_LARGE_PREFIXES = ("bedrock:", "openai:gpt-4o", "glm:", "deepseek")
_DOMESTIC = {"solar", "ax", "exaone"}
# `orchestrator` = NO injected model: the production assignment layer
# (`orthus/models/orchestration.py::get_chat_model_for`, ASSIGNMENTS per
# docs/model-orchestration.md §15) resolves the worker per task at run time.
# Domestic-only by construction (requires ORTHUS_MODEL_ORCHESTRATION_ENABLED=true
# + a domestic ORTHUS_LLM default slot), so it is allowed without --final-verify.
_ALLOWED_NONFINAL = _DOMESTIC | {"baseline", "mock", "orchestrator"}


def _is_large_slug(slug: str) -> bool:
    s = slug.lower()
    if s in _ALLOWED_NONFINAL:
        return False
    return any(s.startswith(p) for p in _LARGE_PREFIXES) or s not in _ALLOWED_NONFINAL


def enforce_final_verify_gate(slugs: list[str], final_verify: bool) -> None:
    """Refuse any large/frontier slug unless --final-verify was passed."""
    large = [s for s in slugs if _is_large_slug(s)]
    if large and not final_verify:
        print(
            "REFUSED: large/frontier models require --final-verify AND user "
            f"dataset confirmation. Blocked slugs: {large}\n"
            "Without --final-verify only domestic (solar/exaone/ax) + baseline "
            "(gpt-4o-mini) + mock are allowed (STATE.md hard gate)."
        )
        raise SystemExit(2)


# --------------------------------------------------------------------------- #
# Pool
# --------------------------------------------------------------------------- #
class _ProdAdapterUsageWrapper:
    """Wrap a REAL production `ChatModel` adapter (`BedrockConverseChat` /
    `OpenAIChat`) so Phase-6 large/frontier slugs expose the same
    `model_id` / `reset_usage()` / `usage_totals()` / `.complete()` shape as
    `pool.WorkerChat` (`experiments/fugu-ko/pool.py`), matching how
    `build_pool` wraps the domestic vendors — usage/latency accounting stays
    uniform across every slug in the pool dict, not just `solar`/`ax`/`exaone`.

    The wrapped adapter is the actual `orthus/models/adapters/*` class (never a
    reimplementation, per this harness's `docstring` contract) — its
    `.complete()`, retries, and error behavior are byte-identical to
    production. Neither `BedrockConverseChat.complete()` nor
    `OpenAIChat.complete()` returns raw usage (only text), so every call is
    recorded with `missing=True` token counts here; call count and the
    harness's own per-item `latency_ms` (measured in `run_model`, model-
    agnostic) stay accurate regardless.
    """

    def __init__(self, inner: Any, *, model_id: str):
        self._inner = inner
        self.model_id = model_id
        self.usage_events: list[dict] = []

    def reset_usage(self) -> None:
        self.usage_events = []

    def usage_totals(self) -> dict:
        return {
            "n_llm_calls": len(self.usage_events),
            "in_tok": 0,
            "out_tok": 0,
            "n_usage_missing": len(self.usage_events),
        }

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        self.usage_events.append({"in": None, "out": None, "missing": True})
        return self._inner.complete(system, user, json_only=json_only)


def _build_bedrock_chat(slug: str) -> Any:
    """`bedrock:<modelId>` -> real `BedrockConverseChat`, env-configured.

    Env matches `orthus/models/registry.py`'s own bedrock wiring exactly:
    `ORTHUS_LLM_BEDROCK_API_KEY` (fallback `ORTHUS_LLM_API_KEY`),
    `ORTHUS_LLM_BEDROCK_REGION` (default `us-east-1`),
    `ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX` (default `us`). No network call here —
    the adapter constructor is pure Python; only `.complete()` calls out.
    """
    model_id = slug.split(":", 1)[1] if ":" in slug else ""
    if not model_id:
        raise ValueError(f"bedrock:<modelId> slug missing a model id (got {slug!r})")
    api_key = os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY") or os.environ.get("ORTHUS_LLM_API_KEY", "")
    if not api_key.strip():
        raise ValueError(
            "ORTHUS_LLM_BEDROCK_API_KEY (or ORTHUS_LLM_API_KEY) required for "
            f"bedrock:<modelId> slugs (slug={slug!r})"
        )
    region = os.environ.get("ORTHUS_LLM_BEDROCK_REGION", "us-east-1")
    inference_prefix = os.environ.get("ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX", "us")

    from orthus.models.adapters.bedrock import BedrockConverseChat

    kwargs: dict = {}
    # Opus 4.8+ rejects any explicit `temperature` (HTTP 400, same family quirk
    # as gpt-5) — omit the field so the model default applies.
    if "opus-4-8" in model_id or "opus-4-7" in model_id:
        kwargs["temperature"] = None
    inner = BedrockConverseChat(
        api_key=api_key,
        region=region,
        model_id=model_id,
        inference_prefix=inference_prefix,
        **kwargs,
    )
    return _ProdAdapterUsageWrapper(inner, model_id=slug)


def _build_openai_chat(slug: str) -> Any:
    """`openai:<model>` -> real `OpenAIChat`, env `ORTHUS_LLM_API_KEY` / `ORTHUS_LLM_BASE_URL`."""
    model = slug.split(":", 1)[1] if ":" in slug else ""
    if not model:
        raise ValueError(f"openai:<model> slug missing a model name (got {slug!r})")
    api_key = os.environ.get("ORTHUS_LLM_API_KEY", "")
    if not api_key.strip():
        raise ValueError(f"ORTHUS_LLM_API_KEY required for openai:<model> slugs (slug={slug!r})")
    base_url = os.environ.get("ORTHUS_LLM_BASE_URL", "https://api.openai.com/v1")

    from orthus.models.adapters.openai_compat import OpenAIChat

    # The gpt-5 family (gpt-5.3-chat-latest, gpt-5.5) rejects any `temperature`
    # value but its own default (HTTP 400 on `temperature=0`, which OpenAIChat
    # sends by default). Omit the field for that family; other openai:<model>
    # slugs keep temperature=0. (gpt-5.5 also requires `max_completion_tokens`
    # instead of `max_tokens`, but OpenAIChat sends no token cap at all, so no
    # further handling is needed here.)
    kwargs: dict = {}
    if model.startswith("gpt-5"):
        kwargs["temperature"] = None
    inner = OpenAIChat(base_url, api_key, model, **kwargs)
    return _ProdAdapterUsageWrapper(inner, model_id=slug)


_GLM_BASE_URL = "https://api.z.ai/api/paas/v4/"


def _build_glm_chat(slug: str) -> Any:
    """`glm:<model>` -> real `OpenAIChat` (GLM is OpenAI-compatible) pointed at
    `_GLM_BASE_URL`, env `ORTHUS_GLM_API_KEY` (harness-only var, not a
    `orthus/settings.py` field — see `LIVE_BENCH_RUNBOOK.md`)."""
    model = slug.split(":", 1)[1] if ":" in slug else ""
    if not model:
        raise ValueError(f"glm:<model> slug missing a model name (got {slug!r})")
    api_key = os.environ.get("ORTHUS_GLM_API_KEY", "")
    if not api_key.strip():
        raise ValueError(f"ORTHUS_GLM_API_KEY required for glm:<model> slugs (slug={slug!r})")

    from orthus.models.adapters.openai_compat import OpenAIChat

    inner = OpenAIChat(_GLM_BASE_URL, api_key, model)
    return _ProdAdapterUsageWrapper(inner, model_id=slug)


def _build_deepseek_chat(slug: str) -> Any:
    """`deepseek` (or `deepseek:<model>`) -> real `OpenAIChat` (DeepSeek's
    official API is OpenAI-compatible), symmetric to `_build_glm_chat`.

    env (harness-only vars, not `orthus/settings.py` fields — like `ORTHUS_GLM_API_KEY`):
    `ORTHUS_LLM_DEEPSEEK_API_KEY` (required),
    `ORTHUS_LLM_DEEPSEEK_MODEL` (default `deepseek-chat` = V3.2 series),
    `ORTHUS_LLM_DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`).
    A `deepseek:<model>` slug suffix overrides `ORTHUS_LLM_DEEPSEEK_MODEL`.
    No network call here — only `.complete()` calls out."""
    override = slug.split(":", 1)[1] if ":" in slug else ""
    model = override or os.environ.get("ORTHUS_LLM_DEEPSEEK_MODEL", "deepseek-chat")
    api_key = os.environ.get("ORTHUS_LLM_DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise ValueError(f"ORTHUS_LLM_DEEPSEEK_API_KEY required for deepseek slugs (slug={slug!r})")
    base_url = os.environ.get("ORTHUS_LLM_DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    from orthus.models.adapters.openai_compat import OpenAIChat

    inner = OpenAIChat(base_url, api_key, model)
    return _ProdAdapterUsageWrapper(inner, model_id=slug)


def _build_codex_chat(slug: str) -> Any:
    """`codex:<model>` -> resident `CodexPoolChat` on ChatGPT-plan OAuth.

    Escape hatch for models whose API key is quota-blocked (2026-07-23:
    `openai:gpt-5.5` 429s; the same model answers via Codex OAuth). Uses the
    production pool adapter (`orthus/models/adapters/codex_pool.py`) with a
    dedicated CODEX_HOME from `ORTHUS_E2E_CODEX_HOME` (provisioned via
    scripts/ops/provision_compile_codex_home.py --model <model>). The slug's
    model suffix MUST match the home's config.toml `model` — fail-closed check
    here so a stale home can never silently run the wrong model. Caveat for
    reports: OpenAI does not guarantee the Codex-served snapshot equals the
    API snapshot of the same name — annotate results "via Codex OAuth".
    """
    import tomllib

    want = slug.split(":", 1)[1] if ":" in slug else ""
    home = os.environ.get("ORTHUS_E2E_CODEX_HOME", "").strip()
    if not want or not home:
        raise ValueError(
            f"codex slug needs a model suffix and ORTHUS_E2E_CODEX_HOME (slug={slug!r})"
        )
    cfg = Path(os.path.expanduser(home)) / "config.toml"
    got = tomllib.loads(cfg.read_text(encoding="utf-8")).get("model", "")
    if got != want:
        raise ValueError(f"CODEX_HOME model mismatch: home={got!r} slug={want!r}")

    from orthus.models.adapters.codex_pool import CodexPoolChat

    pool_size = int(os.environ.get("ORTHUS_E2E_CODEX_POOL_SIZE", "3"))
    inner = CodexPoolChat(pool_size=pool_size, home_base=home, model_id=slug)
    return _ProdAdapterUsageWrapper(inner, model_id=slug)


def build_e2e_pool(slugs: list[str]) -> dict[str, Any]:
    """slug -> ChatModel. Adds special slug `mock` (offline MockChat).

    `bedrock:<modelId>` / `openai:<model>` / `glm:<model>` / `deepseek` slugs
    construct the real production adapter (wiring only — no call is made here).
    These are exactly the `_is_large_slug` prefixes `enforce_final_verify_gate` refuses
    without `--final-verify`; the gate check in `main()` runs BEFORE this
    function is ever called, so construction (let alone a network call) is
    unreachable on a refused run.
    """
    from orthus.models.adapters.mock import MockChat

    pool: dict[str, Any] = {}
    domestic = [s for s in slugs if s in (_DOMESTIC | {"baseline"})]
    if "mock" in slugs:
        pool["mock"] = MockChat()
    if "orchestrator" in slugs:
        # No injected chat: dispatch_l2 skips the get_chat_model monkeypatch when
        # chat is None, so production `get_chat_model_for(task)` resolves the
        # assigned worker itself (the whole point of the orchestrator arm).
        pool["orchestrator"] = None
    if domestic:
        from pool import build_pool

        pool.update(build_pool(domestic))
    for slug in slugs:
        if slug in pool:
            continue
        if slug.startswith("bedrock:"):
            pool[slug] = _build_bedrock_chat(slug)
        elif slug.startswith("openai:"):
            pool[slug] = _build_openai_chat(slug)
        elif slug.startswith("glm:"):
            pool[slug] = _build_glm_chat(slug)
        elif slug == "deepseek" or slug.startswith("deepseek:"):
            pool[slug] = _build_deepseek_chat(slug)
        elif slug.startswith("codex:"):
            pool[slug] = _build_codex_chat(slug)
    return pool


def is_mock_model(slug: str) -> bool:
    return slug == "mock"


# --------------------------------------------------------------------------- #
# L1 dispatch — one adapter per task code. Each returns a normalized output +
# reached_llm (deterministic fast-path detection, reused from harness.py logic).
# --------------------------------------------------------------------------- #
def _l1_reached_llm(task: str, q: str, *, ext_tier: int | None = None) -> bool:
    from orthus.agentwork.service import detect_assistant_command_action
    from orthus.router.decompose import _has_command_verb, _has_connective_or_enum
    from orthus.router.route import _rule_based_route

    if task == "t5":
        return _rule_based_route(q) is None
    if task == "t6":
        return detect_assistant_command_action(q) is None and _rule_based_route(q) is None
    if task == "t7":
        compact = q.lower().replace(" ", "")
        return _has_command_verb(compact) or _has_connective_or_enum(q, ext_tier=ext_tier or 0)
    # t3/t9/t10/t2/t8: the LLM is always on the path.
    return True


# harness-side company-scope user id (matches harness.py convention). Shared
# with runner_lib so `ensure_l1_harness_user()` seeds the same row.
_UID = rl.L1_HARNESS_UID


def dispatch_l1(task: str, req: dict, chat) -> dict:
    """Call the production entry point with chat_model injected. Returns a dict
    with `output` (normalized for scoring) and `reached_llm`."""
    from orthus.router.route import classify, classify_intent
    from orthus.router.decompose import should_decompose
    from orthus.router.graph import bind_graph_params
    from orthus.structured.query import query_structured
    from orthus.wiki.qa import ask
    from orthus.agentwork.delegation import extract_delegation_intent
    from orthus.settings import get_settings

    q = req.get("question") or req.get("text") or ""
    # t7_holdout / e3 items pin an explicit prefilter tier on the request
    # (manifest_schema.md; e.g. tag `prefilter_ext_tier_3` -> request.ext_tier=3).
    # `should_decompose`'s own default (None -> settings) would silently fall
    # back to tier 0 here since the harness never sets
    # ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER, so this must be threaded explicitly.
    ext_tier = req.get("ext_tier")
    reached = _l1_reached_llm(task, q, ext_tier=ext_tier)

    if task == "t5":
        route = classify(q, chat_model=chat)
        return {"output": route, "reached_llm": reached}
    if task == "t6":
        intent = classify_intent(q, allow_commands=req.get("allow_commands", True), chat_model=chat)
        label = intent.command_family or intent.route
        return {"output": label, "reached_llm": reached}
    if task == "t7":
        gate = should_decompose(q, chat_model=chat, ext_tier=ext_tier)
        return {"output": bool(gate), "reached_llm": reached}
    if task == "t9":
        intent_out: list[str] = []
        binding = bind_graph_params(q, chat_model=chat, intent_out=intent_out)
        intent = intent_out[0] if intent_out else (binding.intent if binding else None)
        return {
            "output": {"intent": intent, "binding": bool(binding)},
            "reached_llm": True,
        }
    if task == "t10":
        settings = get_settings()
        out = extract_delegation_intent(q, settings, chat_model=chat)
        return {"output": out, "reached_llm": True}
    if task == "t3":
        if _ACTIVE_LADDER is not None:
            # B1: the ladder owns the t3 call(s). At `--glue-level 0` it makes the
            # same single t0 call and applies no post-processing (b1-prereg §2 L0).
            return _ACTIVE_LADDER.run_t3(_UID, q, chat, scope=req.get("scope", "company"))
        r = query_structured(_UID, q, scope=req.get("scope", "company"), chat_model=chat)
        return {
            "output": {
                "gate_pass": bool(r.validation.passed),
                "status": r.status,
                "rows": r.rows,
            },
            "reached_llm": True,
        }
    if task == "t2":
        a = ask(
            _UID,
            q,
            k=req.get("k", 5),
            scope=req.get("scope", "company"),
            chat_model=chat,
            learn=req.get("learn", False),
            record_gaps=req.get("record_gaps", False),
        )
        return {
            "output": {"answer": a.answer, "n_sources": len(a.sources)},
            "reached_llm": True,
        }
    if task == "t8":
        # synthesize needs frozen sub_answers/sub_questions not present in input
        # (STATE.md: requires_frozen_subs -> qualitative only). Plumbing only.
        return {"output": None, "reached_llm": True, "qualitative_only": True}
    raise ValueError(f"unknown L1 task: {task}")


def _strip_honorific(value: Any) -> str:
    """Strip a trailing Korean honorific (님/씨, with or without a leading space)
    before string comparison. Model-independent normalization for t10 assignee
    matching (STATE.md: EXAONE-style "홍길동님" vs golden "홍길동" should
    still count as a mode-match pass)."""
    s = str(value).strip()
    for suffix in ("님", "씨"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
            break
    return s


def score_l1_exact(task: str, output: Any, expected_value: Any) -> tuple[bool, str]:
    if task == "t5":
        return (output == expected_value), f"{output!r} vs {expected_value!r}"
    if task == "t6":
        return (output == expected_value), f"{output!r} vs {expected_value!r}"
    if task == "t7":
        return (bool(output) == bool(expected_value)), f"{output!r} vs {expected_value!r}"
    if task == "t9":
        want_intent = (expected_value or {}).get("intent")
        got_intent = (output or {}).get("intent")
        return (got_intent == want_intent), f"intent {got_intent!r} vs {want_intent!r}"
    if task == "t10":
        if expected_value is None:
            return (output is None), f"expected null, got {output!r}"
        if not isinstance(output, dict):
            return False, f"expected dict, got {output!r}"
        ok = all(
            _strip_honorific(output.get(k)) == _strip_honorific(v)
            for k, v in expected_value.items()
        )
        return ok, f"{output!r} superset of {expected_value!r}: {ok}"
    if task == "t3":
        want = expected_value or {}
        gate_ok = bool(output.get("gate_pass")) == bool(want.get("gate_pass"))
        # `gate_only` items (STATE.md: 10 t3 items excluded by t3_gold.py from
        # live-DB verification) carry expected.value.result=null — only the
        # validation gate is asserted, never an intset comparison.
        if want.get("result") is None:
            return gate_ok, f"gate {gate_ok} (gate_only, no result to compare)"
        got_rows = output.get("rows") or []
        # t3 goldens are int-sets of aggregate counts. Compare counts-only:
        # keep numeric cells and drop string group labels, whose surface form
        # varies by model (e.g. `SELECT col, COUNT(*) ... GROUP BY col` emits a
        # label column the golden never carries). `_flatten_rows` set-ified all
        # cells including labels, so a fully-correct grouped count still failed.
        got = _t3_counts_only(got_rows)
        rows_ok = got == set(want["result"])
        return (gate_ok and rows_ok), f"gate {gate_ok}, rows {rows_ok}"
    return False, "no scorer"


def _flatten_rows(rows: Any) -> list:
    out: list = []
    for r in rows or []:
        if isinstance(r, dict):
            out.extend(r.values())
        elif isinstance(r, (list, tuple)):
            out.extend(r)
        else:
            out.append(r)
    return out


def _t3_counts_only(rows: Any) -> set:
    """Set of the numeric (int/float) cells across t3 result rows, dropping
    string group labels. bool is excluded (it is an int subclass but never a
    count). Used only by the t3 scorer — `_flatten_rows` stays as-is for any
    other caller."""
    out: set = set()
    for cell in _flatten_rows(rows):
        if isinstance(cell, bool):
            continue
        if isinstance(cell, (int, float)):
            out.add(cell)
    return out


# --------------------------------------------------------------------------- #
# L2 dispatch — TestClient against the real route + fixture-seeded scratch DB.
# --------------------------------------------------------------------------- #
def _auth_override(actor: dict):
    from orthus.auth import AuthenticatedUser

    uid = actor.get("user_id")
    user_id = uuid.UUID(uid) if uid else uuid.uuid4()
    kwargs = dict(
        user_id=user_id,
        auth_mode=actor.get("auth_mode", "session"),
        display_name=actor.get("display_name", "Actor"),
        role=actor.get("role", "owner"),
        node_id=actor.get("node_id", "company"),
    )
    if actor.get("email"):
        kwargs["email"] = actor["email"]
    return lambda: AuthenticatedUser(**kwargs)


def _load_fixture(item: rl.Item) -> dict | None:
    fx = item.input.get("fixture")
    if not fx:
        return None
    path = rl.REPO / "experiments" / "fugu-ko" / fx["path"]
    if not path.exists():
        path = rl.HERE / fx["path"].replace("e2e/", "")
    return json.loads(path.read_text("utf-8"))


class _ScriptedMock:
    """Return a fixed distill/JSON script for the scripted g1-S regression items."""

    model_id = "mock:scripted"

    def __init__(self, script: str):
        self._script = script

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self._script


def _mail_ingest_auth(body: dict, tags: list[str] | None = None) -> tuple[bytes, dict[str, str]]:
    """Sign a `POST /mail/ingest` body per `orthus/mail/ingest.py::verify_mail_ingest_auth`
    (Bearer + D15 HMAC(timestamp.body)). g3 ingest fixtures document that the
    harness — not a session actor — must produce this envelope itself; the
    secrets come from the fixture's `settings` block, already applied to the
    live `Settings` singleton by `apply_fixture()` before this runs. Returns
    the exact raw body bytes the signature covers, so the caller must send
    those bytes verbatim (not re-serialize via `json=`, which could byte-drift).

    `auth_probe:bad_bearer` / `auth_probe:bad_hmac` tags (g3 negative-auth
    items, expect 401) deliberately corrupt the matching credential so a
    correctly-signed default doesn't mask what they're testing.
    """
    import hashlib
    import hmac as hmac_lib
    import time as time_lib

    from orthus.settings import get_settings

    tags = tags or []
    settings = get_settings()
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    ts = str(int(time_lib.time()))
    signed = f"{ts}.".encode("utf-8") + raw_body
    secret = (settings.mail_ingest_hmac_secret or "").encode("utf-8")
    if "auth_probe:bad_hmac" in tags:
        secret = secret + b"-wrong"
    sig = hmac_lib.new(secret, signed, hashlib.sha256).hexdigest()
    bearer = settings.mail_ingest_secret
    if "auth_probe:bad_bearer" in tags:
        bearer = (bearer or "") + "-wrong"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "X-Orthus-Timestamp": ts,
        "X-Orthus-Signature": f"sha256={sig}",
        "Content-Type": "application/json",
    }
    return raw_body, headers


def dispatch_l2(item: rl.Item, model_slug: str, chat) -> dict:
    """Seed fixture, drive the real route, return an `actual` projection dict.

    Fail-closed (defect-1 fix): every L2 item goes through `apply_fixture()`
    or a bare `truncate_all_tables()` below, so on a non-test/staging DSN
    (`rl.truncate_guard_ok()` False — e.g. someone sourced company `node.env`)
    this returns a `skipped` marker BEFORE touching the DB at all, rather than
    letting `truncate_all_tables()`/`apply_fixture()` no-op/raise partway
    through. `run_model` turns this into a `skipped` score (not `fail`).
    """
    if not rl.truncate_guard_ok():
        return {"skipped": True, "reason": "live_db_truncate_guard"}

    from orthus.api.deps import get_current_user
    from orthus.api.main import app
    from fastapi.testclient import TestClient

    fixture = _load_fixture(item)
    actor = rl.apply_fixture(fixture) if fixture else {}
    if not fixture:
        rl.truncate_all_tables()
        rl.reset_test_settings()

    req = item.input["request"]
    method = req.get("method", "POST").upper()
    path = req.get("path", "")
    body = req.get("body")
    query = req.get("query") or {}

    # WIRING FIX (g2 orchestrate): the g2 fixtures seed an `agent_chat_sessions`
    # row owned by a specific user, but their `actor` block omits `user_id`. Left
    # unset, `_auth_override` mints a random uuid4 that never matches the session
    # owner, so `orchestrate_chat_route` -> `get_session(current.user_id, ...)`
    # returns None and 404s. The 404 body carries no `mode`/`agent_work.*`, so
    # every g2 item scored `deferred: projection_incomplete` — a fixture/actor
    # binding gap, NOT a `_project_l2_state` projection gap (results/routing-
    # decompose.md §2.3 misdiagnosed this). Bind the actor to the seeded session
    # owner when the fixture leaves user_id unset. Only fires for fixtures that
    # seed agent_chat_sessions (currently g2 only), so g1/g3/g4 + all L1 paths
    # are byte-identical.
    if actor and not actor.get("user_id") and fixture:
        _seeded = (fixture.get("rows") or {}).get("agent_chat_sessions") or []
        if _seeded:
            import re as _re

            _m = _re.search(r"/chats/([0-9a-fA-F-]{36})/", path)
            _path_sid = _m.group(1) if _m else None
            _match = next(
                (r for r in _seeded if str(r.get("session_id")) == _path_sid),
                _seeded[0],
            )
            _owner = _match.get("user_id") or _match.get("owner_id")
            if _owner:
                actor = {**actor, "user_id": str(_owner)}

    client = TestClient(app, raise_server_exceptions=False)
    overrides: list = []
    patches: list = []

    if actor:
        app.dependency_overrides[get_current_user] = _auth_override(actor)
        overrides.append(get_current_user)

    # Scripted MockChat for g1-S regression items (deterministic distill).
    script = (fixture or {}).get("mock_chat_script")
    if script:
        import orthus.models.orchestration as orch

        orig = orch.get_chat_model
        orch.get_chat_model = lambda: _ScriptedMock(script)
        patches.append(("orch", orig))
        # Orchestrator arm: with ORTHUS_MODEL_ORCHESTRATION_ENABLED=true a live
        # ASSIGNMENTS hit would bypass the scripted default and run a real
        # worker on a deterministic regression item. Empty the table for the
        # duration so `get_chat_model_for` falls through to the scripted
        # default (restored in `finally`).
        orig_assign = orch.ASSIGNMENTS
        orch.ASSIGNMENTS = {}
        patches.append(("assign", orig_assign))

    # Model swap for discriminating L2 items (not needed under mock default).
    if model_slug not in ("mock", "baseline") and chat is not None and not script:
        import orthus.models.orchestration as orch
        import orthus.models.registry as reg

        o1, o2 = orch.get_chat_model, reg.get_chat_model
        orch.get_chat_model = lambda: chat
        reg.get_chat_model = lambda: chat
        patches.append(("orch", o1))
        patches.append(("reg", o2))

    # g3 send-gate items need a deterministic monkeypatched sender.
    send_patches = _maybe_patch_send(item)

    try:
        if method == "GET":
            resp = client.get(path, params=query)
        elif method == "POST" and path == "/mail/ingest":
            # HMAC-authenticated route (no session actor) — sign the exact
            # bytes we send, do not let httpx re-serialize `json=`.
            raw_body, sign_headers = _mail_ingest_auth(body or {}, item.tags)
            resp = client.post(path, content=raw_body, params=query, headers=sign_headers)
        elif method == "POST":
            resp = client.post(path, json=body, params=query)
        else:
            resp = client.request(method, path, json=body, params=query)
        try:
            payload = resp.json()
        except Exception:
            payload = None
        actual = {"status": resp.status_code}
        if isinstance(payload, dict):
            actual.update(payload)
        actual["_response"] = payload
        actual.update(_project_l2_state(item, actor, payload))
        return actual
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)
        for kind, orig in reversed(patches):
            if kind == "orch":
                import orthus.models.orchestration as orch

                orch.get_chat_model = orig
            elif kind == "reg":
                import orthus.models.registry as reg

                reg.get_chat_model = orig
            elif kind == "assign":
                import orthus.models.orchestration as orch

                orch.ASSIGNMENTS = orig
        for restore in send_patches:
            restore()


def _maybe_patch_send(item: rl.Item) -> list:
    """Monkeypatch send_mail to a fixed result for g3 send-gate items (§5 DESIGN:
    determinism via _stub_send, NOT ORTHUS_EMAIL_SENDER)."""
    restores: list = []
    ep = item.entry_point
    if item.task != "g3":
        return restores
    if "/mail/send" not in ep and "/decision" not in ep:
        return restores
    try:
        from orthus.mail.compose import MailSendResult
    except Exception:
        try:
            from orthus.schemas.canonical import MailSendResult  # type: ignore
        except Exception:
            return restores

    def _stub(*a, **k):
        return MailSendResult(backend="acme", status="sent", provider_message_id="re_stub")

    for modname, attr in (
        ("orthus.api.routes.mail", "send_mail"),
        ("orthus.mail.reply", "send_mail"),
    ):
        try:
            mod = __import__(modname, fromlist=["_"])
            if hasattr(mod, attr):
                orig = getattr(mod, attr)
                setattr(mod, attr, _stub)
                restores.append(lambda m=mod, a=attr, o=orig: setattr(m, a, o))
        except Exception:
            continue
    return restores


def _project_l2_state(item: rl.Item, actor: dict, payload: Any) -> dict:
    """Compute the custom `assert` keys each flow needs from DB + response.

    Any key we cannot compute is left ABSENT so the scorer marks the item
    deferred (incomplete projection) rather than falsely failing it.
    """
    proj: dict = {}
    task = item.task
    if task == "g4":
        proj.update(_project_g4())
    elif task == "g2":
        proj.update(_project_g2(payload))
    elif task == "g3":
        proj.update(_project_g3(item, actor, payload))
    elif task == "g1":
        proj.update(_project_g1(payload))
    return proj


def _project_g4() -> dict:
    from sqlalchemy import func, select

    from orthus.db import session
    from orthus.tables import collector_commands

    with session() as s:
        n = int(s.execute(select(func.count()).select_from(collector_commands)).scalar_one())
    return {"collector_commands_count": n}


def _project_g2(payload: Any) -> dict:
    # g2 asserts use dotted paths already present in the RoutedAnswer payload
    # (mode, agent_work.*) + a computed part_count.
    proj: dict = {}
    if isinstance(payload, dict):
        parts = payload.get("parts") or (payload.get("decomposed") or {}).get("parts")
        # Always project a count (0 when the answer did not decompose) so a
        # part_count_min assert FAILS on a non-decomposed answer instead of
        # deferring as projection_incomplete (which would excuse the miss).
        proj["part_count"] = len(parts) if isinstance(parts, list) else 0
    return proj


def _project_g3(item: rl.Item, actor: dict, payload: Any) -> dict:
    from sqlalchemy import func, select

    from orthus.db import session
    from orthus.tables import email_send_log

    proj: dict = {}
    if isinstance(payload, dict):
        if "ingested" in payload:
            proj["ingested"] = payload.get("ingested")
        if "scope" in payload:
            proj["scope"] = payload.get("scope")
        wid = payload.get("work_id")
        proj["work_id_present"] = bool(wid)
    with session() as s:
        proj["log_count"] = int(
            s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
        )
    # g3-X (mail -> delegation extraction): project the delegation-family item
    # created by `orthus/mail/delegation.py::build_delegation_candidate`
    # (action_family='agent_task'). Keys match the g3-x assert spec
    # (delegation_work_id_present / delegation_state / delegation_reason_codes).
    if item.has_tag("g3-x"):
        from orthus.tables import agent_work_items

        with session() as s:
            rows = s.execute(
                select(
                    agent_work_items.c.work_id,
                    agent_work_items.c.state,
                    agent_work_items.c.reason_codes,
                ).where(agent_work_items.c.action_family == "agent_task")
            ).all()
        proj["delegation_work_id_present"] = bool(rows)
        if rows:
            proj["delegation_state"] = rows[0][1]
            proj["delegation_reason_codes"] = list(rows[0][2] or [])
        else:
            proj["delegation_state"] = None
            proj["delegation_reason_codes"] = []
    return proj


def _project_g1(payload: Any) -> dict:
    """Project resulting wiki state for g1-S structural items."""
    from sqlalchemy import select

    from orthus.db import session

    proj: dict = {}
    try:
        from orthus.tables import wiki_pages

        with session() as s:
            rows = s.execute(select(wiki_pages.c.kind, wiki_pages.c.slug)).all()
        kinds = [r[0] for r in rows]
        proj["resulting_wiki_page_kinds"] = sorted(set(kinds))
        proj["resulting_claim_count"] = sum(1 for k in kinds if k == "claim")
        proj["resulting_wiki_pages_row_count"] = len(rows)
    except Exception:
        pass
    return proj


# --------------------------------------------------------------------------- #
# Model-independence classification (what is scorable under a mock/live run)
# --------------------------------------------------------------------------- #
def is_model_independent(item: rl.Item, result: dict, model_slug: str) -> bool:
    """True => score pass/fail; False => 'deferred to live bench'."""
    if item.kind in ("judge", "metric"):
        return False
    if "model_fallback_zero" in item.invariants:
        return False  # explicitly a model-discriminating item
    if item.layer == "L2":
        return item.kind == "structural"
    # L1: scorable if a deterministic fast-path answered, or a real model ran.
    if item.task in ("t2", "t8"):
        return False
    # e3_control/e3_missed/t7_holdout (STATE.md flag): identity is carried in
    # tags, not a distinct task code. These are set-wide e3_prefilter items
    # (`aggregate_e3`'s missed_recall/mis_split_rate) — never scored as a
    # generic per-item t7 pass/fail, at any model (deterministic prefilter
    # tuning + LLM stage-2 both fold into the aggregate, not a single-item gate).
    if item.task == "t7" and item.has_tag(
        "missed_probe", "control_probe", "aggregate_scored_set_wide"
    ):
        return False
    if is_mock_model(model_slug):
        if item.task in ("t3", "t9", "t10"):
            return False  # need live DB / always reach the LLM
        return not result.get("reached_llm", True)
    return True


# --------------------------------------------------------------------------- #
# Scoring dispatch
# --------------------------------------------------------------------------- #
def score_item(item: rl.Item, result: dict, model_slug: str) -> dict:
    """Return {status: pass|fail|deferred|error|skipped, ...}. `status` drives
    exit code only for model-independent items."""
    if result.get("skipped"):
        return {"status": "skipped", "reason": result.get("reason", "skipped")}
    if result.get("error"):
        return {"status": "error", "detail": result["error"]}

    kind = item.kind
    scored = is_model_independent(item, result, model_slug)

    if kind == "structural":
        actual = result.get("actual") or {}
        assert_spec = item.expected.get("assert", {})
        # If any non-dotted assert key was never projected, projection is
        # incomplete -> defer (do not falsely fail).
        if _projection_incomplete(actual, assert_spec):
            return {"status": "deferred", "reason": "projection_incomplete"}
        ok, failures = rl.structural_assert(actual, assert_spec)
        cz = _confident_zero_flag(item, result)
        if not scored:
            return {"status": "deferred", "assert_ok": ok, "failures": failures}
        if cz:
            return {"status": "fail", "reason": "confident_zero", "failures": failures}
        return {"status": "pass" if ok else "fail", "failures": failures}

    if kind == "metric":
        out_set = (result.get("output") or {}).get("claims") or result.get("output_set") or []
        ok, detail = rl.score_metric(out_set, item.expected)
        return {"status": "deferred", "metric": detail}  # metric => live bench

    if kind == "judge":
        return {"status": "deferred", "reason": "judge_live_only"}

    # exact
    output = result.get("output")
    ok, detail = score_l1_exact(item.task, output, item.expected.get("value"))
    cz = _confident_zero_flag(item, result)
    if not scored:
        return {"status": "deferred", "exact_ok": ok, "detail": detail}
    if cz:
        return {"status": "fail", "reason": "confident_zero", "detail": detail}
    return {"status": "pass" if ok else "fail", "detail": detail}


def _projection_incomplete(actual: dict, assert_spec: dict) -> bool:
    for key in assert_spec:
        base = key
        for suf in ("_contains_any", "_contains", "_min"):
            if base.endswith(suf) and base not in actual:
                base = base[: -len(suf)]
                break
        if "." in base:
            continue  # dotted paths read from response object, treated present
        if base not in actual:
            return True
    return False


def _confident_zero_flag(item: rl.Item, result: dict) -> bool:
    if "no_confident_zero" not in item.invariants:
        return False
    output = result.get("output")
    if item.kind == "structural":
        output = (result.get("actual") or {}).get("mode")
    return rl.is_confident_zero(item.task, output, expected_nonempty=True)


# --------------------------------------------------------------------------- #
# Aggregate metrics (e3 set-wide) — manifest_schema.md §7.2
# --------------------------------------------------------------------------- #
def aggregate_e3(rows: list[dict]) -> dict | None:
    missed = [r for r in rows if r["item"].has_tag("missed_probe")]
    control = [r for r in rows if r["item"].has_tag("control_probe")]
    if not missed and not control:
        return None

    def _correct(r):
        return r["score"].get("status") == "pass" or r["score"].get("exact_ok") is True

    missed_recall = sum(1 for r in missed if _correct(r)) / len(missed) if missed else None
    # mis-split = control items that WRONGLY decomposed (expected False, got True)
    mis = 0
    for r in control:
        got = r["result"].get("output")
        if got is True:
            mis += 1
    mis_rate = mis / len(control) if control else None
    return {
        "missed_n": len(missed),
        "missed_recall": missed_recall,
        "missed_recall_min": 0.80,
        "control_n": len(control),
        "mis_split_rate": mis_rate,
        "mis_split_max": 0.05,
        "recall_pass": (missed_recall is None or missed_recall >= 0.80),
        "mis_split_pass": (mis_rate is None or mis_rate <= 0.05),
    }


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
# Orchestrator-arm slot evidence: every LLM call that goes through the
# production per-task wrapper (`orchestration.FallbackChat.complete`) is
# recorded as (task, primary model_id) so the run can PROVE different models
# served different stages. Observation only — the original method still runs.
_SLOT_CALLS: list[tuple[str, str]] = []
_SLOT_RECORDER_INSTALLED = False


def _install_slot_recorder() -> None:
    global _SLOT_RECORDER_INSTALLED
    if _SLOT_RECORDER_INSTALLED:
        return
    from orthus.models import orchestration as orch

    orig = orch.FallbackChat.complete

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        _SLOT_CALLS.append((self.task, getattr(self._primary, "model_id", "?")))
        return orig(self, system, user, json_only=json_only)

    orch.FallbackChat.complete = complete
    _SLOT_RECORDER_INSTALLED = True


def run_model(slug: str, chat, items: list[rl.Item]) -> list[dict]:
    # `default_manifest_paths` always orders all L1 items (tier_a/tier_b)
    # ahead of L2 (`l2/g*.jsonl`), so seeding once here survives every L1
    # item; L2 dispatch (which truncates+reseeds per fixture) only runs
    # after. Re-seeded per model since a prior model's L2 phase truncated it.
    rl.ensure_l1_harness_user()
    if slug == "orchestrator":
        _install_slot_recorder()
    rows: list[dict] = []
    for item in items:
        t0 = time.monotonic()
        slot_before = len(_SLOT_CALLS)
        calls_before = None
        if chat is not None and hasattr(chat, "usage_totals"):
            try:
                calls_before = int(chat.usage_totals().get("n_llm_calls", 0))
            except Exception:  # noqa: BLE001 — usage capture must never break a run
                calls_before = None
        result: dict = {}
        try:
            if item.layer == "L1":
                result = dispatch_l1(item.task, item.input.get("request", {}), chat)
            else:
                actual = dispatch_l2(item, slug, chat)
                if isinstance(actual, dict) and actual.get("skipped"):
                    result = {"skipped": True, "reason": actual.get("reason")}
                else:
                    result = {"actual": actual, "output": actual}
        except Exception as e:  # noqa: BLE001
            result = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        if slug == "orchestrator":
            slot_calls = _SLOT_CALLS[slot_before:]
            result["slot_calls"] = [list(c) for c in slot_calls]
            result["n_llm_calls"] = len(slot_calls)
        if calls_before is not None:
            try:
                result["n_llm_calls"] = (
                    int(chat.usage_totals().get("n_llm_calls", 0)) - calls_before
                )
            except Exception:  # noqa: BLE001 — usage capture must never break a run
                pass
        if _ACTIVE_LADDER is not None:
            # B1 §4.3: per-stage rescue/breakage needs each ladder stage scored by
            # the SAME scorer as the final answer. Tasks with no existing glue
            # implementation record that fact instead of a fabricated stage list.
            if item.layer == "L1" and not _ACTIVE_LADDER.applies(item.task):
                result.setdefault("glue", _ACTIVE_LADDER.passthrough(item.task))
            gl.annotate_stage_scores(item, result, score_l1_exact)
        score = score_item(item, result, slug)
        rows.append({"item": item, "result": result, "score": score})
    return rows


def _serializable(result: dict) -> dict:
    out = {}
    for k, v in result.items():
        if k in ("actual",):
            v = {kk: vv for kk, vv in (v or {}).items() if kk != "_response"}
        try:
            json.dumps(v, ensure_ascii=False, default=str)
            out[k] = v
        except Exception:
            out[k] = str(v)
    return out


def write_raw(slug: str, rows: list[dict]) -> Path:
    rl.RAW_DIR.mkdir(parents=True, exist_ok=True)
    # 병렬 실행 안전: summary와 동일하게 raw 파일명도 env로 분리할 수 있다
    # (미설정 시 기존 그대로 = byte-identical — 동시 실행 중인 다른 벤치의
    # e2e_<slug>.jsonl을 덮지 않기 위한 opt-in).
    _prefix = os.environ.get("ORTHUS_E2E_RAW_PREFIX", "")
    out = rl.RAW_DIR / f"e2e_{_prefix}{slug}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            item = r["item"]
            rec = {
                "id": item.id,
                "task": item.task,
                "layer": item.layer,
                "kind": item.kind,
                "score": r["score"],
                "latency_ms": r["result"].get("latency_ms"),
                "result": _serializable(r["result"]),
            }
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return out


def correctness_map(rows: list[dict]) -> dict[str, bool]:
    """id -> bool for every `pass`/`fail` (model-independently scored) item.

    Deferred/error rows are excluded (not "incorrect") so paired comparisons
    below only pair ids both models actually scored, matching
    `mcnemar_from_correct`/`bootstrap_paired_diff_ci`'s id-intersection join.
    """
    out: dict[str, bool] = {}
    for r in rows:
        status = r["score"]["status"]
        if status in ("pass", "fail"):
            out[r["item"].id] = status == "pass"
    return out


def pairwise_model_stats(all_rows_by_model: dict[str, list[dict]]) -> list[dict]:
    """McNemar + bootstrap-CI on paired accuracy for every model pair.

    Only meaningful once >=2 models ran (a live domestic bench comparison,
    e.g. `--models solar,exaone`); a single-model smoke returns [].
    """
    import itertools

    slugs = list(all_rows_by_model)
    if len(slugs) < 2:
        return []
    correct = {slug: correctness_map(rows) for slug, rows in all_rows_by_model.items()}
    out: list[dict] = []
    for a, b in itertools.combinations(slugs, 2):
        mc = rl.mcnemar_from_correct(correct[a], correct[b])
        if mc["n_paired"] == 0:
            continue
        ci_lo, ci_hi = rl.bootstrap_paired_diff_ci(correct[a], correct[b])
        out.append(
            {
                "a": a,
                "b": b,
                "n_paired": mc["n_paired"],
                "a_only_correct": mc["a_only"],
                "b_only_correct": mc["b_only"],
                "mcnemar_p_value": mc["p_value"],
                "significant_p05": mc["significant"],
                "bootstrap_diff_ci95": [ci_lo, ci_hi],
            }
        )
    return out


def summarize(slug: str, rows: list[dict]) -> dict:
    from collections import Counter

    statuses = Counter(r["score"]["status"] for r in rows)
    scored = [r for r in rows if r["score"]["status"] in ("pass", "fail")]
    passed = sum(1 for r in scored if r["score"]["status"] == "pass")
    lat = [r["result"].get("latency_ms", 0) for r in rows]
    p50, p95 = rl.percentiles(lat)
    per_task: dict[str, dict] = {}
    for r in rows:
        t = r["item"].task
        d = per_task.setdefault(t, {"pass": 0, "fail": 0, "deferred": 0, "error": 0})
        st = r["score"]["status"]
        d[st] = d.get(st, 0) + 1
    return {
        "model": slug,
        "n_items": len(rows),
        "status_counts": dict(statuses),
        "scored": len(scored),
        "passed": passed,
        "accuracy_on_scored": (passed / len(scored)) if scored else None,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "per_task": per_task,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="mock", help="comma list: solar,exaone,ax,baseline,mock")
    ap.add_argument("--limit", type=int, default=0, help="canary: first N items")
    ap.add_argument("--tier", choices=["A", "B", "all"], default="A")
    ap.add_argument("--layer", choices=["L1", "L2", "all"], default="all")
    ap.add_argument("--tasks", default="", help="comma filter e.g. t3,g4")
    ap.add_argument(
        "--only-tag",
        default="",
        help="run only items carrying this manifest tag (e.g. d9_ext) — "
        "D9 extension arms re-measure just the new frozen items",
    )
    ap.add_argument(
        "--final-verify",
        action="store_true",
        help="unlock large/frontier models (dataset-confirmed only)",
    )
    ap.add_argument(
        "--strict-hash", action="store_true", help="hard-fail on any input_sha256 drift"
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="offline plumbing gate: assert invariants + write output",
    )
    # --- B1 glue-ladder axis (analysis/b1-prereg.md) --------------------------
    ap.add_argument(
        "--glue-level",
        type=int,
        choices=sorted(gl.LEVEL_NAMES),
        default=None,
        help="B1 cumulative glue ladder: 0 bare / 1 +snap / 2 +repair / "
        "3 +k=5 SC / 4 +probe-0+fallback. Omit => no ladder at all "
        "(byte-identical to the pre-B1 harness).",
    )
    ap.add_argument(
        "--pg-database",
        default=None,
        help="rewrite the database name of BOTH ORTHUS_PG_DSN and "
        "ORTHUS_PG_DSN_READONLY (b1-prereg §6: e.g. orthus_company).",
    )
    ap.add_argument("--glue-k", type=int, default=5, help="L3 self-consistency k (default 5)")
    ap.add_argument(
        "--glue-temperature",
        type=float,
        default=0.7,
        help="L3 sample temperature for samples 1..k-1 (sample 0 stays t0)",
    )
    ap.add_argument(
        "--glue-fallback",
        default=",".join(gl.DEFAULT_FALLBACK_ORDER),
        help="L4 null-fallback worker order (the assigned model is skipped)",
    )
    args = ap.parse_args()

    # DSN override must happen before `orthus.settings` is first read. Both DSNs
    # move together — a RW-only swap is what silently scored 18 t3 items against
    # an empty DB in Phase 5 (b1-prereg §6).
    if args.pg_database:
        for env, dsn in gl.override_pg_database(args.pg_database).items():
            print(f"[dsn] {env} -> {gl.mask_dsn(dsn)}")
    if args.glue_level is not None or args.pg_database:
        dsn_info = gl.check_pg_dsn_pair(strict=True)
        print(
            f"[dsn] rw={dsn_info['pg_dsn']} ro={dsn_info['pg_dsn_readonly']} "
            f"(same database: {dsn_info['match']})"
        )

    slugs = [s.strip() for s in args.models.split(",") if s.strip()]
    enforce_final_verify_gate(slugs, args.final_verify)

    paths = rl.default_manifest_paths(args.tier, args.layer)
    items, pending, mism = rl.load_manifest_files(paths, strict_hash=args.strict_hash)
    # tier + task filters
    if args.tier != "all":
        items = [it for it in items if it.tier == args.tier]
    if args.tasks:
        want = {t.strip() for t in args.tasks.split(",")}
        items = [it for it in items if it.task in want]
    if args.only_tag:
        items = [it for it in items if args.only_tag in (it.tags or [])]
    if args.limit:
        items = items[: args.limit]

    print(f"== E2E harness ==  tier={args.tier} layer={args.layer} models={slugs}")
    print(
        f"loaded {len(items)} items | {len(pending)} pending (no frozen input) skipped"
        + (f" | {len(mism)} hash-mismatch skipped" if mism else "")
    )
    if pending:
        print(f"  pending ids (sample): {pending[:6]}{' ...' if len(pending) > 6 else ''}")
    if mism:
        print(f"  hash-mismatch ids: {mism[:6]}{' ...' if len(mism) > 6 else ''}")

    db_ok = rl.db_reachable()
    truncate_ok = rl.truncate_guard_ok()
    if not db_ok:
        print(
            "\n⚠️  orthus_test DB NOT reachable — L2 items cannot dispatch. "
            "Delivering code + static validation only. Set ORTHUS_PG_DSN to a "
            "migrated *test* DB (`make up && make migrate` on orthus_test)."
        )
    elif not truncate_ok:
        # Defect-1 fix: ORTHUS_PG_DSN resolves to a reachable but non-test/staging
        # DB (e.g. a sourced company node.env) — never TRUNCATE it. L1 items
        # (including read-only t3 against orthus_company) still dispatch below;
        # every L2 item self-skips inside `dispatch_l2` for the same reason.
        print(
            f"\n⚠️  {rl.LIVE_DB_WARNING} "
            f"(ORTHUS_PG_DSN database={rl.dsn_database_name(rl.resolved_pg_dsn())!r})"
        )
    else:
        rl.truncate_all_tables()  # clean slate; fallback baseline = 0

    fb_before = rl.count_fallback_spans() if db_ok else 0

    try:
        pool = build_e2e_pool(slugs)
    except Exception as e:  # noqa: BLE001
        print(f"\npool build failed: {type(e).__name__}: {e}")
        print("(domestic slugs need keys.json/FUGU_KEYS; use --models mock offline.)")
        raise SystemExit(1) from e

    if args.glue_level is not None:
        global _ACTIVE_LADDER
        _fb_pool: dict[str, Any] = {}

        def _fallback_chat(slug: str):
            if slug not in _fb_pool:
                try:
                    _fb_pool[slug] = build_e2e_pool([slug]).get(slug)
                except Exception as e:  # noqa: BLE001 — a missing key must not abort the run
                    print(f"  [glue] fallback worker {slug!r} unavailable: {type(e).__name__}")
                    _fb_pool[slug] = None
            return _fb_pool[slug]

        _ACTIVE_LADDER = gl.GlueLadder(
            level=args.glue_level,
            k=args.glue_k,
            temperature=args.glue_temperature,
            fallback_order=tuple(s.strip() for s in args.glue_fallback.split(",") if s.strip()),
            fallback_chat=_fallback_chat,
        )
        print(
            f"\n== glue ladder ==  L{_ACTIVE_LADDER.level} ({_ACTIVE_LADDER.name}), "
            f"applies to {gl.LADDER_TASKS}; other tasks pass through unchanged"
        )

    summaries = []
    all_rows_by_model: dict[str, list[dict]] = {}
    for slug, chat in pool.items():
        rows = run_model(slug, chat, items)
        all_rows_by_model[slug] = rows
        out = write_raw(slug, rows)
        s = summarize(slug, rows)
        summaries.append(s)
        print(
            f"\n[{slug}] {s['status_counts']}  scored {s['scored']} "
            f"(pass {s['passed']})  p50 {s['latency_p50_ms']}ms p95 {s['latency_p95_ms']}ms"
        )
        print(f"  raw -> {out.relative_to(rl.REPO)}")
        stages = gl.aggregate_stages(rows) if _ACTIVE_LADDER is not None else None
        if stages:
            s["glue_stages"] = stages
            for lv, d in stages["per_level"].items():
                print(
                    f"  glue L{lv} {d['name']:<15} acc {d['accuracy']:.4f} "
                    f"({d['correct']}/{d['n']})  fired {d['fired']}  "
                    f"rescued {d['rescued']}  broke {d['broke']}"
                )
            print(
                f"  glue calls/item {stages['calls_per_item']:.2f}  "
                f"fallback items {stages['n_fallback_items']}"
            )
        e3 = aggregate_e3(rows)
        if e3:
            print(
                f"  e3 aggregate: missed_recall={e3['missed_recall']} "
                f"(>=0.80 {e3['recall_pass']}) mis_split={e3['mis_split_rate']} "
                f"(<=0.05 {e3['mis_split_pass']})"
            )

    # --- pairwise model comparison (McNemar + bootstrap CI, >=2 models only) ---
    pairwise = pairwise_model_stats(all_rows_by_model)
    if pairwise:
        print("\n== pairwise model comparison ==")
        for p in pairwise:
            print(
                f"  {p['a']} vs {p['b']}: n_paired={p['n_paired']} "
                f"a_only={p['a_only_correct']} b_only={p['b_only_correct']} "
                f"p={p['mcnemar_p_value']:.4f} sig={p['significant_p05']} "
                f"bootstrap_diff_ci95={p['bootstrap_diff_ci95']}"
            )

    # --- invariants gate ---
    fb_after = rl.count_fallback_spans() if db_ok else 0
    fb_delta = fb_after - fb_before
    confident_zero_fails = [
        r["item"].id
        for rows in all_rows_by_model.values()
        for r in rows
        if r["score"].get("reason") == "confident_zero"
    ]
    scored_fails = [
        r["item"].id
        for rows in all_rows_by_model.values()
        for r in rows
        if r["score"]["status"] == "fail"
    ]

    # b1-prereg §6 invariant: `model.fallback == 0` is asserted EXCEPT at L4,
    # where the null-fallback stage is on by design — there it is counted and
    # reported instead of enforced.
    fallback_exempt = _ACTIVE_LADDER is not None and _ACTIVE_LADDER.level >= 4
    glue_fallback_items = _ACTIVE_LADDER.n_fallback_used if _ACTIVE_LADDER else 0

    print("\n== invariants ==")
    if fallback_exempt:
        print(
            f"  model.fallback spans (delta over run): {fb_delta}  "
            f"(EXEMPT at glue L4 — b1-prereg §6)"
        )
        print(f"  glue L4 null-fallback adopted on {glue_fallback_items} item(s)")
    else:
        print(
            f"  model.fallback spans (delta over run): {fb_delta}  "
            f"({'CLEAN' if fb_delta == 0 else 'VIOLATED'})"
        )
    print(f"  confident-zero violations: {len(confident_zero_fails)} {confident_zero_fails[:5]}")
    print(f"  model-independent scored FAILS: {len(scored_fails)} {scored_fails[:8]}")

    summary_doc = {
        "tier": args.tier,
        "layer": args.layer,
        "models": slugs,
        "n_items": len(items),
        "pending_skipped": len(pending),
        "hash_mismatch_skipped": len(mism),
        "db_reachable": db_ok,
        "invariants": {
            "model_fallback_delta": fb_delta,
            "model_fallback_zero_ok": fb_delta == 0,
            "confident_zero_violations": len(confident_zero_fails),
        },
        "scored_fails": scored_fails,
        "summaries": summaries,
        "pairwise_model_stats": pairwise,
        "smoke": args.smoke,
    }
    if _ACTIVE_LADDER is not None:
        # Keys added ONLY under the ladder, so a no-flag run's summary.json stays
        # byte-identical to the pre-B1 harness (verified by diff).
        summary_doc["invariants"]["model_fallback_exempt_l4"] = fallback_exempt
        summary_doc["invariants"]["glue_fallback_items"] = glue_fallback_items
        summary_doc["glue"] = {
            "level": _ACTIVE_LADDER.level,
            "level_name": _ACTIVE_LADDER.name,
            "k": _ACTIVE_LADDER.k,
            "temperature": _ACTIVE_LADDER.temperature,
            "fallback_order": list(_ACTIVE_LADDER.fallback_order),
            "ladder_tasks": list(gl.LADDER_TASKS),
            "no_glue_tasks_reason": gl.NO_GLUE_REASON,
            "notes": _ACTIVE_LADDER.notes,
            "pg_dsn": gl.check_pg_dsn_pair(strict=False),
        }
    rl.RAW_DIR.mkdir(parents=True, exist_ok=True)
    # 병렬 실행 안전: 여러 하네스가 동시에 돌 때 요약 파일이 서로를 덮지 않도록
    # env `ORTHUS_E2E_SUMMARY_NAME`로 파일명을 분리할 수 있다(미설정 시 기존 그대로 = byte-identical).
    _summary_name = os.environ.get("ORTHUS_E2E_SUMMARY_NAME", "e2e_summary.json")
    (rl.RAW_DIR / _summary_name).write_text(
        json.dumps(summary_doc, ensure_ascii=False, indent=2, default=str), "utf-8"
    )
    print(f"\nsummary -> {(rl.RAW_DIR / _summary_name).relative_to(rl.REPO)}")

    # exit code: invariants + no model-independent regression failure
    violated = (fb_delta != 0 and not fallback_exempt) or confident_zero_fails or scored_fails
    if violated:
        print("\nRESULT: FAIL (invariant or model-independent regression)")
        raise SystemExit(1)
    print("\nRESULT: PASS")


if __name__ == "__main__":
    main()
