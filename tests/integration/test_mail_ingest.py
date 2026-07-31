from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import audit_log, auth_identities, corpus_chunks, documents, users, wiki_pages

client = TestClient(app)


def _payload(**overrides):
    body = {
        "backend": "nova",
        "owner_addr": "owner@nova.example",
        "external_id": "ses-1",
        "message_id": "<ses-1@nova.example>",
        "direction": "inbound",
        "from_addr": "Lead <lead@example.com>",
        "to_addr": ["owner@nova.example"],
        "cc_addr": [],
        "subject": "P6 ingest",
        "body_text": "Raw body with jane@example.com must stay unredacted in mail ingest.",
        "body_html": "<p>Raw body</p>",
        "sent_at": "2026-06-10T10:00:00Z",
    }
    body.update(overrides)
    return body


def _signed_request(body: dict, *, bearer: str = "ingest-secret", hmac_secret: str = "hmac-secret"):
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    digest = hmac.new(
        hmac_secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw,
        hashlib.sha256,
    ).hexdigest()
    return raw, {
        "Authorization": f"Bearer {bearer}",
        "X-Orthus-Timestamp": timestamp,
        "X-Orthus-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def _enable_ingest(monkeypatch, user_id):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_ingest_enabled", True)
    monkeypatch.setattr(settings, "mail_ingest_secret", "ingest-secret")
    monkeypatch.setattr(settings, "mail_ingest_hmac_secret", "hmac-secret")
    monkeypatch.setattr(settings, "mail_ingest_service_user_id", str(user_id))
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")


def test_mail_ingest_disabled_returns_404(clean):
    raw, headers = _signed_request(_payload())

    response = client.post("/mail/ingest", content=raw, headers=headers)

    assert response.status_code == 404


def test_mail_ingest_rejects_bad_bearer(clean, user_id, monkeypatch):
    _enable_ingest(monkeypatch, user_id)
    raw, headers = _signed_request(_payload(), bearer="wrong")

    response = client.post("/mail/ingest", content=raw, headers=headers)

    assert response.status_code == 401


def test_mail_ingest_rejects_bad_hmac(clean, user_id, monkeypatch):
    _enable_ingest(monkeypatch, user_id)
    raw, headers = _signed_request(_payload(), hmac_secret="wrong")

    response = client.post("/mail/ingest", content=raw, headers=headers)

    assert response.status_code == 401


def test_mail_ingest_creates_company_document_and_skips_duplicate(clean, user_id, monkeypatch):
    _enable_ingest(monkeypatch, user_id)
    # Company scope now requires an explicitly shared mailbox
    # (docs/p6-mail-individual-scope.md); individual mailboxes ingest personal.
    monkeypatch.setattr(get_settings(), "mail_nova_kind", "shared")
    raw, headers = _signed_request(_payload())

    first = client.post("/mail/ingest", content=raw, headers=headers)
    second = client.post("/mail/ingest", content=raw, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["ingested"] is True
    assert second.json()["ingested"] is False
    assert first.json()["scope"] == "company"
    assert first.json()["source_canonical_id"] == "mail:nova:<ses-1@nova.example>"
    assert first.json()["doc_id"] == second.json()["doc_id"]

    with session() as s:
        rows = s.execute(
            select(
                documents.c.doc_id,
                documents.c.user_id,
                documents.c.source,
                documents.c.source_canonical_id,
                documents.c.scope,
                documents.c.project,
                documents.c.markdown,
            )
        ).all()
        chunks = s.execute(select(corpus_chunks.c.chunk_id)).all()
        wiki = s.execute(select(wiki_pages.c.slug)).all()
        audit_rows = s.execute(
            select(audit_log.c.node, audit_log.c.phase, audit_log.c.meta).where(
                audit_log.c.node == "mail.ingest"
            )
        ).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user_id
    assert row.source == "mail_nova"
    assert row.source_canonical_id == "mail:nova:<ses-1@nova.example>"
    assert row.scope == "company"
    assert row.project == "nova"
    assert "jane@example.com" in row.markdown
    assert chunks
    assert wiki
    assert any(
        item.phase == "exit" and item.meta.get("idempotent_skip") is False for item in audit_rows
    )
    assert any(
        item.phase == "exit" and item.meta.get("idempotent_skip") is True for item in audit_rows
    )


def test_mail_ingest_owner_addr_maps_to_auth_identity(clean, user_id, monkeypatch):
    mapped = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=mapped, display_name="Mapped"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=mapped,
                provider="google",
                provider_subject="mapped",
                email="owner@nova.example",
                email_verified=True,
            )
        )
        s.commit()
    _enable_ingest(monkeypatch, user_id)
    raw, headers = _signed_request(_payload(external_id="ses-2", message_id="<ses-2@nova.example>"))

    response = client.post("/mail/ingest", content=raw, headers=headers)

    assert response.status_code == 200
    with session() as s:
        row = s.execute(select(documents.c.user_id)).first()
    assert row.user_id == mapped


def test_mail_ingest_acme_uses_company_project(clean, user_id, monkeypatch):
    _enable_ingest(monkeypatch, user_id)
    monkeypatch.setattr(get_settings(), "mail_acme_kind", "shared")
    raw, headers = _signed_request(
        _payload(
            backend="acme",
            owner_addr="owner@acme.example",
            external_id="dz-1",
            message_id="<dz-1@acme.example>",
            to_addr=["owner@acme.example"],
        )
    )

    response = client.post("/mail/ingest", content=raw, headers=headers)

    assert response.status_code == 200
    assert response.json()["scope"] == "company"
    with session() as s:
        row = s.execute(select(documents.c.source, documents.c.project)).first()
    assert row.source == "mail_acme"
    assert row.project == "company"


def test_mail_ingest_rejects_gmail_until_p6_4(clean, user_id, monkeypatch):
    _enable_ingest(monkeypatch, user_id)
    raw, headers = _signed_request(_payload(backend="gmail", to_addr=["owner@gmail.com"]))

    response = client.post("/mail/ingest", content=raw, headers=headers)

    assert response.status_code == 422


def test_mail_ingest_individual_unresolvable_owner_returns_422_not_500(clean, user_id, monkeypatch):
    # Default mailbox kind is "individual"; owner@nova.example has no auth_identity, so
    # ingest_mail raises MailIngestConfigError. The endpoint must surface a clean
    # 422 config error, never an uncaught 500.
    _enable_ingest(monkeypatch, user_id)
    monkeypatch.setattr(get_settings(), "mail_nova_kind", "individual")
    raw, headers = _signed_request(_payload(external_id="cfg-1", message_id="<cfg-1@nova.example>"))

    response = client.post("/mail/ingest", content=raw, headers=headers)

    assert response.status_code == 422
    with session() as s:
        rows = s.execute(select(documents.c.doc_id).where(documents.c.source == "mail_nova")).all()
    assert rows == []
