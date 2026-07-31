"""Google Workspace connector via the local `gws` CLI.

The connector never stores Google OAuth tokens. Authentication stays in the
user's `gws` config/keyring; orthus only shells out to read-only commands and
normalizes JSON output into InternalDocument rows.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from orthus.audit.redact import redact_pii_text
from orthus.connectors.base import Connector
from orthus.schemas.canonical import InternalDocument

DEFAULT_GWS_COMMAND = "gws"
DEFAULT_GWS_TIMEOUT_SECONDS = 60
DEFAULT_GWS_GMAIL_QUERY = "in:anywhere"
DEFAULT_GWS_GMAIL_MAX_MESSAGES = 50
DEFAULT_GWS_GMAIL_MAX_BODY_CHARS = 12000
DEFAULT_GWS_DRIVE_QUERY = "trashed=false"
DEFAULT_GWS_DRIVE_MAX_FILES = 50
DEFAULT_GWS_DRIVE_MAX_BYTES = 1024 * 1024
ALLOWED_GWS_COMMAND_NAMES = {"gws"}

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"ya29\.[A-Za-z0-9._-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]
_GOOGLE_MIME_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.drawing": "image/png",
}
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/x-ndjson",
    "application/javascript",
}


class GwsCliError(RuntimeError):
    """Raised when the local gws command cannot complete a read operation."""


class GwsCliUnavailable(GwsCliError):
    """Raised when the configured gws command is not installed."""


@dataclass(frozen=True)
class GwsCliRunner:
    command: str = DEFAULT_GWS_COMMAND
    timeout_seconds: int = DEFAULT_GWS_TIMEOUT_SECONDS

    def run_json(self, args: Sequence[str]) -> Any:
        output = self.run_text(_with_json_format(args))
        try:
            return json.loads(output or "{}")
        except json.JSONDecodeError as exc:
            raise GwsCliError("gws returned non-json output") from exc

    def run_to_file(self, args: Sequence[str], output_path: Path) -> str:
        return self.run_text([*args, "-o", str(output_path)])

    def run_text(self, args: Sequence[str]) -> str:
        argv = [*_command_argv(self.command), *args]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GwsCliUnavailable(gws_install_message(self.command)) from exc
        except subprocess.TimeoutExpired as exc:
            raise GwsCliError(f"gws command timed out after {self.timeout_seconds}s") from exc

        if completed.returncode != 0:
            hint = " Run `gws auth login -s drive,gmail`." if completed.returncode == 2 else ""
            detail = _redact_text(completed.stderr or completed.stdout or "no output")
            raise GwsCliError(f"gws exited {completed.returncode}:{hint} {detail}".strip())
        return completed.stdout.strip()


@dataclass(frozen=True)
class GwsGmailMessage:
    message_id: str
    thread_id: str
    rfc_message_id: str = ""
    subject: str = "(no subject)"
    from_addr: str = ""
    to_addr: str = ""
    cc_addr: str = ""
    date: datetime | None = None
    body_text: str = ""
    body_html: str = ""
    direction: str = "inbound"
    read: bool = True
    starred: bool = False
    label: str = ""


class GwsGmailConnector(Connector):
    """Read Gmail messages through the local gws CLI."""

    def __init__(
        self,
        runner: GwsCliRunner | None = None,
        *,
        query: str = DEFAULT_GWS_GMAIL_QUERY,
        max_messages: int = DEFAULT_GWS_GMAIL_MAX_MESSAGES,
        max_body_chars: int = DEFAULT_GWS_GMAIL_MAX_BODY_CHARS,
    ) -> None:
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        if max_body_chars <= 0:
            raise ValueError("max_body_chars must be positive")
        self.runner = runner or GwsCliRunner()
        self.query = query.strip()
        self.max_messages = max_messages
        self.max_body_chars = max_body_chars

    def iter_messages(self, since: datetime | None) -> Iterator[GwsGmailMessage]:
        for item in self._iter_message_refs(since):
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            read = self.runner.run_json(["gmail", "+read", "--id", message_id, "--headers"])
            message = _gmail_message(
                message_id,
                str(item.get("threadId") or ""),
                read,
                self.max_body_chars,
            )
            if message is not None:
                yield message

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        for message in self.iter_messages(since):
            doc = _gmail_document(message, self.max_body_chars)
            if doc is not None:
                yield doc

    def _iter_message_refs(self, since: datetime | None) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        yielded = 0
        while yielded < self.max_messages:
            params: dict[str, Any] = {
                "userId": "me",
                "maxResults": min(100, self.max_messages - yielded),
                "q": _gmail_query(self.query, since),
            }
            if page_token:
                params["pageToken"] = page_token
            data = self.runner.run_json(
                ["gmail", "users", "messages", "list", "--params", _json_arg(params)]
            )
            messages = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(messages, list) or not messages:
                return
            for item in messages:
                if isinstance(item, dict):
                    yielded += 1
                    yield item
                    if yielded >= self.max_messages:
                        return
            page_token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
            if not page_token:
                return


class GwsDriveConnector(Connector):
    """Read Drive file metadata/content through the local gws CLI."""

    def __init__(
        self,
        runner: GwsCliRunner | None = None,
        *,
        query: str = DEFAULT_GWS_DRIVE_QUERY,
        max_files: int = DEFAULT_GWS_DRIVE_MAX_FILES,
        max_bytes: int = DEFAULT_GWS_DRIVE_MAX_BYTES,
    ) -> None:
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.runner = runner or GwsCliRunner()
        self.query = query.strip() or DEFAULT_GWS_DRIVE_QUERY
        self.max_files = max_files
        self.max_bytes = max_bytes

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        for item in self._iter_files():
            modified_at = _parse_time(item.get("modifiedTime"))
            if since is not None and modified_at is not None and modified_at <= since:
                continue
            doc = self._drive_document(item, modified_at)
            if doc is not None:
                yield doc

    def _iter_files(self) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        yielded = 0
        while yielded < self.max_files:
            params: dict[str, Any] = {
                "pageSize": min(100, self.max_files - yielded),
                "q": self.query,
                "fields": (
                    "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,size,trashed)"
                ),
            }
            if page_token:
                params["pageToken"] = page_token
            data = self.runner.run_json(["drive", "files", "list", "--params", _json_arg(params)])
            files = data.get("files") if isinstance(data, dict) else None
            if not isinstance(files, list) or not files:
                return
            for item in files:
                if isinstance(item, dict):
                    yielded += 1
                    yield item
                    if yielded >= self.max_files:
                        return
            page_token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
            if not page_token:
                return

    def _drive_document(
        self,
        item: dict[str, Any],
        modified_at: datetime | None,
    ) -> InternalDocument | None:
        file_id = str(item.get("id") or "").strip()
        if not file_id or item.get("trashed") is True:
            return None
        mime_type = str(item.get("mimeType") or "")
        if mime_type == "application/vnd.google-apps.folder":
            return None
        name = _redact_text(str(item.get("name") or file_id))[:180]
        text, content_status = self._read_file_text(file_id, mime_type, item.get("size"))
        title = f"Drive: {name}"
        lines = [
            f"# {name}",
            "",
            "Source: gws_drive",
            f"File ID: {file_id}",
            f"MIME: {mime_type or 'unknown'}",
            f"Modified: {item.get('modifiedTime') or ''}",
            f"URL: {item.get('webViewLink') or ''}",
            f"Content: {content_status}",
            "",
            "## Content",
            "",
            text.strip() or "(content unavailable)",
            "",
        ]
        return InternalDocument(
            title=title,
            markdown="\n".join(lines),
            source="gws_drive",
            source_external_id=f"drive:{file_id}",
            source_last_edited_at=modified_at,
            project="company",
        )

    def _read_file_text(
        self,
        file_id: str,
        mime_type: str,
        size_value: Any,
    ) -> tuple[str, str]:
        if _metadata_size_too_large(size_value, self.max_bytes):
            return "", "skipped:size_limit"
        args = _drive_download_args(file_id, mime_type)
        if args is None:
            return "", "skipped:unsupported_mime"

        with tempfile.TemporaryDirectory(prefix="orthus-gws-drive-") as tmp:
            output = Path(tmp) / "content"
            try:
                fallback_stdout = self.runner.run_to_file(args, output)
            except GwsCliError as exc:
                return "", f"skipped:{_short_error(exc)}"
            try:
                raw = output.read_bytes() if output.exists() else fallback_stdout.encode()
            except OSError:
                return "", "skipped:read_failed"
        if len(raw) > self.max_bytes:
            return "", "skipped:size_limit"
        if b"\x00" in raw:
            return "", "skipped:binary"
        text = raw.decode("utf-8", errors="ignore").strip()
        return _redact_text(text), "ok"


def gws_command_available(command: str = DEFAULT_GWS_COMMAND) -> bool:
    argv = _command_argv(command)
    return bool(argv and shutil.which(argv[0]))


def gws_install_message(command: str = DEFAULT_GWS_COMMAND) -> str:
    name = _command_argv(command)[0] if _command_argv(command) else DEFAULT_GWS_COMMAND
    return (
        f"{name} command not found; install googleworkspace-cli and run "
        "`gws auth login -s drive,gmail`"
    )


def gws_command_label(command: str = DEFAULT_GWS_COMMAND) -> str:
    argv = _command_argv(command)
    return Path(argv[0]).name if argv else DEFAULT_GWS_COMMAND


def _gmail_message(
    message_id: str,
    thread_id: str,
    payload: Any,
    max_body_chars: int,
) -> GwsGmailMessage | None:
    if isinstance(payload, str):
        subject = "(no subject)"
        sender = ""
        recipients = ""
        cc_addr = ""
        sent_at = None
        rfc_message_id = ""
        body_text = payload
        body_html = ""
        labels: list[str] = []
        read = True
        starred = False
        direction = "inbound"
    elif isinstance(payload, dict):
        headers = payload.get("headers")
        subject = str(payload.get("subject") or _header_value(headers, "subject") or "(no subject)")
        sender = str(payload.get("from") or _header_value(headers, "from") or "")
        recipients = str(payload.get("to") or _header_value(headers, "to") or "")
        cc_addr = str(payload.get("cc") or _header_value(headers, "cc") or "")
        sent_at = str(payload.get("date") or _header_value(headers, "date") or "") or None
        rfc_message_id = str(
            payload.get("message_id")
            or payload.get("messageId")
            or _header_value(headers, "message-id")
            or ""
        )
        body_text = str(
            payload.get("body_text")
            or payload.get("body")
            or payload.get("text")
            or payload.get("plain")
            or ""
        )
        body_html = str(payload.get("body_html") or payload.get("html") or "")
        labels = _label_values(
            payload.get("labelIds") or payload.get("label_ids") or payload.get("labels")
        )
        read = _gmail_read_value(payload, labels)
        starred = _has_label(labels, "STARRED") or _bool_value(
            payload.get("starred"), default=False
        )
        direction = "outbound" if _has_label(labels, "SENT") else "inbound"
    else:
        return None

    return GwsGmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        rfc_message_id=rfc_message_id,
        subject=subject[:180],
        from_addr=sender,
        to_addr=recipients,
        cc_addr=cc_addr,
        date=_parse_email_date(sent_at),
        body_text=body_text[:max_body_chars],
        body_html=body_html[:max_body_chars],
        direction=direction,
        read=read,
        starred=starred,
        label=",".join(labels),
    )


def _gmail_document(message: GwsGmailMessage, max_body_chars: int) -> InternalDocument | None:
    body = _redact_text(message.body_text or message.body_html)[:max_body_chars]
    if len(body.strip()) < 2:
        return None
    subject = _redact_text(message.subject)[:180]
    sender = _redact_text(message.from_addr)
    recipients = _redact_text(message.to_addr)
    title = f"Gmail: {subject}"
    lines = [
        f"# {subject}",
        "",
        "Source: gws_gmail",
        f"Message ID: {message.message_id}",
        f"Thread ID: {message.thread_id}",
        f"From: {sender}",
        f"To: {recipients}",
        f"Date: {message.date.isoformat() if message.date else ''}",
        "",
        "## Body",
        "",
        body,
        "",
    ]
    return InternalDocument(
        title=title,
        markdown="\n".join(lines),
        source="gws_gmail",
        source_external_id=f"gmail:{message.message_id}",
        source_last_edited_at=message.date,
        project="company",
    )


def _gmail_query(base: str, since: datetime | None) -> str:
    parts = [base] if base else []
    if since is not None:
        parts.append(f"after:{since.astimezone(UTC).strftime('%Y/%m/%d')}")
    return " ".join(parts).strip()


def _drive_download_args(file_id: str, mime_type: str) -> list[str] | None:
    export_mime = _GOOGLE_MIME_EXPORTS.get(mime_type)
    if export_mime is not None:
        if export_mime.startswith("image/"):
            return None
        return [
            "drive",
            "files",
            "export",
            "--params",
            _json_arg({"fileId": file_id, "mimeType": export_mime}),
        ]
    if _is_text_mime(mime_type):
        return [
            "drive",
            "files",
            "get",
            "--params",
            _json_arg({"fileId": file_id, "alt": "media"}),
        ]
    return None


def _is_text_mime(mime_type: str) -> bool:
    return mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_TYPES


def _metadata_size_too_large(value: Any, max_bytes: int) -> bool:
    if value in (None, ""):
        return False
    try:
        return int(value) > max_bytes
    except (TypeError, ValueError):
        return False


def _header_value(headers: Any, name: str) -> str:
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
        return ""
    if isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict) and str(item.get("name") or "").lower() == name.lower():
                return str(item.get("value") or "")
    return ""


def _label_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[\s,]+", str(value))
    return [item.strip() for item in raw if item.strip()]


def _has_label(labels: Sequence[str], name: str) -> bool:
    return any(label.upper() == name.upper() for label in labels)


def _gmail_read_value(payload: dict[str, Any], labels: Sequence[str]) -> bool:
    if _has_label(labels, "UNREAD"):
        return False
    if "unread" in payload:
        return not _bool_value(payload.get("unread"), default=False)
    if "read" in payload:
        return _bool_value(payload.get("read"), default=True)
    if "is_read" in payload:
        return _bool_value(payload.get("is_read"), default=True)
    return True


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def _parse_email_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _json_arg(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _with_json_format(args: Sequence[str]) -> list[str]:
    if "--format" in args:
        return list(args)
    return [*args, "--format", "json"]


def _command_argv(command: str) -> list[str]:
    argv = shlex.split(command.strip() or DEFAULT_GWS_COMMAND)
    if not argv:
        argv = [DEFAULT_GWS_COMMAND]
    command_name = Path(argv[0]).name
    if command_name not in ALLOWED_GWS_COMMAND_NAMES:
        raise GwsCliError(f"gws command not allowlisted: {command_name}")
    return argv


def _short_error(exc: Exception) -> str:
    text = re.sub(r"[^a-zA-Z0-9_:-]+", "_", str(exc).strip().lower())
    return text[:80] or "fetch_failed"


def _redact_text(text: str) -> str:
    out = text.replace("\x00", " ")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return redact_pii_text(re.sub(r"\s+", " ", out).strip())
