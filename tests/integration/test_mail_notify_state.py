"""Desktop new-mail signal (`mail_notify_state`).

The cheap `/mail/notify-state` poll counts only mail documents the caller can see
— company-scope mail plus the caller's own personal (individual-mailbox) mail —
and never leaks another user's personal mail into the count, the latest time, or
the latest subject. Non-mail documents and personal Gmail (`gws_gmail`) are
excluded so a frequent poll stays a tight, local count.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import insert

from orthus.db import session
from orthus.mail.notify import mail_notify_state
from orthus.tables import documents, users


def _user(display_name: str) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name=display_name))
        s.commit()
    return uid


def _doc(*, source: str, scope: str, user_id: uuid.UUID, title: str, created_at: datetime) -> None:
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=uuid.uuid4(),
                user_id=user_id,
                title=title,
                block_json={},
                markdown="",
                source=source,
                schema_version=1,
                scope=scope,
                created_at=created_at,
            )
        )
        s.commit()


def _seed(owner: uuid.UUID, other: uuid.UUID) -> None:
    t = lambda d: datetime(2026, 1, d, tzinfo=UTC)  # noqa: E731
    # Visible to owner: company mail + owner's personal mail.
    _doc(source="mail_nova", scope="company", user_id=other, title="회사 메일 A", created_at=t(1))
    _doc(
        source="mail_acme",
        scope="personal",
        user_id=owner,
        title="개인 메일 B",
        created_at=t(3),
    )
    # NOT visible to owner: another user's personal mail (newest overall -> must be filtered).
    _doc(
        source="mail_nova",
        scope="personal",
        user_id=other,
        title="남의 개인 메일 C",
        created_at=t(5),
    )
    # Excluded regardless of scope: personal Gmail projection + non-mail documents.
    _doc(source="gws_gmail", scope="personal", user_id=owner, title="Gmail: 개인", created_at=t(4))
    _doc(source="notion", scope="company", user_id=other, title="노션 문서", created_at=t(2))


def test_notify_state_counts_only_visible_mail(clean):
    owner, other = _user("owner"), _user("other")
    _seed(owner, other)

    state = mail_notify_state(owner)

    # A (company) + B (owner personal); excludes C, D(gmail), E(notion).
    assert state.total == 2
    # Latest visible is B on 01-03 — NOT C on 01-05 (another user's personal).
    assert state.latest_at == datetime(2026, 1, 3, tzinfo=UTC)
    assert state.latest_title == "개인 메일 B"


def test_notify_state_isolates_other_users_personal_mail(clean):
    owner, other = _user("owner"), _user("other")
    _seed(owner, other)

    state = mail_notify_state(other)

    # A (company) + C (other's own personal); still excludes owner's personal B.
    assert state.total == 2
    assert state.latest_at == datetime(2026, 1, 5, tzinfo=UTC)
    assert state.latest_title == "남의 개인 메일 C"


def test_notify_state_empty_when_no_mail(clean):
    state = mail_notify_state(uuid.uuid4())

    assert state.total == 0
    assert state.latest_at is None
    assert state.latest_title is None
