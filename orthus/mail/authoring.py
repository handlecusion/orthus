"""Wiki-authoring gate for ingested mail.

Mirrors `connectors.slack.should_author_slack_markdown`: a deterministic check
that keeps automated / transactional mail (login magic-links, OTP / verification
codes, password resets, test-sends, bounce notices) out of the company wiki.

Why: these carry no lasting knowledge, and near-identical copies (e.g. dozens of
"Orthus 로그인 링크" magic-link mails) collide on the same wiki source slug, so only
one is ever recorded as authored and the rest are re-distilled every compile
cycle — wasted compute plus wiki noise. Content-bearing mail (threads, CI
results, announcements) is unaffected and still authored.
"""

from __future__ import annotations

import re

# Transactional/auth content markers (matched case-insensitively against the
# subject + body). Kept specific to auth/transactional intent so that genuine
# knowledge mail (CI results, announcements, marketing, threads) stays authored.
_TRANSACTIONAL_MARKERS: tuple[str, ...] = (
    # login / magic links
    "로그인 링크",
    "로그인하세요",
    "magic link",
    "magic-link",
    "login link",
    "sign in to your",
    "log in to your",
    # verification / OTP
    "인증 코드",
    "인증코드",
    "인증 번호",
    "인증번호",
    "확인 코드",
    "보안 코드",
    "이메일 인증",
    "verification code",
    "verify your email",
    "one-time password",
    "one time password",
    "one-time code",
    "one time code",
    "otp code",
    "passcode",
    # password reset
    "비밀번호 재설정",
    "password reset",
    "reset your password",
    # test sends
    "test-send",
    "테스트 발송",
)

# Magic-link / token-consume URLs in the body.
_MAGIC_LINK_RE = re.compile(r"(magic-link|/auth/[\w/-]*\btoken=)", re.IGNORECASE)

# Local-parts that only ever send transactional/automated mail.
_TRANSACTIONAL_SENDERS: frozenset[str] = frozenset(
    {"login", "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "mailer-daemon"}
)

_FROM_RE = re.compile(r"^From:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def _from_local_part(markdown: str) -> str:
    m = _FROM_RE.search(markdown)
    if not m:
        return ""
    addr = _ADDR_RE.search(m.group(1))
    if not addr:
        return ""
    return addr.group(0).split("@", 1)[0].strip().lower()


def should_author_mail_markdown(title: str, markdown: str) -> tuple[bool, str | None]:
    """Return (False, reason) for automated/transactional mail, else (True, None)."""
    low = f"{title}\n{markdown}".lower()
    for marker in _TRANSACTIONAL_MARKERS:
        if marker in low:
            return False, "transactional_mail"
    if _MAGIC_LINK_RE.search(markdown):
        return False, "transactional_mail"
    if _from_local_part(markdown) in _TRANSACTIONAL_SENDERS:
        return False, "transactional_mail"
    return True, None
