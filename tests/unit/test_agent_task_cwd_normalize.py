"""Central agent_task cwd intake normalization (the `@` folder-picker affordance).

A user may type the FE `@` folder-picker prefix into the cwd field ("@~/Code").
The central intake strips it before it enters the work-item payload + dispatched
collector command, so the daemon never chdir's into a literal "@~/Code" that does
not exist. The daemon strips defensively too (test_agent_task_runner).
"""

from __future__ import annotations

from orthus.agentwork.service import _normalize_agent_task_cwd


def test_strips_leading_at_affordance():
    assert _normalize_agent_task_cwd("@~/Code") == "~/Code"


def test_strips_at_with_trailing_space():
    assert _normalize_agent_task_cwd("@ ~/Code") == "~/Code"


def test_plain_path_untouched():
    assert _normalize_agent_task_cwd("<local>") == "<local>"
    assert _normalize_agent_task_cwd("~/Code") == "~/Code"


def test_empty_and_none_become_none():
    assert _normalize_agent_task_cwd(None) is None
    assert _normalize_agent_task_cwd("") is None
    assert _normalize_agent_task_cwd("   ") is None
    # A lone "@" (no path) is not a usable cwd -> fall back to the daemon default.
    assert _normalize_agent_task_cwd("@") is None
