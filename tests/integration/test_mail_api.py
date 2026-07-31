from __future__ import annotations

from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.api.routes import mail as mail_routes
from orthus.schemas.canonical import CanonicalEmail, MailBackendStatus, MailInboxResponse
from orthus.settings import get_settings

client = TestClient(app)
DEMO_USER_HEADERS = {"X-User-Id": "00000000-0000-4000-8000-000000000001"}


def test_mail_inbox_unconfigured_backends_fail_closed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", False)
    monkeypatch.setattr(settings, "mail_nova_owner", "")
    monkeypatch.setattr(settings, "mail_nova_api_key", "")
    monkeypatch.setattr(settings, "mail_nova_api_key_secret_ref", "")
    monkeypatch.setattr(settings, "mail_acme_owner", "")
    monkeypatch.setattr(settings, "mail_acme_api_token", "")
    monkeypatch.setattr(settings, "mail_acme_api_token_secret_ref", "")
    monkeypatch.setattr(settings, "mail_acme_session", "")
    monkeypatch.setattr(settings, "mail_acme_session_secret_ref", "")
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    response = client.get("/mail/inbox", headers=DEMO_USER_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["unread"] == 0
    assert [(row["backend"], row["configured"], row["ok"]) for row in payload["backends"]] == [
        ("nova", False, True),
        ("acme", False, True),
        ("gmail", False, True),
    ]


def test_mail_inbox_limit_validation():
    response = client.get("/mail/inbox?limit=501", headers=DEMO_USER_HEADERS)

    assert response.status_code == 422


def test_mail_inbox_rejects_anonymous_session_request(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")

    response = client.get("/mail/inbox")

    assert response.status_code == 401


def test_mail_inbox_returns_mixed_backend_rows(monkeypatch):
    async def fake_list_unified_inbox(*_args, **_kwargs):
        return MailInboxResponse(
            items=[
                CanonicalEmail(
                    backend="nova",
                    external_id="s1",
                    scope="company",
                    from_addr="lead@nova.example",
                    to_addr=["owner@nova.example"],
                    subject="company mail",
                ),
                CanonicalEmail(
                    backend="gmail",
                    external_id="g1",
                    scope="personal",
                    from_addr="friend@example.com",
                    to_addr=["owner@gmail.com"],
                    subject="personal mail",
                ),
            ],
            total=2,
            unread=1,
            backends=[
                MailBackendStatus(backend="nova", configured=True, ok=True, total=1, unread=1),
                MailBackendStatus(backend="acme", configured=False, ok=True),
                MailBackendStatus(backend="gmail", configured=True, ok=True, total=1, unread=0),
            ],
        )

    monkeypatch.setattr(mail_routes, "list_unified_inbox", fake_list_unified_inbox)

    response = client.get("/mail/inbox", headers=DEMO_USER_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert [(row["backend"], row["scope"]) for row in payload["items"]] == [
        ("nova", "company"),
        ("gmail", "personal"),
    ]
    assert payload["total"] == 2


# --- company-mail single detail + triage proxy (Gmail-style UX slices 1-2) ----

from orthus.mail.backends import MailBackendConfig  # noqa: E402

_FAKE_CFG = MailBackendConfig(
    backend="acme",
    base_url="https://mail.example.test",
    owner="biz@acme.example",
    bearer_token="x",
)


def test_mail_message_detail_proxy(monkeypatch):
    async def fake_detail(cfg, external_id, _client):
        assert external_id == "abc123"
        return CanonicalEmail(
            backend="acme",
            external_id=external_id,
            scope="company",
            from_addr="lead@acme.example",
            to_addr=["biz@acme.example"],
            subject="hello",
            body_text="full body",
            read=True,
        )

    monkeypatch.setattr(mail_routes, "resolve_backend_config", lambda *a, **k: _FAKE_CFG)
    monkeypatch.setattr(mail_routes, "fetch_backend_email_detail", fake_detail)

    res = client.get("/mail/message?backend=acme&id=abc123", headers=DEMO_USER_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["subject"] == "hello"
    assert body["body_text"] == "full body"


def test_mail_message_unconfigured_backend_404(monkeypatch):
    monkeypatch.setattr(mail_routes, "resolve_backend_config", lambda *a, **k: None)
    res = client.get("/mail/message?backend=acme&id=abc123", headers=DEMO_USER_HEADERS)
    assert res.status_code == 404


def test_mail_message_gmail_is_read_only_404(monkeypatch):
    # gmail never resolves through the company-backend proxy (read-only P6.1b).
    monkeypatch.setattr(mail_routes, "resolve_backend_config", lambda *a, **k: None)
    res = client.get("/mail/message?backend=gmail&id=g1", headers=DEMO_USER_HEADERS)
    assert res.status_code == 404


def test_mail_message_patch_flags(monkeypatch):
    seen = {}

    async def fake_mutate(cfg, external_id, _client, *, read=None, starred=None, label=None):
        seen.update(id=external_id, read=read, starred=starred)
        return True

    monkeypatch.setattr(mail_routes, "resolve_backend_config", lambda *a, **k: _FAKE_CFG)
    monkeypatch.setattr(mail_routes, "mutate_backend_email", fake_mutate)

    res = client.patch(
        "/mail/message?backend=acme&id=abc123",
        json={"starred": True, "read": False},
        headers=DEMO_USER_HEADERS,
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert seen == {"id": "abc123", "read": False, "starred": True}


def test_mail_message_trash_proxy(monkeypatch):
    async def fake_trash(cfg, external_id, _client, *, restore=False):
        return not restore or external_id == "abc123"

    monkeypatch.setattr(mail_routes, "resolve_backend_config", lambda *a, **k: _FAKE_CFG)
    monkeypatch.setattr(mail_routes, "trash_backend_email", fake_trash)

    res = client.post("/mail/message/trash?backend=acme&id=abc123", headers=DEMO_USER_HEADERS)
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_mail_conversation_proxy(monkeypatch):
    async def fake_conversation(cfg, contact, _client):
        assert contact == "person@example.com"
        return [
            CanonicalEmail(
                backend="acme",
                external_id="m1",
                scope="company",
                from_addr="person@example.com",
                to_addr=["biz@acme.example"],
                subject="first",
                direction="inbound",
            ),
            CanonicalEmail(
                backend="acme",
                external_id="m2",
                scope="company",
                from_addr="biz@acme.example",
                to_addr=["person@example.com"],
                subject="re: first",
                direction="outbound",
            ),
        ]

    monkeypatch.setattr(mail_routes, "resolve_backend_config", lambda *a, **k: _FAKE_CFG)
    monkeypatch.setattr(mail_routes, "fetch_backend_conversation", fake_conversation)

    res = client.get(
        "/mail/conversation?backend=acme&contact=person@example.com",
        headers=DEMO_USER_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert [m["external_id"] for m in body] == ["m1", "m2"]
    assert [m["direction"] for m in body] == ["inbound", "outbound"]


def test_normalize_parses_replied_flag():
    from orthus.mail.backends import normalize_backend_email

    email = normalize_backend_email(
        "acme",
        {"id": "x", "from_addr": "a@b.com", "subject": "s", "replied": 1},
        owner_addr="biz@acme.example",
    )
    assert email.replied is True
