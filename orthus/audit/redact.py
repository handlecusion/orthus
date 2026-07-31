"""PII redaction (operations §2.3). Applied to every audit `output` and any
serialized query result before it leaves the process. Rules are unit-tested."""

from __future__ import annotations

import re
from typing import Any

# email: mask the local-part, keep domain for debuggability.
_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# Korean RRN: 6 digits - 7 digits.
_RRN = re.compile(r"\b(\d{6})-?\d{7}\b")
# card: 13-16 digits, optionally grouped by space/dash.
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
# phone: KR mobile / generic separated digit groups.
_PHONE = re.compile(r"\b(\+?\d{1,3}[- ]?)?(\d{2,4})[- ]?(\d{3,4})[- ]?(\d{4})\b")
# UUIDs are internal identifiers; protect them from the generic phone regex.
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _mask_str(s: str) -> str:
    uuids: list[str] = []

    def keep_uuid(match: re.Match[str]) -> str:
        uuids.append(match.group(0))
        return f"__ORTHUS_UUID_{len(uuids) - 1}__"

    s = _UUID.sub(keep_uuid, s)
    s = _EMAIL.sub(lambda m: f"{m.group(1)[0]}***{m.group(2)}", s)
    s = _RRN.sub(lambda m: f"{m.group(1)}-*******", s)
    # card before phone (card is the longer/more specific digit run).
    s = _CARD.sub(lambda m: "****-****-****-****", s)
    s = _PHONE.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}-****-{m.group(4)}", s)
    for idx, value in enumerate(uuids):
        s = s.replace(f"__ORTHUS_UUID_{idx}__", value)
    return s


def redact_pii_text(s: str) -> str:
    """Mask PII in a single string. Same rule set as `redact_pii`."""
    return _mask_str(s)


def redact_pii(obj: Any) -> Any:
    """Recursively mask PII in strings inside dict/list/str. Other types pass through."""
    if isinstance(obj, str):
        return redact_pii_text(obj)
    if isinstance(obj, dict):
        return {k: redact_pii(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_pii(v) for v in obj]
    return obj
