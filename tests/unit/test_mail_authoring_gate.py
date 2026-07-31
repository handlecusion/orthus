"""Mail wiki-authoring gate: skip automated/transactional mail, keep content."""

from orthus.mail.authoring import should_author_mail_markdown
from orthus.wiki.author import _should_author_document

_LOGIN_LINK = """# Mail
Backend: acme
From: Orthus <login@auth.acme.example>
To: owner01@acme.example
Subject: 대리 로그인 링크

## Body
아래 링크로 로그인하세요.

https://orthus-central.example.com/api/auth/magic-link/consume?token=ABC123
이 링크는 한 번만 사용할 수 있습니다.
"""

_OTP = """# Mail
From: noreply@example.com
Subject: 인증 코드 안내

## Body
요청하신 인증 코드는 482913 입니다.
"""

_TEST_SEND = """# Mail
From: owner01@acme.example
Subject: [orthus P6.3 D13 test-send]

## Body
self-test ping.
"""

_CONTENT_THREAD = """# Mail
From: Jaden <jaden@nova.example>
To: team@nova.example
Subject: 다음 주 아틀라스 일정 공유

## Body
다음 주 아틀라스 일정은 화요일 오후 2시입니다. 장소는 강남 스튜디오이고,
배우 3명과 감독이 참석합니다. 준비물은 대본과 의상 레퍼런스입니다.
"""

_CI_NOTIFICATION = """# Mail
From: GitHub <notifications@github.com>
Subject: [chopratejas/headroom] Run failed: CI, Attempt #2

## Body
The CI workflow failed on the fix/615-process-leak branch at commit 0da2fa4.
The process-leak integration test timed out after 600 seconds.
"""


def test_login_magic_link_is_skipped():
    allowed, reason = should_author_mail_markdown("Mail: 대리 로그인 링크", _LOGIN_LINK)
    assert allowed is False
    assert reason == "transactional_mail"


def test_otp_verification_is_skipped():
    allowed, _ = should_author_mail_markdown("Mail: 인증 코드 안내", _OTP)
    assert allowed is False


def test_test_send_is_skipped():
    allowed, _ = should_author_mail_markdown("Mail: [orthus P6.3 D13 test-send]", _TEST_SEND)
    assert allowed is False


def test_content_thread_is_authored():
    allowed, reason = should_author_mail_markdown("Mail: 다음 주 아틀라스 일정 공유", _CONTENT_THREAD)
    assert allowed is True
    assert reason is None


def test_ci_notification_is_authored():
    # Automated sender, but real knowledge content + no transactional marker.
    allowed, _ = should_author_mail_markdown(
        "Mail: [chopratejas/headroom] Run failed: CI, Attempt #2", _CI_NOTIFICATION
    )
    assert allowed is True


def test_wired_into_should_author_document():
    # mail_* sources route through the mail gate; other sources are unaffected.
    assert _should_author_document("mail_acme", "Mail: 대리 로그인 링크", _LOGIN_LINK) == (
        False,
        "transactional_mail",
    )
    assert _should_author_document("mail_nova", "Mail: 일정 공유", _CONTENT_THREAD) == (True, None)
    assert _should_author_document("notion", "Anything", "body") == (True, None)
