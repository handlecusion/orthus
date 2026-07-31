"""Personal email export connector.

Reads node-local `.eml`, `.mbox`, and simple JSON email exports. The connector
keeps message text only; attachments and local absolute paths are not stored in
document content.
"""

from __future__ import annotations

import hashlib
import json
import mailbox
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from orthus.audit.redact import redact_pii_text
from orthus.connectors.base import Connector
from orthus.connectors.local_files import parse_local_file_roots
from orthus.schemas.canonical import InternalDocument

EMAIL_EXPORT_SUFFIXES = {".eml", ".mbox", ".json"}
DEFAULT_EMAIL_EXPORT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_EMAIL_EXPORT_MAX_BODY_CHARS = 12000

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
]


@dataclass(frozen=True)
class EmailExportMessage:
    key: str
    subject: str
    sender: str
    recipients: str
    sent_at: str | None
    body: str


class EmailExportsConnector(Connector):
    """Connector over node-local email export files."""

    def __init__(
        self,
        roots: Iterable[Path | str],
        *,
        max_bytes: int = DEFAULT_EMAIL_EXPORT_MAX_BYTES,
        max_body_chars: int = DEFAULT_EMAIL_EXPORT_MAX_BODY_CHARS,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_body_chars <= 0:
            raise ValueError("max_body_chars must be positive")
        self.roots = tuple(Path(root).expanduser().resolve(strict=False) for root in roots)
        self.max_bytes = max_bytes
        self.max_body_chars = max_body_chars

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
        for index, item in enumerate(self._parse_file(path)):
            key = item.key or f"{rel}:{index}"
            external_hash = hashlib.sha256(f"{path_hash}:{key}:{index}".encode()).hexdigest()
            yield InternalDocument(
                title=f"Email: {item.subject}",
                markdown=_email_markdown(rel, item),
                source="email_exports",
                source_external_id=f"email-export:{external_hash}",
                source_last_edited_at=mtime,
                project="company",
            )

    def _parse_file(self, path: Path) -> Iterator[EmailExportMessage]:
        suffix = path.suffix.lower()
        if suffix == ".eml":
            yield from _parse_eml(path, self.max_body_chars)
        elif suffix == ".mbox":
            yield from _parse_mbox(path, self.max_body_chars)
        elif suffix == ".json":
            yield from _parse_json_file(path, self.max_body_chars)


def roots_from_settings(value: str) -> tuple[Path, ...]:
    return parse_local_file_roots(value)


def _parse_eml(path: Path, max_body_chars: int) -> Iterator[EmailExportMessage]:
    try:
        msg = message_from_bytes(path.read_bytes(), policy=policy.default)
    except (OSError, ValueError):
        return
    item = _message_from_email(msg, max_body_chars)
    if item is not None:
        yield item


def _parse_mbox(path: Path, max_body_chars: int) -> Iterator[EmailExportMessage]:
    try:
        box = mailbox.mbox(path)
    except OSError:
        return
    try:
        for msg in box:
            item = _message_from_email(msg, max_body_chars)
            if item is not None:
                yield item
    finally:
        box.close()


def _parse_json_file(path: Path, max_body_chars: int) -> Iterator[EmailExportMessage]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return
    yield from _messages_from_json_obj(obj, max_body_chars)


def _messages_from_json_obj(obj: Any, max_body_chars: int) -> Iterator[EmailExportMessage]:
    if isinstance(obj, list):
        for index, item in enumerate(obj):
            if isinstance(item, dict):
                msg = _message_from_json_dict(item, max_body_chars, f"item-{index}")
                if msg is not None:
                    yield msg
        return

    if not isinstance(obj, dict):
        return

    for key in ("emails", "messages", "items"):
        if isinstance(obj.get(key), list):
            for index, item in enumerate(obj[key]):
                if isinstance(item, dict):
                    msg = _message_from_json_dict(item, max_body_chars, f"{key}-{index}")
                    if msg is not None:
                        yield msg
            return

    msg = _message_from_json_dict(obj, max_body_chars, "email")
    if msg is not None:
        yield msg


def _message_from_email(msg: Message, max_body_chars: int) -> EmailExportMessage | None:
    subject = _redact_text(_header(msg.get("subject")) or "(no subject)")[:180]
    sender = _redact_text(_header(msg.get("from")))
    recipients = _redact_text(_header(msg.get("to")))
    sent_at = _header(msg.get("date"))[:80] or None
    body = _redact_text(_body_from_email(msg))[:max_body_chars].strip()
    if len(body) < 2:
        return None
    key = _header(msg.get("message-id")) or f"{sent_at}:{subject}:{sender}"
    return EmailExportMessage(
        key=key,
        subject=subject,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        body=body,
    )


def _message_from_json_dict(
    obj: dict[str, Any], max_body_chars: int, fallback_key: str
) -> EmailExportMessage | None:
    subject = _redact_text(str(obj.get("subject") or obj.get("title") or "(no subject)"))[:180]
    sender = _redact_text(str(obj.get("from") or obj.get("sender") or ""))
    recipients_value = obj.get("to") or obj.get("recipients") or ""
    if isinstance(recipients_value, list):
        recipients_value = ", ".join(str(v) for v in recipients_value)
    recipients = _redact_text(str(recipients_value))
    sent_at = str(obj.get("date") or obj.get("sent_at") or obj.get("timestamp") or "")[:80] or None
    body_value = obj.get("body") or obj.get("text") or obj.get("plain") or obj.get("html") or ""
    body = _redact_text(_strip_html(str(body_value)))[:max_body_chars].strip()
    if len(body) < 2:
        return None
    key = str(obj.get("id") or obj.get("message_id") or obj.get("message-id") or fallback_key)
    return EmailExportMessage(
        key=key,
        subject=subject,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        body=body,
    )


def _body_from_email(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get_content_disposition() or "").lower()
            if disposition == "attachment" or part.get_filename():
                continue
            text = _payload_text(part)
            if not text:
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                html_parts.append(_strip_html(text))
    else:
        text = _payload_text(msg)
        if msg.get_content_type() == "text/html":
            html_parts.append(_strip_html(text))
        else:
            plain_parts.append(text)

    return "\n\n".join(plain_parts or html_parts)


def _payload_text(part: Message) -> str:
    try:
        if hasattr(part, "get_content"):
            content = part.get_content()
            return content if isinstance(content, str) else ""
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _header(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _redact_text(text: str) -> str:
    out = text.replace("\x00", " ")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return redact_pii_text(re.sub(r"\s+", " ", out).strip())


def _email_markdown(rel: str, item: EmailExportMessage) -> str:
    lines = [
        f"# {item.subject}",
        "",
        "Source: email_exports",
        f"Path: {rel}",
        f"From: {item.sender or '(unknown)'}",
        f"To: {item.recipients or '(unknown)'}",
    ]
    if item.sent_at:
        lines.append(f"Date: {item.sent_at}")
    lines.extend(["", "## Body", "", item.body])
    return "\n".join(lines).rstrip() + "\n"


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def _strip_html(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
    except Exception:
        return re.sub(r"<[^>]+>", " ", value)
    return " ".join(parser.parts) or value


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
        or path.suffix.lower() not in EMAIL_EXPORT_SUFFIXES
    )
