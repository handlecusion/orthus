"""P10.7 Part B: route a personal app-chat turn to the caller's own enrolled
collector daemon (the resident agent runtime) instead of running the inline
company orchestrator.

Fail-closed. Gated on ``ORTHUS_APP_CHAT_GATEWAY_ROUTING_ENABLED`` (default off):
when off, ``orchestrate_chat_route`` is byte-identical to pre-P10.7. When on,
ONLY an explicitly *personal-scope* turn (P8 owner-scope merged view) from an
operator who has an enrolled run-scope daemon routes to the daemon. Company-scope
pure search and the ``scope=all`` merged company orchestrator path are untouched
(spec §5 — "company 오케스트레이터 경로는 불변").

The routing itself is a self-delegation: the turn becomes an ``agent_task`` work
item assigned to the caller's own daemon (reusing the deterministic policy gate +
command queue + WS push), and the daemon answers on its resident engine (Part A),
streaming ``agent_output`` frames back over the existing per-work SSE endpoint.
"""

from __future__ import annotations

from uuid import UUID

from orthus.agentwork.delegation import resolve_enrolled_daemon
from orthus.settings import Settings, get_settings

# Live chat actors that may drive a daemon. Mirrors service._AGENT_TASK_OPERATOR_ROLES
# minus "scheduler" (a scheduler is never the actor of an interactive chat turn).
_APP_CHAT_ROUTE_ROLES = frozenset({"owner", "admin"})


def app_chat_routing_enabled(settings: Settings | None = None) -> bool:
    """The central routing flag (fail-closed, default off)."""
    s = settings or get_settings()
    return bool(s.app_chat_gateway_routing_enabled)


def should_route_app_chat(
    *,
    user_id: UUID,
    actor_role: str | None,
    scope: str,
    settings: Settings | None = None,
) -> bool:
    """True when this chat turn should run on the caller's resident daemon.

    All of the following are required (any miss → False → inline orchestrator):

    - the routing flag is on;
    - ``agent_task`` delegation is enabled on this node (the same gate the
      dispatch itself re-checks);
    - this is the company node (delegation runs company-node only);
    - the turn is *explicitly personal-scope* — NOT ``company`` and NOT the
      merged ``all`` view, so company-scope search + the merged company
      orchestrator stay unchanged;
    - the caller is an operator (owner/admin);
    - the caller has an enrolled run-scope daemon on this node.

    The enrolled-daemon check is last (a DB read) so the cheap flag/scope/role
    gates short-circuit first.
    """
    s = settings or get_settings()
    if not app_chat_routing_enabled(s):
        return False
    if not s.agent_task_enabled:
        return False
    if s.node_kind != "company":
        return False
    if scope != "personal":
        return False
    if (actor_role or "").strip().lower() not in _APP_CHAT_ROUTE_ROLES:
        return False
    return resolve_enrolled_daemon(user_id, settings=s) is not None
