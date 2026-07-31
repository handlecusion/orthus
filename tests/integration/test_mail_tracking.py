"""메일 열람 추적 통합 테스트 — DB 라운드트립 + 공개 픽셀/현황 엔드포인트.

공개 픽셀은 무인증으로 token만으로 open을 기록하고, 현황 조회는 owner 세션이
자기 token만 본다(타 owner/플래그 off는 404). hash-only 저장이라 평문 PII는 없다.
"""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.mail.tracking import create_tracking, get_status, record_open
from orthus.settings import get_settings

client = TestClient(app)
DEMO_USER = UUID("00000000-0000-4000-8000-000000000001")
DEMO_USER_HEADERS = {"X-User-Id": str(DEMO_USER)}
OTHER_USER_HEADERS = {"X-User-Id": "00000000-0000-4000-8000-000000000099"}


def _seed(token: str, owner_id: UUID = DEMO_USER) -> None:
    create_tracking(
        node_id=get_settings().node_id,
        owner_id=owner_id,
        token=token,
        recipient_hash="r" * 64,
        subject_hash="s" * 64,
        sender_kind="nova",
    )


def test_create_record_status_roundtrip(clean):
    _seed("tok-roundtrip")
    node_id = get_settings().node_id

    before = get_status("tok-roundtrip", node_id=node_id, owner_id=DEMO_USER)
    assert before is not None
    assert before.opened is False
    assert before.open_count == 0

    from datetime import UTC, datetime

    t1 = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)
    assert record_open("tok-roundtrip", t1) is True
    after = get_status("tok-roundtrip", node_id=node_id, owner_id=DEMO_USER)
    assert after.opened is True
    assert after.open_count == 1
    assert after.first_opened_at == t1
    assert after.last_opened_at == t1

    # 두 번째 open: count 증가 + last 갱신, first는 불변.
    t2 = datetime(2026, 6, 29, 11, 0, tzinfo=UTC)
    assert record_open("tok-roundtrip", t2) is True
    again = get_status("tok-roundtrip", node_id=node_id, owner_id=DEMO_USER)
    assert again.open_count == 2
    assert again.first_opened_at == t1  # 최초 1회만
    assert again.last_opened_at == t2


def test_record_open_unknown_token_is_noop(clean):
    from datetime import UTC, datetime

    assert record_open("does-not-exist", datetime.now(UTC)) is False


def test_public_pixel_returns_gif_and_records(clean, monkeypatch):
    monkeypatch.setattr(get_settings(), "mail_open_tracking_enabled", True)
    _seed("tok-pixel-aaaaaaaa")

    resp = client.get("/mail/track/open/tok-pixel-aaaaaaaa.gif")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/gif"
    assert "no-store" in resp.headers.get("cache-control", "")
    assert resp.content[:6] == b"GIF89a"

    status = get_status("tok-pixel-aaaaaaaa", node_id=get_settings().node_id, owner_id=DEMO_USER)
    assert status.open_count == 1


def test_public_pixel_flag_off_404(clean, monkeypatch):
    # fail-closed 패리티: 플래그 off면 라우트가 없는 것처럼 404 + DB 무접촉.
    monkeypatch.setattr(get_settings(), "mail_open_tracking_enabled", False)
    _seed("tok-off")

    resp = client.get("/mail/track/open/tok-off.gif")
    assert resp.status_code == 404

    status = get_status("tok-off", node_id=get_settings().node_id, owner_id=DEMO_USER)
    assert status.open_count == 0  # 기록 안 됨


def test_public_pixel_unknown_token_still_gif(clean, monkeypatch):
    # 형태는 토큰이지만 DB에 없는 경우: 200 GIF, 기록은 no-op(매칭 행 없음).
    monkeypatch.setattr(get_settings(), "mail_open_tracking_enabled", True)
    resp = client.get("/mail/track/open/never-seen-bbbbbbbb.gif")
    assert resp.status_code == 200
    assert resp.content[:6] == b"GIF89a"


def test_public_pixel_malformed_token_gif_no_crash(clean, monkeypatch):
    # 형태가 토큰이 아닌(스캐너) 요청도 200 GIF로 응답하되 DB write는 건너뛴다.
    monkeypatch.setattr(get_settings(), "mail_open_tracking_enabled", True)
    resp = client.get("/mail/track/open/" + ("x" * 400) + ".gif")
    assert resp.status_code == 200
    assert resp.content[:6] == b"GIF89a"


def test_status_endpoint_owner_isolation(clean, monkeypatch):
    monkeypatch.setattr(get_settings(), "mail_open_tracking_enabled", True)
    _seed("tok-owned", owner_id=DEMO_USER)

    mine = client.get("/mail/track/status/tok-owned", headers=DEMO_USER_HEADERS)
    assert mine.status_code == 200
    assert mine.json()["token"] == "tok-owned"
    assert mine.json()["opened"] is False

    # 다른 owner는 자기 row가 아니므로 404(존재 비노출).
    other = client.get("/mail/track/status/tok-owned", headers=OTHER_USER_HEADERS)
    assert other.status_code == 404

    missing = client.get("/mail/track/status/no-such", headers=DEMO_USER_HEADERS)
    assert missing.status_code == 404


def test_status_endpoint_flag_off_404(clean, monkeypatch):
    monkeypatch.setattr(get_settings(), "mail_open_tracking_enabled", False)
    _seed("tok-flagoff-status")
    resp = client.get("/mail/track/status/tok-flagoff-status", headers=DEMO_USER_HEADERS)
    assert resp.status_code == 404
