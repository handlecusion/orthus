"""Personal AI session log connectors.

Codex and Claude Code session files can contain tool outputs, full file reads,
and shell logs. This connector keeps only user/assistant text messages, skips
tool payloads, and stores path hashes instead of absolute local paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orthus.audit.redact import redact_pii_text
from orthus.connectors.base import Connector
from orthus.connectors.local_files import parse_local_file_roots
from orthus.schemas.canonical import InternalDocument

AI_SESSION_SUFFIXES = {".jsonl", ".json"}
DEFAULT_AI_SESSION_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_AI_SESSION_MAX_FILES = 200
DEFAULT_AI_SESSION_MAX_MESSAGES = 200
DEFAULT_AI_SESSION_MAX_MESSAGE_CHARS = 4000

_TEXT_ITEM_TYPES = {"text", "input_text", "output_text"}
_USER_ROLES = {"user", "human"}
_ASSISTANT_ROLES = {"assistant", "model"}
_SKIP_DIR_NAMES = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", "node_modules"}
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S,
    ),
]


@dataclass(frozen=True)
class SessionMessage:
    role: str
    text: str
    created_at: str | None = None


class AiSessionsConnector(Connector):
    """Normalize local Codex/Claude session logs into per-file documents."""

    def __init__(
        self,
        *,
        source: str,
        label: str,
        roots: Iterable[Path | str],
        max_bytes: int = DEFAULT_AI_SESSION_MAX_BYTES,
        max_files: int = DEFAULT_AI_SESSION_MAX_FILES,
        max_messages: int = DEFAULT_AI_SESSION_MAX_MESSAGES,
        max_message_chars: int = DEFAULT_AI_SESSION_MAX_MESSAGE_CHARS,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        if max_message_chars <= 0:
            raise ValueError("max_message_chars must be positive")
        self.source = source
        self.label = label
        self.roots = tuple(Path(root).expanduser().resolve(strict=False) for root in roots)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.max_messages = max_messages
        self.max_message_chars = max_message_chars

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        yielded = 0
        for root in self.roots:
            for doc in self._iter_root(root, since):
                yield doc
                yielded += 1
                if yielded >= self.max_files:
                    return

    def _iter_root(self, root: Path, since: datetime | None) -> Iterator[InternalDocument]:
        if not root.exists():
            return
        if root.is_file():
            doc = self._read_document(root, root, since)
            if doc is not None:
                yield doc
            return
        if not root.is_dir():
            return

        candidates: list[tuple[datetime, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = sorted(
                name for name in dirnames if not _skip_dir(current / name, root=root)
            )
            for name in sorted(filenames):
                path = current / name
                mtime = _candidate_mtime(path, since, self.max_bytes)
                if mtime is None:
                    continue
                candidates.append((mtime, path))

        for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True):
            doc = self._read_document(root, path, since)
            if doc is not None:
                yield doc

    def _read_document(
        self,
        root: Path,
        path: Path,
        since: datetime | None,
    ) -> InternalDocument | None:
        if _skip_file(path):
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > self.max_bytes:
            return None
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        if since is not None and mtime <= since:
            return None

        try:
            resolved = path.resolve(strict=True)
            if root.is_dir():
                resolved.relative_to(root)
        except (OSError, ValueError):
            return None

        messages = list(self._parse_messages(path))[: self.max_messages]
        if not messages:
            return None

        rel = _relative_label(root, path)
        external_id = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        return InternalDocument(
            title=f"{self.label}: {rel}",
            markdown=self._to_markdown(rel, messages),
            source=self.source,
            source_external_id=f"path-sha256:{external_id}",
            source_last_edited_at=mtime,
            project="company",
        )

    def _parse_messages(self, path: Path) -> Iterator[SessionMessage]:
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            yield from _parse_jsonl(path, self.max_message_chars)
        elif suffix == ".json":
            yield from _parse_json(path, self.max_message_chars)

    def _to_markdown(self, rel: str, messages: list[SessionMessage]) -> str:
        lines = [
            f"# {self.label} Session",
            "",
            f"Source: {self.source}",
            f"Path: {rel}",
            f"Messages: {len(messages)}",
            "",
            "## Transcript",
            "",
        ]
        for message in messages:
            suffix = f" ({message.created_at})" if message.created_at else ""
            lines.append(f"### {message.role}{suffix}")
            lines.append("")
            lines.append(message.text)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def roots_from_settings(value: str) -> tuple[Path, ...]:
    return parse_local_file_roots(value)


def _parse_jsonl(path: Path, max_chars: int) -> Iterator[SessionMessage]:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    message = _message_from_dict(obj, max_chars)
                    if message is not None:
                        yield message
    except OSError:
        return


def _parse_json(path: Path, max_chars: int) -> Iterator[SessionMessage]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return
    yield from _messages_from_obj(obj, max_chars)


def _messages_from_obj(obj: Any, max_chars: int) -> Iterator[SessionMessage]:
    if isinstance(obj, dict):
        if isinstance(obj.get("messages"), list):
            for item in obj["messages"]:
                if isinstance(item, dict):
                    message = _message_from_dict(item, max_chars)
                    if message is not None:
                        yield message
        if isinstance(obj.get("mapping"), dict):
            for node in obj["mapping"].values():
                if isinstance(node, dict) and isinstance(node.get("message"), dict):
                    message = _message_from_dict({"message": node["message"]}, max_chars)
                    if message is not None:
                        yield message
        message = _message_from_dict(obj, max_chars)
        if message is not None:
            yield message
    elif isinstance(obj, list):
        for item in obj:
            yield from _messages_from_obj(item, max_chars)


def _message_from_dict(obj: dict[str, Any], max_chars: int) -> SessionMessage | None:
    message = obj.get("message")
    role = obj.get("role") or obj.get("sender") or obj.get("author_role")
    content: Any
    created: Any

    if isinstance(message, dict):
        author = message.get("author")
        author_role = author.get("role") if isinstance(author, dict) else None
        role = role or message.get("role") or author_role
        content = message.get("content")
        created = (
            message.get("create_time")
            or message.get("created_at")
            or obj.get("timestamp")
            or obj.get("created_at")
        )
    else:
        content = obj.get("content") or obj.get("text") or obj.get("prompt")
        created = obj.get("timestamp") or obj.get("created_at") or obj.get("create_time")

    role = _normalize_role(role or obj.get("type"))
    if role is None:
        return None

    text = _redact_session_text(_text_from_content(content)).strip()
    if len(text) < 2:
        return None
    return SessionMessage(role=role, text=text[:max_chars], created_at=_parse_time(created))


def _normalize_role(value: Any) -> str | None:
    raw = str(value or "").lower()
    if raw in _USER_ROLES:
        return "user"
    if raw in _ASSISTANT_ROLES:
        return "assistant"
    return None


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").lower()
                if item_type and item_type not in _TEXT_ITEM_TYPES:
                    continue
                parts.append(_text_from_content(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        item_type = str(content.get("type") or "").lower()
        if item_type and item_type not in _TEXT_ITEM_TYPES:
            return ""
        if isinstance(content.get("parts"), list):
            return "\n".join(str(part) for part in content["parts"] if isinstance(part, str))
        for key in ("text", "content", "value"):
            value = content.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return _text_from_content(value)
    return ""


def _redact_session_text(text: str) -> str:
    out = text.replace("\x00", " ")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return redact_pii_text(re.sub(r"\s+", " ", out).strip())


def _parse_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            if value > 10_000_000_000:
                value = value / 1000
            return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        return value[:40]
    return None


def _relative_label(root: Path, path: Path) -> str:
    if root.is_file():
        return path.name
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _skip_dir(path: Path, *, root: Path) -> bool:
    if path == root:
        return False
    return path.name.startswith(".") or path.name in _SKIP_DIR_NAMES or path.is_symlink()


def _skip_file(path: Path) -> bool:
    return (
        path.name.startswith(".")
        or path.is_symlink()
        or path.suffix.lower() not in AI_SESSION_SUFFIXES
    )


def _candidate_mtime(path: Path, since: datetime | None, max_bytes: int) -> datetime | None:
    if _skip_file(path):
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size > max_bytes:
        return None
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    if since is not None and mtime <= since:
        return None
    return mtime
