"""WikiTask hygiene helpers.

Generated open questions are useful only when they point to durable wiki work.
Connector artifacts, binary/media placeholders, and assistant-session follow-up
questions can otherwise flood `/wiki/tasks` with items a reviewer cannot act on.
"""

from __future__ import annotations

import re
import unicodedata

MAX_OPEN_QUESTIONS_PER_SOURCE = 3

# Source buckets whose generated open questions are activity-log metadata, not
# durable wiki knowledge. Mapped to the hygiene reason recorded on drop/cleanup.
_LOW_SIGNAL_BUCKET_REASON = {
    "ai_session": "ai_session_transient",
    "media": "media_placeholder",
    "weekly_retro": "weekly_retro_transient",
    "slack": "slack_fragment_transient",
    "schedule": "schedule_metadata",
    "mail": "mail_metadata",
}

# Dashboard log sources whose rows are transactional records, not narrative docs.
_DASHBOARD_LOG_SOURCES = {"dashboard_meeting", "dashboard_weekly", "dashboard_calendar"}


def is_structured_row_source(source: str | None, source_db_name: str | None) -> bool:
    """True when the originating document is a structured DB row / dashboard log.

    Notion DB rows carry a non-empty ``source_db_name``; dashboard meeting/weekly/
    calendar logs use a specific ``source`` value. Their generated open_questions
    are empty-column fill-in-the-blank prompts ("미팅 내용은 무엇인가?"), not durable
    knowledge, so they must not become tasks. Narrative Notion pages (no
    source_db_name), editor docs, slack, and mail keep their questions. Claims from
    these sources still fold into pages — only the open_questions are suppressed.
    """
    if source_db_name is not None and source_db_name.strip() != "":
        return bool(source) and source.strip() != ""
    return (source or "") in _DASHBOARD_LOG_SOURCES


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[“”\"'`.,;:!?()\[\]{}]+")
_MEDIA_SOURCE_RE = re.compile(
    r"(?:^|[-_.])(mp4|mov|m4v|webm|ts|jpg|jpeg|png|gif|webp|heic)(?:[-_.]|$)",
    re.I,
)
_MEDIA_QUESTION_RE = re.compile(
    "|".join(
        [
            r"visual content",
            r"subject matter",
            r"actual content",
            r"contenu visuel",
            r"contenu r.el",
            r"image",
            r"vid[ée]o",
            r"file size",
            r"size limit",
            r"taille",
            r"duration",
            r"uploaded",
            r"google drive",
            r"owns this file",
            r"created or shared this file",
            r"파일.*크기",
            r"이미지.*내용",
            r"영상.*내용",
        ]
    ),
    re.I,
)
_AUTH_QUESTION_RE = re.compile(
    "|".join(
        [
            r"/login",
            r"log(?:ged)? in",
            r"prompted to log",
            r"authentication",
            r"authentifizierung",
            r"session without",
            r"로그인",
            r"인증",
        ]
    ),
    re.I,
)
_GENERIC_METADATA_RE = re.compile(
    "|".join(
        [
            r"담당자는 누구",
            r"예상 소요 시간",
            r"파트너사는 누구",
            r"참석 직원 목록",
            r"who (?:created|uploaded|owns)",
            r"who owns",
            r"context or purpose",
            r"purpose of this image",
            r"for what purpose",
            r"à quelle série",
            r"wer hat diese datei",
            r"qui a cr",
        ]
    ),
    re.I,
)


def source_bucket(source_slug: str) -> str:
    """Classify generated source slugs for deterministic WikiTask policy."""
    slug = source_slug.lower()
    if slug.startswith(("src-claude-", "src-codex-", "src-chatgpt-", "src-openai-")):
        return "ai_session"
    if slug.startswith("src-주간-회고-"):
        return "weekly_retro"
    if slug.startswith("src-slack-"):
        return "slack"
    if slug.startswith("src-일정-"):
        return "schedule"
    if slug.startswith("src-drive-") and _MEDIA_SOURCE_RE.search(slug):
        return "media"
    if slug.startswith("src-mail-"):
        return "mail"
    return "other"


def normalize_open_question(question: str) -> str:
    """Normalize questions for dedup and per-source cap decisions."""
    text = unicodedata.normalize("NFKC", question).casefold()
    text = _WS_RE.sub(" ", text).strip()
    return text


def open_question_hygiene_reason(question: str, *, source_slug: str) -> str | None:
    """Return reason when a generated open question should not become a task."""
    text = normalize_open_question(question)
    if not text:
        return "blank"

    bucket = source_bucket(source_slug)
    # Transactional/activity-log sources (AI sessions, weekly retros, slack
    # fragments, calendar entries, mail) are not durable knowledge. Their
    # generated open questions are fill-in-the-blank metadata a reviewer cannot
    # answer without the source itself, so they never become tasks. Real claims
    # from these sources still fold into pages; only the open_questions drop.
    bucket_reason = _LOW_SIGNAL_BUCKET_REASON.get(bucket)
    if bucket_reason:
        return bucket_reason
    if _MEDIA_QUESTION_RE.search(text):
        return "media_placeholder"
    if _AUTH_QUESTION_RE.search(text):
        return "auth_session_noise"
    if _GENERIC_METADATA_RE.search(text):
        return "generic_metadata"
    return None


def filter_generated_open_questions(
    source_slug: str,
    questions: list[str],
    *,
    max_questions: int = MAX_OPEN_QUESTIONS_PER_SOURCE,
) -> list[str]:
    """Drop low-signal generated questions, dedupe, and cap per source."""
    kept: list[str] = []
    seen: set[str] = set()
    for question in questions:
        if open_question_hygiene_reason(question, source_slug=source_slug):
            continue
        key = normalize_open_question(question)
        if key in seen:
            continue
        seen.add(key)
        kept.append(question.strip())
        if len(kept) >= max_questions:
            break
    return kept


# --- structured-row claim tautology suppression ----------------------------
# Notion DB row properties render as a flat "**Prop**: Value" markdown dump
# (`connectors/notion.py::_render_properties`). Distilling that flattened dump
# regenerates two low-information claim shapes with zero synthesis beyond the
# raw table cell:
#   1. self-reference — the row's own title echoed back as its own "name":
#      "Wan2.2의 이름은 Wan2.2이다."
#   2. a bare context-free relative-rating adjective with no elaboration:
#      "우선순위는 높음." (docs/agent-chat-answer-quality.md 잔여 항목)
# Both are suppressed for structured row sources only (callers gate this on
# `is_structured_row_source`); narrative documents keep the exact same wording
# as real claims. Real facts (numbers, dates, names, statuses outside the
# closed filler set) are never touched — this is deliberately narrow to avoid
# over-suppression.

_SELF_NAME_KO_RE = re.compile(
    r"^(?P<subject>\S.*?)(?:의)?\s*(?:이름|명칭|제목)\s*(?:은|는)\s*(?P<value>\S.*?)\s*"
    r"(?:이다|입니다|다)\.?\s*$"
)
_SELF_NAME_EN_RE = re.compile(
    r"^(?:the\s+)?(?:name|title)\s+of\s+(?P<subject>.+?)\s+is\s+(?P<value>.+?)\.?\s*$",
    re.IGNORECASE,
)
_SELF_NAME_EN_POSSESSIVE_RE = re.compile(
    r"^(?P<subject>.+?)['’]s\s+(?:name|title)\s+is\s+(?P<value>.+?)\.?\s*$",
    re.IGNORECASE,
)
_SELF_NAME_PATTERNS = (_SELF_NAME_KO_RE, _SELF_NAME_EN_RE, _SELF_NAME_EN_POSSESSIVE_RE)

# Closed set: context-free relative-rating/boolean adjectives that convey no
# information without the property name the flattened row render already
# dropped. Deliberately narrow — dates, numbers, statuses, and named values
# (e.g. "완료", "김철수", "2026-07-01") are never in this set, so they always
# survive as real claims. (Korean "높다/낮다" are intentionally absent: the
# regex's optional trailing-다 copula group always consumes their own final
# "다" first, so a bare "다"-suffixed entry could never actually match — see
# `_BARE_FILLER_KO_RE`.)
_BARE_FILLER_VALUES = {
    "높음",
    "낮음",
    "보통",
    "중간",
    "예",
    "아니오",
    "아니요",
    "네",
    "high",
    "medium",
    "low",
    "true",
    "false",
    "yes",
    "no",
}
# Whole claim must reduce to exactly "<short subject><particle><value>[copula]."
# with nothing else (no comma/extra clause) — genuine bare restatements are
# always this short; anything with real elaboration falls through untouched.
_BARE_FILLER_KO_RE = re.compile(
    r"^\S.{0,30}?(?:은|는|이|가)\s*(?P<value>[^\s,.\n]+?)\s*(?:이다|입니다|다)?\.?\s*$"
)
# English mirror: whole claim reduces to "<subject> is <single-token value>."
# — the value's char class forbids whitespace, so any remainder with a real
# additional clause (multiple words) never matches.
_BARE_FILLER_EN_RE = re.compile(r"^\S.{0,40}?\s+is\s+(?P<value>[^\s.]+?)\.?\s*$", re.IGNORECASE)
_BARE_FILLER_PATTERNS = (_BARE_FILLER_KO_RE, _BARE_FILLER_EN_RE)


def _norm_tautology_span(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = _PUNCT_RE.sub("", value)
    return _WS_RE.sub(" ", value).strip()


def is_structured_row_tautology(claim_text: str) -> bool:
    """True when a structured-row claim is a self-reference or bare filler
    restatement of a single table cell, not a synthesized fact.

    Callers MUST gate this on `is_structured_row_source` first — this check is
    not applied to narrative documents, where identical wording could be a
    legitimate claim (e.g. a page literally about naming conventions).
    """
    text = (claim_text or "").strip()
    if not text:
        return False

    for pattern in _SELF_NAME_PATTERNS:
        m = pattern.match(text)
        if m and _norm_tautology_span(m.group("subject")) == _norm_tautology_span(m.group("value")):
            return True

    for pattern in _BARE_FILLER_PATTERNS:
        m = pattern.match(text)
        if m and _norm_tautology_span(m.group("value")) in _BARE_FILLER_VALUES:
            return True

    return False


def normalize_claim_for_conflict(text: str) -> str:
    """Normalize near-identical claim text before conflict detection.

    This intentionally removes presentation churn only: Unicode compatibility,
    common escaped quote artifacts, case, whitespace, and punctuation. Numeric
    values and words remain, so real content differences still conflict.
    """
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")
    value = value.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    value = value.casefold()
    value = _PUNCT_RE.sub("", value)
    return _WS_RE.sub(" ", value).strip()


def claims_equivalent_for_conflict(existing: str, incoming: str) -> bool:
    return normalize_claim_for_conflict(existing) == normalize_claim_for_conflict(incoming)
