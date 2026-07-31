"""Slack channel/thread connector."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from orthus.audit.redact import redact_pii_text
from orthus.connectors.base import Connector
from orthus.connectors.project_map import PROJECTS
from orthus.schemas.canonical import InternalDocument
from orthus.structured.slack_extract import extract_slack_rows_from_messages

DEFAULT_SLACK_MAX_MESSAGES = 300
_DIRECT_EPISODE_MAX_MESSAGES = 24
_DIRECT_GAP_SOFT_SECONDS = 10 * 60
_DIRECT_GAP_LOOSE_SECONDS = 60 * 60
_ALLOWED_SUBTYPES = {"", "thread_broadcast"}
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]
_URL_RE = re.compile(r"https?://\S+|<https?://[^>|]+(?:\|[^>]+)?>", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}")
_SLACK_MENTION_RE = re.compile(r"<@U[A-Z0-9]+>|U[A-Z0-9]{8,}")
_WORD_RE = re.compile(r"[A-Za-z0-9가-힣]+")
_REPLY_CUE_RE = re.compile(
    r"(?i)(^|\s)(그거|그건|그걸|그럼|근데|그리고|아까|위에|방금|이거|요거|저거|"
    r"맞아|아니|ㅇㅇ|오키|확인|넵|네|됩니다|안됩니다|해줘|해주세요|"
    r"that|this|it|above|earlier|then|also|yes|no|ok|re:|about)(\s|$)"
)
_PROJECT_WEIGHTS: dict[str, tuple[tuple[str, int], ...]] = {
    "atlas": (
        ("아틀라스", 5),
        ("출연자", 4),
        ("촬영현장", 4),
        ("촬영관리", 4),
        ("배역", 4),
        ("정산", 4),
        ("계약서", 4),
        ("큐알", 3),
        ("qr", 3),
        ("출근", 3),
        ("퇴근", 3),
        ("공고", 3),
        ("가온엔터", 3),
        ("국적", 3),
        ("실명인증", 3),
        ("2차인증", 3),
        ("검수서", 3),
        ("반납", 3),
    ),
    "nova": (
        ("nova", 6),
        ("nova.example", 6),
        ("노바티", 6),
        ("노바디", 6),
        ("vfx", 4),
        ("dpx", 4),
        ("프리미어", 4),
        ("premiere", 4),
        ("after effects", 4),
        ("ai 영상", 4),
        ("레퍼런스", 3),
        ("원본 프레임", 3),
        ("higgsfield", 3),
        ("ltx", 3),
        ("wan", 3),
        ("캡션", 3),
        ("captions", 3),
    ),
    "orbit": (
        ("orbit", 6),
        ("매물", 5),
        ("부동산", 5),
        ("홈즈데모", 5),
        ("중앙센터", 4),
        ("원룸백과", 5),
        ("총동문회", 5),
        ("포털 부동산", 5),
        ("findb", 4),
        ("demohomes", 4),
        ("bdsdemo", 4),
        ("chrome extension", 3),
        ("chromewebstore", 3),
    ),
}
_LOW_VALUE_ACKS = {
    "ㅇㅇ",
    "ㅇㅇㅇ",
    "음",
    "굿",
    "굿굿",
    "오케이",
    "오키",
    "확인",
    "확인이용",
    "화긴",
    "돼",
    "어어",
    "잠시만",
    "좋습니다",
    "찢",
    "그럼",
    "보는중",
    "처리됬나",
    "먼소리지",
}
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "있습니다",
    "합니다",
    "해주세요",
    "확인",
    "관련",
    "오늘",
    "내일",
    "그거",
    "이거",
    "요거",
    "아까",
}
_PROMPT_INJECTION_RE = re.compile(
    r"(?i)(지금까지의\s*(모든\s*)?지시|지시.*잊|ignore.*instructions|"
    r"system prompt|developer message|너는\s*윤리적인\s*에이전트)"
)
_HTTP_TRACE_RE = re.compile(
    r"(?i)(request\s+url|method\s+(get|post|put|patch|delete)|status\s+code|"
    r"remote\s+address|referrer\s+policy)"
)


@dataclass(frozen=True)
class SlackMessage:
    ts: str
    user: str
    text: str
    raw_text: str = ""


@dataclass(frozen=True)
class SlackThread:
    channel: str
    thread_ts: str
    messages: list[SlackMessage]
    is_channel_episode: bool = False


def parse_slack_channels(value: str) -> tuple[str, ...]:
    channels: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        channels.append(item)
    return tuple(channels)


def parse_slack_channel_projects(value: str) -> dict[str, str]:
    """Parse `C123=atlas,C999=nova` project defaults for Slack channels."""
    out: dict[str, str] = {}
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"invalid Slack channel project mapping: {item}")
        channel, project = [part.strip() for part in item.split("=", 1)]
        if not channel or project not in PROJECTS:
            raise ValueError(f"invalid Slack channel project mapping: {item}")
        out[channel] = project
    return out


class SlackConnector(Connector):
    """Connector over Slack conversations.history and conversations.replies."""

    def __init__(
        self,
        token: str,
        channels: Iterable[str],
        *,
        client: httpx.Client | None = None,
        max_messages: int = DEFAULT_SLACK_MAX_MESSAGES,
        channel_projects: dict[str, str] | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("Slack token required")
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        self.channels = tuple(parse_slack_channels(",".join(channels)))
        if not self.channels:
            raise ValueError("at least one Slack channel required")
        self.max_messages = max_messages
        self.channel_projects = dict(channel_projects or {})
        self._client = client or httpx.Client(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        oldest = str(since.timestamp()) if since is not None else None
        for channel in self.channels:
            history = [
                item for item in self._history(channel, oldest=oldest) if _message_allowed(item)
            ]
            for thread in self._threads_from_history(channel, history):
                messages = [msg for msg in thread.messages if msg.text]
                if not messages:
                    continue
                yield _document_from_thread(
                    SlackThread(channel, thread.thread_ts, messages, thread.is_channel_episode),
                    channel_projects=self.channel_projects,
                )

    def _history(self, channel: str, *, oldest: str | None) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        yielded = 0
        while yielded < self.max_messages:
            params: dict[str, Any] = {
                "channel": channel,
                "limit": min(200, self.max_messages - yielded),
            }
            if oldest:
                params["oldest"] = oldest
            if cursor:
                params["cursor"] = cursor
            data = self._get("conversations.history", params)
            messages = data.get("messages") or []
            if not isinstance(messages, list) or not messages:
                return
            for item in messages:
                if isinstance(item, dict):
                    yielded += 1
                    yield item
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return

    def _replies(self, channel: str, thread_ts: str) -> list[SlackMessage]:
        data = self._get(
            "conversations.replies",
            {"channel": channel, "ts": thread_ts, "limit": min(200, self.max_messages)},
        )
        messages = data.get("messages") or []
        if not isinstance(messages, list):
            return []
        return [
            _slack_message(item)
            for item in messages
            if isinstance(item, dict) and _message_allowed(item)
        ]

    def _threads_from_history(
        self, channel: str, history: list[dict[str, Any]]
    ) -> Iterator[SlackThread]:
        """Build SlackThread docs from real threads plus channel-adjacent replies.

        Slack users often answer in the channel instead of a Slack thread. Time
        gaps are only soft evidence: direct-channel messages stay in one episode
        when project/entity terms, repeated participants, or reply-like phrasing
        show continuity, and split when the topic changes even if messages are
        close together.
        """
        seen_threads: set[str] = set()
        pending_direct: list[SlackMessage] = []

        def flush_direct() -> SlackThread | None:
            if not pending_direct:
                return None
            messages = list(pending_direct)
            pending_direct.clear()
            return SlackThread(channel, messages[0].ts, messages, is_channel_episode=True)

        for item in sorted(history, key=lambda value: _slack_ts_float(str(value.get("ts") or ""))):
            ts = str(item.get("ts") or "")
            thread_ts = str(item.get("thread_ts") or ts)
            is_thread = thread_ts != ts or int(item.get("reply_count") or 0) > 0
            if is_thread:
                direct = flush_direct()
                if direct is not None:
                    yield direct
                if thread_ts in seen_threads:
                    continue
                seen_threads.add(thread_ts)
                messages = self._replies(channel, thread_ts) or [_slack_message(item)]
                yield SlackThread(channel, thread_ts, messages)
                continue

            message = _slack_message(item)
            if pending_direct and not _continues_direct_episode(pending_direct, message):
                direct = flush_direct()
                if direct is not None:
                    yield direct
            pending_direct.append(message)

        direct = flush_direct()
        if direct is not None:
            yield direct

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            error = data.get("error") if isinstance(data, dict) else "invalid_response"
            raise RuntimeError(f"slack_api_error:{error}")
        return data


def _message_allowed(item: dict[str, Any]) -> bool:
    subtype = str(item.get("subtype") or "")
    if subtype not in _ALLOWED_SUBTYPES:
        return False
    if item.get("bot_id") or item.get("app_id"):
        return False
    return bool(str(item.get("text") or "").strip())


def _slack_message(item: dict[str, Any]) -> SlackMessage:
    raw_text = str(item.get("text") or "")
    return SlackMessage(
        ts=str(item.get("ts") or ""),
        user=str(item.get("user") or ""),
        text=_redact_text(raw_text),
        raw_text=raw_text,
    )


def _document_from_thread(
    thread: SlackThread, *, channel_projects: dict[str, str] | None = None
) -> InternalDocument:
    title_text = thread.messages[0].text[:80] if thread.messages else thread.thread_ts
    lines = [
        f"# Slack {thread.channel} {thread.thread_ts}",
        "",
        "Source: slack",
        f"Channel: {thread.channel}",
        f"Thread: {thread.thread_ts}",
        f"Messages: {len(thread.messages)}",
    ]
    if thread.is_channel_episode:
        lines.extend(
            [
                "Context: direct-channel-episode",
                "",
                "## Channel context",
                "",
                (
                    "Grouped direct channel messages by project/entity overlap, "
                    "reply cues, and participant continuity."
                ),
            ]
        )
    lines.extend(["", "## Transcript", ""])
    for message in thread.messages:
        lines.append(f"### {message.user or 'unknown'} ({message.ts})")
        lines.append("")
        lines.append(message.text)
        lines.append("")
    structured_messages = [
        {"user": message.user, "ts": message.ts, "text": message.raw_text or message.text}
        for message in thread.messages
    ]
    project = resolve_slack_project(thread, channel_projects=channel_projects)
    wiki_authoring, wiki_skip_reason = should_author_slack_thread(thread)
    return InternalDocument(
        title=f"Slack {thread.channel}: {title_text}",
        markdown="\n".join(lines).rstrip() + "\n",
        source="slack",
        source_external_id=f"slack:{thread.channel}:{thread.thread_ts}",
        source_last_edited_at=_slack_ts_to_datetime(thread.thread_ts),
        project=project,
        structured_rows=extract_slack_rows_from_messages(
            thread.channel, thread.thread_ts, structured_messages
        ),
        wiki_authoring=wiki_authoring,
        wiki_skip_reason=wiki_skip_reason,
    )


def resolve_slack_project(
    thread: SlackThread, *, channel_projects: dict[str, str] | None = None
) -> str:
    """Resolve company-internal project bucket for Slack content."""
    text = _thread_text(thread)
    explicit = resolve_slack_project_from_text(text)
    if explicit is not None:
        return explicit
    default = (channel_projects or {}).get(thread.channel)
    return default if default in PROJECTS else "company"


def resolve_slack_project_from_text(text: str) -> str | None:
    lowered = text.lower()
    scores: dict[str, int] = {project: 0 for project in PROJECTS}
    for project, terms in _PROJECT_WEIGHTS.items():
        for term, weight in terms:
            if term.lower() in lowered:
                scores[project] += weight
    project, score = max(scores.items(), key=lambda item: item[1])
    return project if score >= 5 else None


def should_author_slack_thread(thread: SlackThread) -> tuple[bool, str | None]:
    texts = [message.raw_text or message.text for message in thread.messages]
    return should_author_slack_texts(texts)


def should_author_slack_markdown(title: str, markdown: str) -> tuple[bool, str | None]:
    del title
    texts: list[str] = []
    capture = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped == "## Transcript":
            capture = True
            continue
        if not capture or not stripped or stripped.startswith("### "):
            continue
        if stripped.startswith(("Source:", "Channel:", "Thread:", "Messages:")):
            continue
        texts.append(stripped)
    return should_author_slack_texts(texts)


def should_author_slack_texts(texts: list[str]) -> tuple[bool, str | None]:
    body = _clean_text(" ".join(texts))
    if not body:
        return False, "empty_slack"
    if _PROMPT_INJECTION_RE.search(body):
        return False, "prompt_injection_like"
    if _HTTP_TRACE_RE.search(body):
        return False, "slack_http_trace"
    without_urls = _clean_text(_URL_RE.sub(" ", body))
    without_mentions = _clean_text(_SLACK_MENTION_RE.sub(" ", without_urls))
    words = _WORD_RE.findall(without_mentions)
    low = without_mentions.lower()
    if len(texts) <= 1 and (_URL_RE.search(body) and len(words) <= 8):
        return False, "slack_url_only"
    if len(texts) <= 1 and low in _LOW_VALUE_ACKS:
        return False, "slack_ack_only"
    if (
        len(texts) <= 1
        and len(without_mentions) < 20
        and resolve_slack_project_from_text(body) is None
    ):
        return False, "slack_too_short"
    if len(texts) <= 1 and (_EMAIL_RE.search(body) or _PHONE_RE.search(body)) and len(words) <= 10:
        return False, "slack_contact_only"
    if body.count("http") >= 2 and len(words) <= 12:
        return False, "slack_link_dump"
    return True, None


def _thread_text(thread: SlackThread) -> str:
    return " ".join(message.raw_text or message.text for message in thread.messages)


def _continues_direct_episode(messages: list[SlackMessage], message: SlackMessage) -> bool:
    if len(messages) >= _DIRECT_EPISODE_MAX_MESSAGES:
        return False

    episode_text = _thread_text(SlackThread("", "", messages))
    message_text = message.raw_text or message.text
    episode_project = resolve_slack_project_from_text(episode_text)
    message_project = resolve_slack_project_from_text(message_text)
    if episode_project and message_project and episode_project != message_project:
        return False

    gap = _slack_ts_float(message.ts) - _slack_ts_float(messages[-1].ts)
    episode_terms = _topic_terms(episode_text)
    message_terms = _topic_terms(message_text)
    overlap = episode_terms & message_terms
    users = {item.user for item in messages if item.user}
    reply_like = _is_reply_like(message_text)

    score = 0
    if episode_project and message_project and episode_project == message_project:
        score += 2
    if len(overlap) >= 2:
        score += 2
    elif len(overlap) == 1:
        score += 1
    if message.user and message.user in users and len(users) <= 4:
        score += 1
    if reply_like:
        score += 2
    if gap <= _DIRECT_GAP_SOFT_SECONDS:
        score += 1
    elif gap <= _DIRECT_GAP_LOOSE_SECONDS:
        score += 1

    has_continuity_signal = bool(reply_like or overlap or gap <= _DIRECT_GAP_SOFT_SECONDS)
    return score >= 3 and has_continuity_signal


def _topic_terms(text: str) -> set[str]:
    cleaned = _SLACK_MENTION_RE.sub(" ", _URL_RE.sub(" ", text.casefold()))
    return {word for word in _WORD_RE.findall(cleaned) if len(word) >= 2 and word not in _STOPWORDS}


def _is_reply_like(text: str) -> bool:
    return bool(_REPLY_CUE_RE.search(_clean_text(text)))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\x00", " ")).strip()


def _slack_ts_to_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(_slack_ts_float(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _slack_ts_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _redact_text(text: str) -> str:
    out = text.replace("\x00", " ")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return redact_pii_text(re.sub(r"\s+", " ", out).strip())
