"""Task-aware model assignment (docs/model-orchestration.md) — Solar-only build.

The load-bearing test is the first one: with the flag off, every task must resolve
to exactly the model the code resolved to before this feature existed. Everything
else is upside; that one is the promise that the default behaviour is unchanged.
"""

from __future__ import annotations

import pytest

from orthus.models.adapters.mock import MockChat
from orthus.models.orchestration import (
    ASSIGNMENTS,
    TASK_CLAIM_HEADLINE,
    TASK_DECOMPOSE,
    TASK_DELEGATION_EXTRACT,
    TASK_DISTILL,
    TASK_EMAIL_DRAFT,
    TASK_GAP_SUGGEST,
    TASK_GRAPH_BIND,
    TASK_INTENT,
    TASK_ROUTING,
    TASK_STRUCTURED,
    TASK_SYNTHESIZE,
    TASK_WIKI_QA,
    FallbackChat,
    get_chat_model_for,
)
from orthus.settings import get_settings

ALL_TASKS = [
    TASK_STRUCTURED,
    TASK_ROUTING,
    TASK_WIKI_QA,
    TASK_INTENT,
    TASK_DECOMPOSE,
    TASK_SYNTHESIZE,
    TASK_GRAPH_BIND,
    TASK_DELEGATION_EXTRACT,
    TASK_EMAIL_DRAFT,
    TASK_GAP_SUGGEST,
    TASK_CLAIM_HEADLINE,
    TASK_DISTILL,
]


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch):
    """Pin the config these tests exercise.

    `Settings` also reads the local `.env`, which on a developer machine carries
    a real Solar key and `ORTHUS_LLM=solar`. Without this fixture the suite would
    quietly assert against whatever that file happens to contain — passing in CI
    and failing locally, or worse, the reverse. Each test states its own config.
    """
    monkeypatch.setenv("ORTHUS_LLM", "mock")
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "false")
    monkeypatch.setenv("ORTHUS_LLM_SOLAR_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("task", ALL_TASKS)
def test_flag_off_keeps_default_model(monkeypatch, task):
    """Flag off = default behaviour. No task may reach a worker."""
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "false")
    # Credentials present, so only the flag can be what holds the assignment back.
    monkeypatch.setenv("ORTHUS_LLM_SOLAR_API_KEY", "k")

    model = get_chat_model_for(task)

    assert not isinstance(model, FallbackChat)
    assert isinstance(model, MockChat)  # the default slot in tests


@pytest.mark.parametrize("task", ALL_TASKS)
def test_unconfigured_worker_keeps_default_model(monkeypatch, task):
    """Flag on but no API key = fail-closed to the default, never a broken worker."""
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "true")  # key empty via fixture

    model = get_chat_model_for(task)

    assert isinstance(model, MockChat)


def test_assigned_task_routes_to_worker(monkeypatch):
    monkeypatch.setenv("ORTHUS_LLM", "solar")  # mock pins the slot; see test below
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "true")
    monkeypatch.setenv("ORTHUS_LLM_SOLAR_API_KEY", "k")

    model = get_chat_model_for(TASK_STRUCTURED)

    assert isinstance(model, FallbackChat)
    assert model.model_id == "solar-pro"


@pytest.mark.parametrize("task", ALL_TASKS)
def test_mock_slot_never_routes_to_a_real_worker(monkeypatch, task):
    """`ORTHUS_LLM=mock` means deterministic and offline — that is how the suite and CI
    pin the chat slot. If orchestration could route past it, a developer with real keys
    in their local .env would silently make the tests call the Solar API over the
    network, and assertions would drift with whatever the model answered. (This is
    not hypothetical: it is exactly what happened before this guard existed.)"""
    monkeypatch.setenv("ORTHUS_LLM", "mock")
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "true")
    monkeypatch.setenv("ORTHUS_LLM_SOLAR_API_KEY", "real-key")

    model = get_chat_model_for(task)

    assert isinstance(model, MockChat)
    assert not isinstance(model, FallbackChat)


def test_distill_is_assigned_to_solar(monkeypatch):
    """`distill` is on the table — an explicit, measured decision (T14, docs §13).

    The apparent coverage trade that once held it back (Solar 4.8 claims/doc vs 7.1) was
    an artefact of the prompt's claim cap, not the model: `_SYSTEM` capped at 8 with no
    lower bound. With the cap lifted, Solar distills at 8.4 claims/doc and 100% precision.

    The assignment is only sound WITH the lifted prompt cap — see
    tests/unit/test_wiki_distill.py, which pins the prompt cap and `_MAX_CLAIMS` together.
    """
    monkeypatch.setenv("ORTHUS_LLM", "solar")  # not mock — so an assignment can route
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "true")
    monkeypatch.setenv("ORTHUS_LLM_SOLAR_API_KEY", "k")

    assert ASSIGNMENTS[TASK_DISTILL] == "solar"

    model = get_chat_model_for(TASK_DISTILL)
    assert isinstance(model, FallbackChat)
    assert model.model_id == "solar-pro"


def test_every_assignment_is_solar():
    """Solar-only build: the whole table resolves to the single vendor. This pins the
    exact table so a future edit shows up as an intentional diff, not a silent drift.
    The per-task seam stays because each slot was measured separately and can be
    re-measured separately (experiments/fugu-ko/RESULTS.md)."""
    assert set(ASSIGNMENTS.values()) == {"solar"}
    assert ASSIGNMENTS == {
        TASK_STRUCTURED: "solar",
        TASK_ROUTING: "solar",
        TASK_WIKI_QA: "solar",
        TASK_INTENT: "solar",
        TASK_DECOMPOSE: "solar",
        TASK_SYNTHESIZE: "solar",
        TASK_GRAPH_BIND: "solar",
        TASK_DELEGATION_EXTRACT: "solar",
        TASK_EMAIL_DRAFT: "solar",
        TASK_GAP_SUGGEST: "solar",
        TASK_CLAIM_HEADLINE: "solar",
        TASK_DISTILL: "solar",
        "followup_rewrite": "solar",
    }


def test_assigned_worker_gives_up_quickly(monkeypatch):
    """The adapter's default budget is six attempts with ~76s of backoff — right for
    bulk authoring, which has no fallback. An assigned worker *has* one, so it must
    bail fast; otherwise a sick worker stalls a live request for minutes before the
    fallback it could have used immediately."""
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "true")
    monkeypatch.setenv("ORTHUS_LLM_SOLAR_API_KEY", "k")
    get_settings.cache_clear()

    from orthus.models.adapters.openai_compat import _RETRIES
    from orthus.models.orchestration import _WORKER_RETRIES, _build_worker, _worker_specs

    worker = _build_worker(_worker_specs()["solar"])

    assert worker._retries == _WORKER_RETRIES
    assert _WORKER_RETRIES < _RETRIES


class _Boom:
    model_id = "boom"

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        raise TimeoutError("worker down")


def test_fallback_serves_the_request_when_the_worker_fails():
    """Availability beats quality: a dead worker must not take the request down."""
    fallback = MockChat()
    chat = FallbackChat(TASK_ROUTING, _Boom(), [fallback])

    answer = chat.complete("sys", "user")

    assert answer == fallback.complete("sys", "user")


def test_fallback_is_audited(monkeypatch):
    """A silent fallback would look like the assigned model is serving traffic when
    it is not. The audit span is how a failing assignment becomes visible."""
    seen: list[tuple[str, dict]] = []

    class _Span:
        def add_meta(self, **kw):
            seen.append(("meta", kw))

    import contextlib

    import orthus.models.orchestration as orch

    @contextlib.contextmanager
    def _fake_audit(node: str, **_):
        seen.append((node, {}))
        yield _Span()

    monkeypatch.setattr(orch, "audit", _fake_audit)

    FallbackChat(TASK_ROUTING, _Boom(), [MockChat()]).complete("sys", "user")

    assert seen[0][0] == "model.fallback"
    meta = seen[1][1]
    assert meta["task"] == TASK_ROUTING
    assert meta["assigned"] == "boom"
    assert meta["reason"] == "TimeoutError"


class _Named:
    """A worker that answers with its own name, so a test can see WHICH rung served."""

    def __init__(self, name: str):
        self.model_id = name

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self.model_id


def test_the_default_slot_backs_a_failing_worker(monkeypatch):
    """The worker runs on the short retry budget with the default slot right behind
    it. A dead worker degrades to the default slot — visibly (audited), never a 500."""
    monkeypatch.setenv("ORTHUS_MODEL_ORCHESTRATION_ENABLED", "true")
    monkeypatch.setenv("ORTHUS_LLM", "solar")
    monkeypatch.setenv("ORTHUS_LLM_SOLAR_API_KEY", "k")
    get_settings.cache_clear()

    import orthus.models.orchestration as orch

    monkeypatch.setattr(orch, "get_chat_model", lambda: _Named("default-slot"))
    monkeypatch.setattr(orch, "_build_worker", lambda spec: _Boom())  # worker dead

    answer = orch.get_chat_model_for(TASK_ROUTING).complete("sys", "user")

    assert answer == "default-slot"
