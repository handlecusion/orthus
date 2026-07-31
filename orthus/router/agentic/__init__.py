"""Inline agentic /ask (docs/inline-agentic-ask.md).

When `ORTHUS_AGENTIC_ASK_ENABLED` is on AND the Solar key is
configured, `/ask` runs an in-process LLM tool-calling loop instead of the
fixed wiki/structured/graph router ladder. The loop's three tools wrap the
SAME backends the legacy branches used (wiki grounding, the sqlglot-gated
structured query, the team calendar), so the validation gate, redaction, and
compiled-wiki grounding are all preserved — the model only SELECTS a tool, it
never executes SQL or bypasses a guard (AGENTS.md 설계원칙 1·4).
"""

from __future__ import annotations

from orthus.router.agentic.loop import run_agentic_answer

__all__ = ["run_agentic_answer"]
