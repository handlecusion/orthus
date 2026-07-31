"""Extract structured rows from Slack transcripts."""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any

_MESSAGE_HEADER = re.compile(r"^###\s+(?P<user>\S+)\s+\((?P<ts>[^)]+)\)\s*$")
_MENTION = re.compile(r"<@([A-Z0-9]+)>")
_MAILTO = re.compile(r"<mailto:([^|>]+)(?:\|([^>]+))?>", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+\-*]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")
_TEL = re.compile(r"<tel:([^|>]+)(?:\|([^>]+))?>", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)((?:\+?82[- ]?)?0?\d{1,2}[- ]?\*{0,4}\d{0,4}[- ]?\d{4})(?!\d)")
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>", re.IGNORECASE)
_URL = re.compile(r"(?<![<|])(https?://[^\s>)]+)", re.IGNORECASE)
_KOREAN_NAME_BEFORE = re.compile(r"([가-힣]{2,6})\s*(?:/|:|-)?\s*$")
_PLACE = re.compile(r"(?:위치|장소)\s*[:：]\s*([^/\n,]+)")
_WHEN = re.compile(
    r"(오늘|내일|모레|다음주|담주|[월화수목금토일]요일|오전\s*\d{1,2}시|오후\s*\d{1,2}시|\d{1,2}시(?:\s*~\s*\d{1,2}시)?)"
)

_ACTION_TERMS = ("해주세요", "해줘", "부탁", "요청", "해야", "해야함", "마감", "처리", "복구")
_EVENT_TERMS = ("미팅", "일정", "워크숍", "회의", "예약", "상담", "발표평가")
_DECISION_TERMS = ("결정", "하기로", "될듯", "고정", "완료함", "승인", "원인")


def extract_slack_rows_from_markdown(markdown: str) -> list[dict[str, Any]]:
    channel, thread_ts = _parse_header(markdown)
    return extract_slack_rows_from_messages(channel, thread_ts, _parse_messages(markdown))


def extract_slack_rows_from_messages(
    channel: str, thread_ts: str, messages: list[dict[str, str]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for idx, message in enumerate(messages):
        user = message.get("user", "")
        ts = message.get("ts", "") or f"msg-{idx}"
        text = _clean_text(message.get("text", ""))
        if not text:
            continue
        base = {
            "channel": channel,
            "thread_ts": thread_ts,
            "message_ts": ts,
            "speaker": user,
        }
        for row in _contact_rows(text, base):
            _append_unique(rows, seen, row)
        for row in _link_rows(text, base):
            _append_unique(rows, seen, row)
        for row in _event_rows(text, base):
            _append_unique(rows, seen, row)
        for row in _action_rows(text, base):
            _append_unique(rows, seen, row)
        for row in _decision_rows(text, base):
            _append_unique(rows, seen, row)
    return rows


def _parse_header(markdown: str) -> tuple[str, str]:
    channel = ""
    thread_ts = ""
    for line in markdown.splitlines():
        if line.startswith("Channel:"):
            channel = line.partition(":")[2].strip()
        elif line.startswith("Thread:"):
            thread_ts = line.partition(":")[2].strip()
    return channel, thread_ts


def _parse_messages(markdown: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    body: list[str] = []
    for line in markdown.splitlines():
        match = _MESSAGE_HEADER.match(line)
        if match:
            if current is not None:
                current["text"] = "\n".join(body).strip()
                messages.append(current)
            current = {"user": match.group("user"), "ts": match.group("ts"), "text": ""}
            body = []
            continue
        if current is not None:
            body.append(line)
    if current is not None:
        current["text"] = "\n".join(body).strip()
        messages.append(current)
    return messages


def _contact_rows(text: str, base: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    emails = _emails(text)
    phones = _phones(text)
    if not emails and not phones:
        return rows
    name = _nearby_name(text, [*emails, *phones])
    for email in emails:
        props = {**base, "name": name, "email": email, "is_redacted": "*" in email}
        rows.append(_row("contact", f"{base['message_ts']}:email:{email}", props, text, "medium"))
    for phone in phones:
        props = {**base, "name": name, "phone": phone, "is_redacted": "*" in phone}
        rows.append(_row("contact", f"{base['message_ts']}:phone:{phone}", props, text, "medium"))
    return rows


def _link_rows(text: str, base: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for idx, (url, label) in enumerate(_links(text)):
        props = {**base, "url": url, "label": label or url}
        rows.append(
            _row("link", f"{base['message_ts']}:link:{idx}:{_short_hash(url)}", props, text, "high")
        )
    return rows


def _event_rows(text: str, base: dict[str, str]) -> list[dict[str, Any]]:
    if not any(term in text for term in _EVENT_TERMS):
        return []
    when = _first_match(_WHEN, text)
    place = _first_match(_PLACE, text)
    props = {**base, "text": text, "when_text": when, "place_text": place}
    return [_row("event", f"{base['message_ts']}:event:{_short_hash(text)}", props, text, "medium")]


def _action_rows(text: str, base: dict[str, str]) -> list[dict[str, Any]]:
    if not any(term in text for term in _ACTION_TERMS):
        return []
    props = {
        **base,
        "text": text,
        "mentions": _MENTION.findall(text),
        "due_text": _first_match(_WHEN, text),
    }
    return [
        _row(
            "action_item", f"{base['message_ts']}:action:{_short_hash(text)}", props, text, "medium"
        )
    ]


def _decision_rows(text: str, base: dict[str, str]) -> list[dict[str, Any]]:
    if not any(term in text for term in _DECISION_TERMS):
        return []
    props = {**base, "text": text}
    return [
        _row(
            "decision", f"{base['message_ts']}:decision:{_short_hash(text)}", props, text, "medium"
        )
    ]


def _emails(text: str) -> list[str]:
    out: list[str] = []
    for match in _MAILTO.finditer(text):
        _append(out, match.group(1))
    for match in _EMAIL.finditer(text):
        _append(out, match.group(1))
    return out


def _phones(text: str) -> list[str]:
    out: list[str] = []
    for match in _TEL.finditer(text):
        _append(out, match.group(1))
    for match in _PHONE.finditer(text):
        value = match.group(1)
        digits = re.sub(r"\D", "", value)
        has_separator = "-" in value or " " in value
        if "*" in value or has_separator or (digits.startswith("0") and len(digits) >= 9):
            _append(out, value)
    return out


def _links(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _SLACK_LINK.finditer(text):
        url = match.group(1)
        label = match.group(2) or ""
        if not url.lower().startswith(("mailto:", "tel:")):
            out.append((url, label))
    for match in _URL.finditer(text):
        url = match.group(1)
        if not any(existing == url for existing, _label in out):
            out.append((url, ""))
    return out


def _nearby_name(text: str, values: list[str]) -> str:
    for value in values:
        idx = -1
        for marker in (f"<mailto:{value}", f"<tel:{value}", value):
            idx = text.find(marker)
            if idx >= 0:
                break
        if idx < 0:
            continue
        prefix = text[max(0, idx - 24) : idx]
        match = _KOREAN_NAME_BEFORE.search(prefix)
        if match:
            return match.group(1)
    return ""


def _row(
    record_type: str, record_key: str, properties: dict[str, Any], evidence: str, confidence: str
) -> dict[str, Any]:
    return {
        "source": "slack",
        "record_type": record_type,
        "record_key": record_key,
        "properties": properties,
        "evidence": evidence[:1000],
        "confidence": confidence,
    }


def _append_unique(
    rows: list[dict[str, Any]], seen: set[tuple[str, str]], row: dict[str, Any]
) -> None:
    key = (str(row.get("record_type")), str(row.get("record_key")))
    if key in seen:
        return
    seen.add(key)
    rows.append(row)


def _append(values: list[str], value: str) -> None:
    cleaned = _clean_text(value)
    if cleaned and cleaned not in values:
        values.append(cleaned)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "").replace("\x00", " ")).strip()


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
