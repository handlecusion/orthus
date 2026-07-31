"""orthus-mcp: a single stdio MCP server for the owner's local Claude Code/Codex.

Central knowledge tools go over HTTPS with a knowledge-scoped collector token;
machine-local tools call the collector daemon modules in-process. See
docs/p8-mcp-surface.md and docs/p8-central-consolidation.md §2.

The `mcp` SDK is an optional extra (`uv sync --extra mcp`); importing this
package must not require it, so server symbols are resolved lazily.
"""

from typing import Any

__all__ = ["SERVER_NAME", "build_server", "main"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from orthus.mcp import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
