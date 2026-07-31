"""P10.7 Part B: should_route_app_chat decision matrix (spec §5).

An explicitly personal-scope chat turn from an operator with an enrolled daemon
routes to the resident daemon; every other combination (flag off, wrong scope,
non-operator, no daemon, agent_task off, personal node) falls through to the
inline company orchestrator. The enrolled-daemon lookup is monkeypatched — no DB.
"""

from __future__ import annotations

import uuid

from orthus.agentwork import app_chat_routing
from orthus.settings import Settings


def _settings(**over) -> Settings:
    base = dict(
        app_chat_gateway_routing_enabled=True,
        agent_task_enabled=True,
        node_kind="company",
    )
    base.update(over)
    return Settings(**base)


def _daemon_present(monkeypatch, present: bool = True):
    monkeypatch.setattr(
        app_chat_routing,
        "resolve_enrolled_daemon",
        lambda user_id, settings=None: ("company", "") if present else None,
    )


def test_routes_when_all_conditions_met(monkeypatch):
    _daemon_present(monkeypatch)
    assert (
        app_chat_routing.should_route_app_chat(
            user_id=uuid.uuid4(), actor_role="owner", scope="personal", settings=_settings()
        )
        is True
    )


def test_admin_also_routes(monkeypatch):
    _daemon_present(monkeypatch)
    assert app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(), actor_role="admin", scope="personal", settings=_settings()
    )


def test_flag_off_never_routes(monkeypatch):
    _daemon_present(monkeypatch)
    assert (
        app_chat_routing.should_route_app_chat(
            user_id=uuid.uuid4(),
            actor_role="owner",
            scope="personal",
            settings=_settings(app_chat_gateway_routing_enabled=False),
        )
        is False
    )


def test_company_scope_never_routes(monkeypatch):
    """company-scope pure search stays on the inline path (spec §5)."""
    _daemon_present(monkeypatch)
    assert not app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(), actor_role="owner", scope="company", settings=_settings()
    )


def test_all_scope_never_routes(monkeypatch):
    """the merged scope=all view stays on the company orchestrator (spec §5)."""
    _daemon_present(monkeypatch)
    assert not app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(), actor_role="owner", scope="all", settings=_settings()
    )


def test_non_operator_never_routes(monkeypatch):
    _daemon_present(monkeypatch)
    assert not app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(), actor_role="member", scope="personal", settings=_settings()
    )


def test_scheduler_role_does_not_route(monkeypatch):
    """scheduler is an operator role for delegation but never a live chat actor."""
    _daemon_present(monkeypatch)
    assert not app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(), actor_role="scheduler", scope="personal", settings=_settings()
    )


def test_no_enrolled_daemon_never_routes(monkeypatch):
    _daemon_present(monkeypatch, present=False)
    assert not app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(), actor_role="owner", scope="personal", settings=_settings()
    )


def test_agent_task_disabled_never_routes(monkeypatch):
    _daemon_present(monkeypatch)
    assert not app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(),
        actor_role="owner",
        scope="personal",
        settings=_settings(agent_task_enabled=False),
    )


def test_personal_node_never_routes(monkeypatch):
    """agent_task delegation is company-node only."""
    _daemon_present(monkeypatch)
    assert not app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(),
        actor_role="owner",
        scope="personal",
        settings=_settings(node_kind="personal"),
    )


def test_enrolled_daemon_lookup_short_circuited_when_cheap_gate_fails(monkeypatch):
    """The DB lookup must not run when a cheap gate already fails (flag off)."""
    called = {"n": 0}

    def _spy(user_id, settings=None):
        called["n"] += 1
        return ("company", "")

    monkeypatch.setattr(app_chat_routing, "resolve_enrolled_daemon", _spy)
    app_chat_routing.should_route_app_chat(
        user_id=uuid.uuid4(),
        actor_role="owner",
        scope="personal",
        settings=_settings(app_chat_gateway_routing_enabled=False),
    )
    assert called["n"] == 0
