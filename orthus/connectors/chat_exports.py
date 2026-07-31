"""Personal chat export connector.

Reads official ChatGPT/Claude export JSON or ZIP files from a local inbox and
normalizes each conversation into one InternalDocument. Only user/assistant
text is kept; system/tool/attachment payloads are ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orthus.audit.redact import redact_pii_text
from orthus.connectors.base import Connector
from orthus.connectors.local_files import parse_local_file_roots
from orthus.schemas.canonical import InternalDocument

CHAT_EXPORT_SUFFIXES = {".json", ".zip"}
DEFAULT_CHAT_EXPORT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_CHAT_EXPORT_MAX_MESSAGES = 300
DEFAULT_CHAT_EXPORT_MAX_MESSAGE_CHARS = 4000

_TEXT_ITEM_TYPES = {"text", "input_text", "output_text"}
_USER_ROLES = {"user", "human"}
_ASSISTANT_ROLES = {"assistant", "model"}
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
class ChatMessage:
    role: str
    text: str
    created_at: str | None = None


@dataclass(frozen=True)
class ChatConversation:
    key: str
    title: str
    messages: list[ChatMessage]
    source_member: str | None = None


class ChatExportsConnector(Connector):
    """Connector over node-local ChatGPT/Claude export files."""

    def __init__(
        self,
        roots: Iterable[Path | str],
        *,
        max_bytes: int = DEFAULT_CHAT_EXPORT_MAX_BYTES,
        max_messages: int = DEFAULT_CHAT_EXPORT_MAX_MESSAGES,
        max_message_chars: int = DEFAULT_CHAT_EXPORT_MAX_MESSAGE_CHARS,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        if max_message_chars <= 0:
            raise ValueError("max_message_chars must be positive")
        self.roots = tuple(Path(root).expanduser().resolve(strict=False) for root in roots)
        self.max_bytes = max_bytes
        self.max_messages = max_messages
        self.max_message_chars = max_message_chars

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        for root in self.roots:
            yield from self._iter_root(root, since)

    def _iter_root(self, root: Path, since: datetime | None) -> Iterator[InternalDocument]:
        if not root.exists():
            return
        if root.is_file():
            yield from self._iter_file(root, root, since)
            return
        if not root.is_dir():
            return

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".") and not (current / name).is_symlink()
            )
            for name in sorted(filenames):
                yield from self._iter_file(root, current / name, since)

    def _iter_file(
        self,
        root: Path,
        path: Path,
        since: datetime | None,
    ) -> Iterator[InternalDocument]:
        if _skip_file(path):
            return
        try:
            stat = path.stat()
        except OSError:
            return
        if stat.st_size > self.max_bytes:
            return

        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        if since is not None and mtime <= since:
            return

        try:
            resolved = path.resolve(strict=True)
            if root.is_dir():
                resolved.relative_to(root)
        except (OSError, ValueError):
            return

        rel = _relative_label(root, path)
        path_hash = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
        for index, conv in enumerate(self._parse_file(path)):
            if not conv.messages:
                continue
            key = conv.key or f"{rel}:{index}"
            external_hash = hashlib.sha256(f"{path_hash}:{key}".encode("utf-8")).hexdigest()
            yield InternalDocument(
                title=f"Chat export: {conv.title}",
                markdown=_conversation_markdown(rel, conv),
                source="chat_exports",
                source_external_id=f"chat-export:{external_hash}",
                source_last_edited_at=mtime,
                project="company",
            )

    def _parse_file(self, path: Path) -> Iterator[ChatConversation]:
        suffix = path.suffix.lower()
        if suffix == ".json":
            yield from _parse_json_file(path, self.max_messages, self.max_message_chars)
        elif suffix == ".zip":
            yield from _parse_zip_file(
                path, self.max_bytes, self.max_messages, self.max_message_chars
            )


def roots_from_settings(value: str) -> tuple[Path, ...]:
    return parse_local_file_roots(value)


def _parse_json_file(path: Path, max_messages: int, max_chars: int) -> Iterator[ChatConversation]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return
    yield from _conversations_from_obj(obj, max_messages, max_chars, source_member=None)


def _parse_zip_file(
    path: Path,
    max_bytes: int,
    max_messages: int,
    max_chars: int,
) -> Iterator[ChatConversation]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = [
                name
                for name in zf.namelist()
                if name.lower().endswith(".json")
                and ("conversation" in name.lower() or "chat" in name.lower())
            ]
            for name in sorted(names):
                info = zf.getinfo(name)
                if info.file_size > max_bytes:
                    continue
                try:
                    obj = json.loads(zf.read(name).decode("utf-8", errors="ignore"))
                except (OSError, json.JSONDecodeError, KeyError):
                    continue
                yield from _conversations_from_obj(
                    obj,
                    max_messages,
                    max_chars,
                    source_member=name,
                )
    except (OSError, zipfile.BadZipFile):
        return


def _conversations_from_obj(
    obj: Any,
    max_messages: int,
    max_chars: int,
    *,
    source_member: str | None,
) -> Iterator[ChatConversation]:
    if isinstance(obj, list):
        for index, item in enumerate(obj):
            if isinstance(item, dict):
                yield from _conversation_candidates(
                    item,
                    max_messages,
                    max_chars,
                    source_member=source_member,
                    fallback_key=f"item-{index}",
                )
        return

    if not isinstance(obj, dict):
        return

    for key in ("conversations", "chats"):
        if isinstance(obj.get(key), list):
            for index, item in enumerate(obj[key]):
                if isinstance(item, dict):
                    yield from _conversation_candidates(
                        item,
                        max_messages,
                        max_chars,
                        source_member=source_member,
                        fallback_key=f"{key}-{index}",
                    )
            return

    yield from _conversation_candidates(
        obj,
        max_messages,
        max_chars,
        source_member=source_member,
        fallback_key="conversation",
    )


def _conversation_candidates(
    obj: dict[str, Any],
    max_messages: int,
    max_chars: int,
    *,
    source_member: str | None,
    fallback_key: str,
) -> Iterator[ChatConversation]:
    messages: list[ChatMessage] = []
    if isinstance(obj.get("mapping"), dict):
        messages.extend(_messages_from_chatgpt_mapping(obj["mapping"], max_chars))
    if isinstance(obj.get("chat_messages"), list):
        messages.extend(_messages_from_list(obj["chat_messages"], max_chars))
    if isinstance(obj.get("messages"), list):
        messages.extend(_messages_from_list(obj["messages"], max_chars))

    if not messages:
        single = _message_from_dict(obj, max_chars)
        if single is not None:
            messages.append(single)

    messages = messages[:max_messages]
    if not messages:
        return

    key = str(
        obj.get("id")
        or obj.get("uuid")
        or obj.get("conversation_id")
        or obj.get("name")
        or obj.get("title")
        or fallback_key
    )
    title = str(obj.get("title") or obj.get("name") or key or "Untitled chat").strip()
    yield ChatConversation(
        key=key,
        title=title[:160] or "Untitled chat",
        messages=messages,
        source_member=source_member,
    )


def _messages_from_chatgpt_mapping(
    mapping: dict[str, Any], max_chars: int
) -> Iterator[ChatMessage]:
    for node in mapping.values():
        if not isinstance(node, dict) or not isinstance(node.get("message"), dict):
            continue
        message = _message_from_dict({"message": node["message"]}, max_chars)
        if message is not None:
            yield message


def _messages_from_list(items: list[Any], max_chars: int) -> Iterator[ChatMessage]:
    for item in items:
        if isinstance(item, dict):
            message = _message_from_dict(item, max_chars)
            if message is not None:
                yield message


def _message_from_dict(obj: dict[str, Any], max_chars: int) -> ChatMessage | None:
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

    text = _redact_text(_text_from_content(content)).strip()
    if len(text) < 2:
        return None
    return ChatMessage(role=role, text=text[:max_chars], created_at=_parse_time(created))


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


def _redact_text(text: str) -> str:
    out = text.replace("\x00", " ")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return redact_pii_text(re.sub(r"\s+", " ", out).strip())


def _conversation_markdown(rel: str, conv: ChatConversation) -> str:
    lines = [
        f"# {conv.title}",
        "",
        "Source: chat_exports",
        f"Path: {rel}",
        f"Messages: {len(conv.messages)}",
    ]
    if conv.source_member:
        lines.append(f"Archive member: {conv.source_member}")
    lines.extend(["", "## Transcript", ""])
    for message in conv.messages:
        suffix = f" ({message.created_at})" if message.created_at else ""
        lines.append(f"### {message.role}{suffix}")
        lines.append("")
        lines.append(message.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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


def _skip_file(path: Path) -> bool:
    return (
        path.name.startswith(".")
        or path.is_symlink()
        or path.suffix.lower() not in CHAT_EXPORT_SUFFIXES
    )
