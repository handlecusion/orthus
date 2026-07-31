"""Default local connector paths.

These are product defaults, not advanced user options. The web UI keeps local
connectors mostly one-click while env/web config can still override when needed.
"""

from __future__ import annotations

from pathlib import Path

from orthus.settings import Settings


def connector_roots_setting(settings: Settings, connector_slug: str) -> str:
    slug = connector_slug.strip().lower()
    configured = _configured_roots(settings, slug)
    if configured:
        return configured
    return default_roots(settings, slug)


def default_roots(settings: Settings, connector_slug: str) -> str:
    slug = connector_slug.strip().lower()
    node_root = settings.resolved_node_root()
    home = Path.home()
    if slug == "local_files":
        return str(node_root / "imports" / "local-files")
    if slug == "codex_sessions":
        return ",".join(
            [
                str(home / ".codex" / "sessions"),
                str(home / ".codex" / "history.jsonl"),
                str(node_root / "imports" / "ai-sessions" / "codex"),
            ]
        )
    if slug == "claude_sessions":
        return ",".join(
            [
                str(home / ".claude" / "projects"),
                str(home / ".claude" / "transcripts"),
                str(home / ".claude" / "history.jsonl"),
                str(node_root / "imports" / "ai-sessions" / "claude"),
            ]
        )
    if slug == "chat_exports":
        return str(node_root / "imports" / "chat-exports")
    if slug == "email_exports":
        return str(node_root / "imports" / "email-exports")
    return ""


def _configured_roots(settings: Settings, connector_slug: str) -> str:
    if connector_slug == "local_files":
        return settings.conn_local_files_roots.strip()
    if connector_slug == "codex_sessions":
        return settings.conn_codex_sessions_roots.strip()
    if connector_slug == "claude_sessions":
        return settings.conn_claude_sessions_roots.strip()
    if connector_slug == "chat_exports":
        return settings.conn_chat_exports_roots.strip()
    if connector_slug == "email_exports":
        return settings.conn_email_exports_roots.strip()
    return ""
