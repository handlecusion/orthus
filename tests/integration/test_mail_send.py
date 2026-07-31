from __future__ import annotations

import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from orthus.api.main import app
from orthus.api.routes import mail as mail_routes
from orthus.db import session
from orthus.schemas.canonical import MailSendResult
from orthus.settings import get_settings
from orthus.tables import email_send_log

client = TestClient(app)
DEMO_USER = "00000000-0000-4000-8000-000000000001"
DEMO_HEADERS = {"X-User-Id": DEMO_USER}


def _email_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _enable_send(monkeypatch, *, acme: bool = True, nova: bool = True) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_send_enabled", True)
    monkeypatch.setattr(settings, "mail_acme_base_url", "https://dz.test")
    monkeypatch.setattr(
        settings, "mail_acme_send_token", "dz-send-token" if acme else ""
    )
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test/v0")
    monkeypatch.setattr(settings, "mail_nova_api_key", "nova-key" if nova else "")
    monkeypatch.setattr(settings, "mail_nova_owner", "owner@nova.example")
    monkeypatch.setattr(settings, "mail_acme_owner", "owner@acme.example")


def _body(**overrides) -> dict:
    payload = {
        "from_addr": "lead@acme.example",
        "to": "dest@example.com",
        "subject": "P6.3 manual",
        "text": "Hello from orthus.",
    }
    payload.update(overrides)
    return payload


def _stub_send(monkeypatch, result: MailSendResult) -> dict:
    seen: dict = {}

    async def fake_send_mail(payload, settings, *, owner_id=None, client=None):  # noqa: ANN001
        seen["payload"] = payload
        seen["owner_id"] = owner_id
        return result

    monkeypatch.setattr(mail_routes, "send_mail", fake_send_mail)
    return seen


def test_mail_send_kill_switch_off_returns_404(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_send_enabled", False)

    res = client.post("/mail/send", headers=DEMO_HEADERS, json=_body())

    assert res.status_code == 404


def test_mail_send_personal_node_returns_404(clean, monkeypatch):
    _enable_send(monkeypatch)
    monkeypatch.setattr(get_settings(), "node_kind", "personal")

    res = client.post("/mail/send", headers=DEMO_HEADERS, json=_body())

    assert res.status_code == 404


def test_mail_send_demo_role_rejected(clean, monkeypatch):
    # demo header => role "demo", which is rejected for real external send.
    _enable_send(monkeypatch)
    _stub_send(monkeypatch, MailSendResult(backend="acme", status="sent"))

    res = client.post("/mail/send", headers=DEMO_HEADERS, json=_body())

    assert res.status_code == 403
    assert res.json()["detail"] == "node operator required"
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 0


def test_mail_send_collector_token_cannot_reach_route(clean, monkeypatch):
    # The route only uses session/demo/jwt identity (get_current_user) — never the
    # collector-token dual-auth path. In session mode a collector Bearer is not a
    # session cookie, so it is rejected before any send.
    _enable_send(monkeypatch)
    monkeypatch.setattr(get_settings(), "auth_mode", "session")

    res = client.post(
        "/mail/send",
        headers={"Authorization": f"Bearer dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"},
        json=_body(),
    )

    assert res.status_code == 401


def test_mail_send_unconfigured_backend_returns_422(clean, monkeypatch):
    # acme send token unset => the resolved backend is send-unconfigured.
    _enable_send(monkeypatch, acme=False)
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        res = client.post("/mail/send", json=_body())
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert res.status_code == 422
    assert "acme send not configured" in res.json()["detail"]


def _owner_override():
    from orthus.auth import AuthenticatedUser

    return lambda: AuthenticatedUser(
        user_id=uuid.UUID(DEMO_USER),
        auth_mode="session",
        display_name="Owner",
        role="owner",
        node_id="company",
    )


def test_mail_send_success_logs_hash_only_manual_origin(clean, monkeypatch):
    _enable_send(monkeypatch)
    _stub_send(
        monkeypatch,
        MailSendResult(backend="acme", status="sent", provider_message_id="re_abc"),
    )
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        res = client.post("/mail/send", json=_body(to="recipient@example.com"))
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert res.status_code == 200
    payload = res.json()
    assert payload["backend"] == "acme"
    assert payload["status"] == "sent"
    # Provider id is masked (hashed), not echoed raw.
    assert payload["provider_message_id"] == _email_hash("re_abc")
    assert payload["provider_message_id"] != "re_abc"

    with session() as s:
        row = s.execute(select(email_send_log)).first()
    assert row is not None
    log = row._mapping
    assert log["origin"] == "manual"
    assert log["work_id"] is None
    assert log["sender_kind"] == "acme"
    assert log["status"] == "sent"
    assert log["recipient_hash"] == _email_hash("recipient@example.com")
    assert "recipient@example.com" not in str(dict(log))
    assert "P6.3 manual" not in str(dict(log))


def _enable_tracking(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_open_tracking_enabled", True)
    monkeypatch.setattr(settings, "mail_tracking_pixel_base_url", "https://orthus.test/api")


def test_mail_send_tracking_injects_pixel_and_returns_token(clean, monkeypatch):
    _enable_send(monkeypatch)
    _enable_tracking(monkeypatch)
    seen = _stub_send(monkeypatch, MailSendResult(backend="acme", status="sent"))
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        res = client.post("/mail/send", json=_body(to="r@example.com", html="<p>hi</p>"))
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert res.status_code == 200
    token = res.json()["tracking_token"]
    assert token  # 발송 성공 + 추적 켜짐 → 토큰 반환
    # 실제로 send_mail에 넘어간 본문에 그 토큰 픽셀이 들어갔다.
    assert f"/mail/track/open/{token}.gif" in seen["payload"].html


def test_mail_send_track_false_suppresses_pixel(clean, monkeypatch):
    _enable_send(monkeypatch)
    _enable_tracking(monkeypatch)
    seen = _stub_send(monkeypatch, MailSendResult(backend="acme", status="sent"))
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        res = client.post(
            "/mail/send", json=_body(to="r@example.com", html="<p>hi</p>", track=False)
        )
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert res.status_code == 200
    assert res.json()["tracking_token"] is None  # per-message opt-out
    assert "track/open" not in (seen["payload"].html or "")


def test_mail_send_nova_success(clean, monkeypatch):
    _enable_send(monkeypatch)
    _stub_send(
        monkeypatch, MailSendResult(backend="nova", status="sent", provider_message_id="s9")
    )
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        res = client.post("/mail/send", json=_body(from_addr="owner@nova.example"))
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert res.status_code == 200
    assert res.json()["backend"] == "nova"
    with session() as s:
        row = s.execute(select(email_send_log)).first()
    assert row._mapping["sender_kind"] == "nova"
    assert row._mapping["origin"] == "manual"


def test_mail_send_failed_result_logs_failed(clean, monkeypatch):
    _enable_send(monkeypatch)
    _stub_send(
        monkeypatch,
        MailSendResult(backend="acme", status="failed", error="403 forbidden"),
    )
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        res = client.post("/mail/send", json=_body())
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert res.status_code == 200
    assert res.json()["status"] == "failed"
    assert res.json()["error"] == "403 forbidden"
    with session() as s:
        row = s.execute(select(email_send_log)).first()
    assert row._mapping["status"] == "failed"
    assert row._mapping["origin"] == "manual"


def test_mail_send_rate_limit_second_send_returns_429(clean, monkeypatch):
    _enable_send(monkeypatch)
    _stub_send(
        monkeypatch,
        MailSendResult(backend="acme", status="sent", provider_message_id="re_1"),
    )
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        first = client.post("/mail/send", json=_body(to="same@example.com"))
        second = client.post("/mail/send", json=_body(to="same@example.com"))
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert first.status_code == 200
    assert second.status_code == 429
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 1


def test_mail_send_routing_reject_returns_422(clean, monkeypatch):
    _enable_send(monkeypatch)
    app.dependency_overrides[mail_routes.get_current_user] = _owner_override()
    try:
        res = client.post("/mail/send", json=_body(from_addr="me@gmail.com"))
    finally:
        app.dependency_overrides.pop(mail_routes.get_current_user, None)

    assert res.status_code == 422
    assert "not routable" in res.json()["detail"]
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 0


def test_mail_inbox_exposes_send_flags(clean, monkeypatch):
    _enable_send(monkeypatch)
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)

    res = client.get("/mail/inbox", headers=DEMO_HEADERS)

    assert res.status_code == 200
    payload = res.json()
    assert payload["send_enabled"] is True
    flags = {row["backend"]: row["send_configured"] for row in payload["backends"]}
    assert flags["acme"] is True
    assert flags["nova"] is True
    assert flags["gmail"] is False
    owners = {row["backend"]: row["owner_addr"] for row in payload["backends"]}
    assert owners["acme"] == "owner@acme.example"
    assert owners["nova"] == "owner@nova.example"
    assert owners["gmail"] is None
