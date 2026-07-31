from __future__ import annotations

import uuid

import httpx
from sqlalchemy import insert, select

from orthus.db import session
from orthus.mail.ingest import ingest_scope_for_backend, mail_source_canonical_id
from orthus.mail.pull import pull_ingest_all, pull_ingest_backend
from orthus.schemas.canonical import MailIngestRequest
from orthus.settings import get_settings
from orthus.tables import auth_identities, corpus_chunks, documents, users, wiki_pages

NOVA_INBOX = {
    "total": 1,
    "unread": 1,
    "emails": [
        {
            "id": "nova-msg-1",
            "message_id": "<nova-msg-1@nova.example>",
            "from_addr": "Lead <lead@example.com>",
            "to_addr": ["owner@nova.example"],
            "subject": "Nova inbound",
            "created_at": "2026-06-10T10:00:00Z",
        }
    ],
}
NOVA_MESSAGE = {
    "id": "nova-msg-1",
    "message_id": "<nova-msg-1@nova.example>",
    "from_addr": "Lead <lead@example.com>",
    "to_addr": ["owner@nova.example"],
    "subject": "Nova inbound",
    "body_text": "Full body with jane@example.com kept unredacted per mail ingest.",
    "body_html": "<p>Full body</p>",
    "created_at": "2026-06-10T10:00:00Z",
}
DZ_INBOX = {
    "total": 1,
    "unread": 0,
    "emails": [
        {
            "id": "dz-msg-1",
            "from_addr": "Partner <partner@example.com>",
            "to_addr": ["owner@acme.example"],
            "subject": "DZ inbound",
            "received_at": "2026-06-10T11:00:00Z",
        }
    ],
}
DZ_MESSAGE = {
    "email": {
        "id": "dz-msg-1",
        "from_addr": "Partner <partner@example.com>",
        "to_addr": ["owner@acme.example"],
        "subject": "DZ inbound",
        "body_text": "DZ full body.",
        "received_at": "2026-06-10T11:00:00Z",
    }
}


def _enable_pull(settings, user_id):
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.mail_pull_ingest_enabled = True
    settings.mail_ingest_service_user_id = str(user_id)
    # These tests cover the shared-mailbox pull path that stays company-scope
    # (docs/p6-mail-individual-scope.md); an individual mailbox would ingest as
    # personal and is covered in test_mail_scope_boundary.py.
    settings.mail_nova_kind = "shared"
    settings.mail_acme_kind = "shared"
    settings.mail_nova_base_url = "https://api.nova.example/v0"
    settings.mail_nova_api_key = "nova-key"
    settings.mail_nova_owner = "owner@nova.example"
    settings.mail_acme_base_url = "https://mail-api.acme.example"
    settings.mail_acme_api_token = "dz-token"
    settings.mail_acme_owner = "owner@acme.example"


def _nova_client(*, fail_message: bool = False) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mail/inbox"):
            return httpx.Response(200, json=NOVA_INBOX)
        if request.url.path.endswith("/mail/nova-msg-1"):
            if fail_message:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=NOVA_MESSAGE)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _dz_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mail/inbox"):
            return httpx.Response(200, json=DZ_INBOX)
        if request.url.path.endswith("/mail/dz-msg-1"):
            return httpx.Response(200, json=DZ_MESSAGE)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pull_ingest_nova_creates_company_document(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)

    result = pull_ingest_backend("nova", settings, client=_nova_client())

    assert result.enabled is True
    assert (result.listed, result.ingested, result.skipped, result.errors) == (1, 1, 0, 0)

    with session() as s:
        rows = s.execute(
            select(
                documents.c.source,
                documents.c.source_canonical_id,
                documents.c.scope,
                documents.c.project,
                documents.c.markdown,
                documents.c.user_id,
            )
        ).all()
        chunks = s.execute(select(corpus_chunks.c.chunk_id)).all()
        wiki = s.execute(select(wiki_pages.c.slug)).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.source == "mail_nova"
    assert row.source_canonical_id == "mail:nova:<nova-msg-1@nova.example>"
    assert row.scope == "company"
    assert row.project == "nova"
    assert row.user_id == user_id
    assert "jane@example.com" in row.markdown
    assert chunks
    assert wiki


def test_pull_ingest_is_idempotent_on_second_run(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)

    first = pull_ingest_backend("nova", settings, client=_nova_client())
    second = pull_ingest_backend("nova", settings, client=_nova_client())

    assert (first.ingested, first.skipped) == (1, 0)
    assert (second.listed, second.ingested, second.skipped, second.errors) == (1, 0, 1, 0)

    with session() as s:
        count = s.execute(select(documents.c.doc_id)).all()
    assert len(count) == 1


def test_pull_ingest_acme_uses_company_project(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)

    result = pull_ingest_backend("acme", settings, client=_dz_client())

    assert (result.listed, result.ingested, result.errors) == (1, 1, 0)
    with session() as s:
        row = s.execute(select(documents.c.source, documents.c.project, documents.c.scope)).first()
    assert row.source == "mail_acme"
    assert row.project == "company"
    assert row.scope == "company"


def test_pull_ingest_one_failing_message_does_not_abort(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)

    result = pull_ingest_backend("nova", settings, client=_nova_client(fail_message=True))

    assert result.enabled is True
    assert result.listed == 1
    assert result.ingested == 0
    assert result.errors == 1
    with session() as s:
        rows = s.execute(select(documents.c.doc_id)).all()
    assert rows == []


def test_pull_ingest_flag_off_is_noop(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)
    settings.mail_pull_ingest_enabled = False

    result = pull_ingest_backend("nova", settings, client=_nova_client())

    assert result.enabled is False
    assert result.listed == 0
    with session() as s:
        rows = s.execute(select(documents.c.doc_id)).all()
    assert rows == []


def test_pull_ingest_personal_node_refuses(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)
    settings.node_kind = "personal"

    result = pull_ingest_backend("nova", settings, client=_nova_client())

    assert result.enabled is False
    with session() as s:
        rows = s.execute(select(documents.c.doc_id)).all()
    assert rows == []


def test_pull_ingest_all_runs_both_company_backends(clean, user_id, monkeypatch):
    settings = get_settings()
    _enable_pull(settings, user_id)

    clients = {"nova": _nova_client(), "acme": _dz_client()}
    real = pull_ingest_backend

    def routed(backend, s, **kwargs):
        kwargs.setdefault("client", clients.get(backend))
        return real(backend, s, **kwargs)

    monkeypatch.setattr("orthus.mail.pull.pull_ingest_backend", routed)

    results = pull_ingest_all(settings)

    by_backend = {r.backend: r for r in results}
    assert set(by_backend) == {"nova", "acme"}
    assert by_backend["nova"].ingested == 1
    assert by_backend["acme"].ingested == 1
    with session() as s:
        rows = s.execute(select(documents.c.source)).all()
    assert {r.source for r in rows} == {"mail_nova", "mail_acme"}


def _register_owner(email: str) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Owner"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=uid,
                provider="test",
                provider_subject=f"sub-{uid}",
                email=email,
                email_verified=True,
            )
        )
        s.commit()
    return uid


def test_pull_ingest_individual_mailbox_is_personal_owner_bound(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)
    # Individual employee mailbox: ingest must land as scope=personal bound to the
    # resolved owner, not company-scope; the pull idempotency scope must agree.
    settings.mail_nova_kind = "individual"
    settings.mail_nova_owner = "owner@nova.example"
    owner = _register_owner("owner@nova.example")

    result = pull_ingest_backend("nova", settings, client=_nova_client())

    assert result.enabled is True
    assert (result.listed, result.ingested, result.skipped, result.errors) == (1, 1, 0, 0)

    # Idempotency scope the pull path used must match the ingest write scope.
    assert ingest_scope_for_backend("nova", settings) == "personal"

    with session() as s:
        row = s.execute(select(documents.c.scope, documents.c.user_id, documents.c.source)).first()
    assert row is not None
    assert row.source == "mail_nova"
    assert row.scope == "personal"
    assert row.user_id == owner

    # Second run is idempotent under the same (source, scope, canonical_id) key.
    summary = MailIngestRequest(
        backend="nova",
        owner_addr="owner@nova.example",
        external_id="nova-msg-1",
        message_id="<nova-msg-1@nova.example>",
        direction="inbound",
        from_addr="Lead <lead@example.com>",
        to_addr=["owner@nova.example"],
        subject="Nova inbound",
    )
    assert mail_source_canonical_id(summary) == "mail:nova:<nova-msg-1@nova.example>"
    second = pull_ingest_backend("nova", settings, client=_nova_client())
    assert (second.ingested, second.skipped) == (0, 1)
    with session() as s:
        count = s.execute(select(documents.c.doc_id)).all()
    assert len(count) == 1


def _recording_client(*, inbox_read: int | None, patch_status: int = 200):
    """Nova-shaped mock that records every request; inbox row carries `read`."""
    requests: list[tuple[str, str, bytes]] = []
    inbox_row: dict[str, object] = dict(NOVA_INBOX["emails"][0])
    if inbox_read is not None:
        inbox_row["read"] = inbox_read

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/mail/inbox"):
            return httpx.Response(200, json={"total": 1, "unread": 1, "emails": [inbox_row]})
        if request.url.path.endswith("/mail/nova-msg-1"):
            if request.method == "PATCH":
                return httpx.Response(patch_status, json={"status": "updated"})
            return httpx.Response(200, json=NOVA_MESSAGE)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def test_pull_ingest_restores_unread_after_body_fetch(clean, user_id):
    # The provider detail GET auto-marks the message read; the ingest body fetch
    # is not a user open, so pull must PATCH the unread flag back.
    settings = get_settings()
    _enable_pull(settings, user_id)
    client, requests = _recording_client(inbox_read=0)

    result = pull_ingest_backend("nova", settings, client=client)

    assert (result.ingested, result.errors) == (1, 0)
    patches = [r for r in requests if r[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0][1].endswith("/mail/nova-msg-1")
    assert b'"read": 0' in patches[0][2] or b'"read":0' in patches[0][2]
    # Restore must come after the detail GET that flipped the flag.
    get_idx = next(
        i for i, r in enumerate(requests) if r[0] == "GET" and r[1].endswith("/mail/nova-msg-1")
    )
    patch_idx = requests.index(patches[0])
    assert patch_idx > get_idx


def test_pull_ingest_leaves_read_message_flags_alone(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)
    client, requests = _recording_client(inbox_read=1)

    result = pull_ingest_backend("nova", settings, client=client)

    assert (result.ingested, result.errors) == (1, 0)
    assert [r for r in requests if r[0] == "PATCH"] == []


def test_pull_ingest_missing_read_field_does_not_restore(clean, user_id):
    # Without list-level unread evidence we must not flip a read message back.
    settings = get_settings()
    _enable_pull(settings, user_id)
    client, requests = _recording_client(inbox_read=None)

    result = pull_ingest_backend("nova", settings, client=client)

    assert (result.ingested, result.errors) == (1, 0)
    assert [r for r in requests if r[0] == "PATCH"] == []


def test_pull_ingest_unread_restore_failure_does_not_abort(clean, user_id):
    settings = get_settings()
    _enable_pull(settings, user_id)
    client, requests = _recording_client(inbox_read=0, patch_status=500)

    result = pull_ingest_backend("nova", settings, client=client)

    # Restore is best-effort: the message still ingests and is not an error.
    assert (result.ingested, result.errors) == (1, 0)
    assert len([r for r in requests if r[0] == "PATCH"]) == 1
