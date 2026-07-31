from __future__ import annotations

from orthus.mail.pull import (
    _to_ingest_request,
    pull_ingest_all,
    pull_ingest_backend,
)
from orthus.mail.backends import MailBackendConfig
from orthus.settings import get_settings


def _configure_nova(settings) -> None:
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.mail_pull_ingest_enabled = True
    settings.mail_nova_base_url = "https://api.nova.example/v0"
    settings.mail_nova_api_key = "nova-key"
    settings.mail_nova_owner = "owner@nova.example"


def test_pull_ingest_disabled_flag_is_noop(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", False)
    monkeypatch.setattr(settings, "node_kind", "company")

    result = pull_ingest_backend("nova", settings)

    assert result.enabled is False
    assert (result.listed, result.ingested, result.skipped, result.errors) == (0, 0, 0, 0)


def test_pull_ingest_personal_node_refuses(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "personal")

    result = pull_ingest_backend("nova", settings)

    assert result.enabled is False


def test_pull_ingest_gmail_backend_not_allowed(monkeypatch):
    settings = get_settings()
    _configure_nova(settings)
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", True)

    result = pull_ingest_backend("gmail", settings)

    assert result.enabled is False


def test_pull_ingest_unconfigured_backend_is_noop(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_nova_api_key", "")
    monkeypatch.setattr(settings, "mail_nova_api_key_secret_ref", "")

    result = pull_ingest_backend("nova", settings)

    assert result.enabled is False


def test_pull_ingest_all_skips_gmail_backend(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", False)
    monkeypatch.setattr(settings, "node_kind", "company")

    results = pull_ingest_all(settings)

    backends = [r.backend for r in results]
    assert backends == ["nova", "acme"]


def test_to_ingest_request_prefers_message_id_for_canonical():
    cfg = MailBackendConfig(
        backend="nova",
        base_url="https://api.nova.example/v0",
        owner="owner@nova.example",
        bearer_token="nova-key",
    )
    row = {
        "id": "msg-1",
        "message_id": "<msg-1@nova.example>",
        "from_addr": "lead@example.com",
        "to_addr": ["owner@nova.example"],
        "subject": "Hi",
        "body_text": "body",
        "created_at": "2026-06-10T10:00:00Z",
    }

    payload = _to_ingest_request("nova", cfg, row)

    assert payload.external_id == "msg-1"
    assert payload.message_id == "<msg-1@nova.example>"
    assert payload.owner_addr == "owner@nova.example"
    assert payload.sent_at is not None


def test_listed_unread_requires_explicit_falsy_read_field():
    from orthus.mail.pull import _listed_unread

    assert _listed_unread({"read": 0}) is True
    assert _listed_unread({"read": False}) is True
    assert _listed_unread({"read": "0"}) is True
    assert _listed_unread({"read": "false"}) is True
    assert _listed_unread({"read": 1}) is False
    assert _listed_unread({"read": True}) is False
    assert _listed_unread({"read": "1"}) is False
    # Missing/unknown -> never flip an already-read message back to unread.
    assert _listed_unread({}) is False
    assert _listed_unread({"read": None}) is False
