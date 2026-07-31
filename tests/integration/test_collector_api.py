from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from orthus.api.main import app
from orthus.collector.auth import (
    ALLOWED_SCOPES,
    effective_scopes,
    hash_collector_token,
)
from orthus.collector.rate_limit import reset_collector_owner_rate_limits
from orthus.db import session
from orthus.schemas.canonical import SCHEMA_VERSION, WikiClaim, WikiPage, WikiSource, WikiTask
from orthus.settings import get_settings
from orthus.tables import (
    audit_log,
    auth_identities,
    connector_items,
    collector_tokens,
    collector_commands,
    connector_accounts,
    corpus_chunks,
    documents,
    embeddings,
    structured_rows,
    users,
    wiki_pages,
)
from orthus.wiki import store
from orthus.wiki.slugs import source_slug_from_title

client = TestClient(app)

COMPANY_NODE = "company"


def _enable(monkeypatch) -> None:
    reset_collector_owner_rate_limits()
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)


def _make_token(
    user_id,
    *,
    node_id: str = COMPANY_NODE,
    revoked: bool = False,
    scopes: list[str] | None = None,
) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    values = dict(
        token_id=uuid.uuid4(),
        user_id=user_id,
        node_id=node_id,
        name="test",
        token_hash=hash_collector_token(token),
        revoked_at="2026-01-01T00:00:00Z" if revoked else None,
    )
    # When scopes is None, rely on the DB server_default (`{ingest}`) — this is
    # exactly the pre-P8.7a token shape the backcompat path must keep working.
    if scopes is not None:
        values["scopes"] = scopes
    with session() as s:
        s.execute(insert(collector_tokens).values(**values))
        s.commit()
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _doc(**overrides) -> dict:
    body = {
        "source": "gws_gmail",
        "source_external_id": "msg-1",
        "title": "Personal note",
        "content_md": "owner-only body",
    }
    body.update(overrides)
    return body


def _session_settings(monkeypatch, admin_email: str) -> None:
    reset_collector_owner_rate_limits()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8820")
    monkeypatch.setattr(settings, "auth_web_base_url", "http://localhost:3820")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_company")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "central_admin_emails", admin_email)
    monkeypatch.setattr(settings, "collector_api_enabled", True)


def _login(local_client: TestClient, email: str) -> uuid.UUID:
    res = local_client.post("/auth/dev-login", json={"email": email})
    assert res.status_code == 200, res.text
    return uuid.UUID(res.json()["user_id"])


def _seed_personal_collector_doc(
    owner_id: uuid.UUID,
    *,
    title: str = "Owner personal source",
    source: str = "gws_gmail",
    external_id: str = "erase-me",
    updated_at: datetime | None = None,
) -> uuid.UUID:
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=owner_id,
                title=title,
                block_json=[],
                markdown="owner personal body",
                source=source,
                source_account_id=None,
                source_external_id=external_id,
                schema_version=SCHEMA_VERSION,
                scope="personal",
                project="company",
                updated_at=updated_at or datetime.now(UTC),
            )
        )
        s.commit()
    return doc_id


def _seed_index_rows(owner_id: uuid.UUID, doc_id: uuid.UUID) -> None:
    embedding_id = uuid.uuid4()
    account_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug="gws_gmail",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=owner_id,
                auth_mode="local_cli",
                status="active",
                settings_redacted={},
            )
        )
        s.execute(
            insert(embeddings).values(
                embedding_id=embedding_id,
                user_id=owner_id,
                kind="corpus_chunk",
                ref_id=doc_id,
                vec=[0.0] * 1024,
                meta={},
                schema_version=SCHEMA_VERSION,
                model_version="mock",
                scope="personal",
                project="company",
            )
        )
        s.execute(
            insert(corpus_chunks).values(
                chunk_id=uuid.uuid4(),
                doc_id=doc_id,
                ordinal=0,
                content="owner personal body",
                embedding_id=embedding_id,
                meta={},
                scope="personal",
                project="company",
            )
        )
        s.execute(
            insert(connector_items).values(
                account_id=account_id,
                external_id="erase-me",
                doc_id=doc_id,
            )
        )
        s.execute(
            insert(structured_rows).values(
                row_id=uuid.uuid4(),
                source="gws_gmail",
                record_type="mail",
                source_doc_id=doc_id,
                record_key="erase-me",
                properties={},
                scope="personal",
                project="company",
                owner_id=owner_id,
                user_id=owner_id,
            )
        )
        s.commit()


def _seed_derived_wiki(owner_id: uuid.UUID, doc_id: uuid.UUID, title: str) -> tuple[str, str, str]:
    source_slug = source_slug_from_title(title, doc_id)
    claim_slug = f"{source_slug}-claim"
    page_slug = f"{source_slug}-page"
    now = datetime.now(UTC)
    store.write_source(
        WikiSource(
            slug=source_slug,
            title=title,
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=now,
            summary="summary",
            key_concepts=[],
            key_claims=[claim_slug],
            terminology=[],
            related_pages=[page_slug],
            open_questions=[],
        ),
        user_id=owner_id,
        scope="personal",
        owner_id=owner_id,
        project="company",
    )
    store.write_claim(
        WikiClaim(
            slug=claim_slug,
            claim="claim",
            supporting=[source_slug],
            conflicting=[],
            confidence="high",
            last_reviewed=date.today(),
            related_pages=[page_slug],
            evidence="evidence",
        ),
        user_id=owner_id,
        scope="personal",
        owner_id=owner_id,
        project="company",
    )
    store.write_page(
        WikiPage(
            slug=page_slug,
            title="Derived Page",
            definition="claim",
            overview="evidence",
            relations=[],
            evidence=[claim_slug],
            competing=[],
            open_questions=[],
            sources=[source_slug],
            backlinks=[claim_slug],
        ),
        user_id=owner_id,
        scope="personal",
        owner_id=owner_id,
        project="company",
    )
    store.write_task(
        WikiTask(
            slug=f"open-question-{source_slug}",
            kind="open_question",
            description="question",
            related=[source_slug],
            created_at=now,
        ),
        user_id=owner_id,
        scope="personal",
        owner_id=owner_id,
        project="company",
    )
    return source_slug, claim_slug, page_slug


def _write_source_slug(owner_id: uuid.UUID, slug: str, doc_id: uuid.UUID, *, scope: str) -> None:
    store.write_source(
        WikiSource(
            slug=slug,
            title=f"{scope} source",
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=datetime.now(UTC),
            summary=f"{scope} summary",
            key_concepts=[],
            key_claims=[],
            terminology=[],
            related_pages=[],
            open_questions=[],
        ),
        user_id=owner_id,
        scope=scope,
        owner_id=owner_id if scope == "personal" else None,
        project="company",
    )


# --- feature flag / node gate ---------------------------------------------------


def test_collector_ingest_flag_off_returns_404(clean):
    # Flag off: the endpoint 404s before any auth/DB work, so a dummy bearer
    # is enough to prove the surface is hidden.
    res = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers("dct_dummy"),
    )
    assert res.status_code == 404


def test_collector_ingest_personal_node_returns_404(clean, user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    token = _make_token(user_id, node_id="personal-a")
    res = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    assert res.status_code == 404


# --- token auth -----------------------------------------------------------------


def test_collector_token_valid_authenticates(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    res = client.get("/collector/commands/pending", headers=_headers(token))
    assert res.status_code == 200
    assert res.json() == {"count": 0, "items": []}


def test_collector_token_revoked_rejected(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, revoked=True)
    res = client.get("/collector/commands/pending", headers=_headers(token))
    assert res.status_code == 401


def test_collector_token_malformed_rejected(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    res = client.get("/collector/commands/pending", headers={"Authorization": "Bearer not-a-dct"})
    assert res.status_code == 401


def test_collector_token_wrong_node_rejected(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, node_id="personal-a")
    res = client.get("/collector/commands/pending", headers=_headers(token))
    assert res.status_code == 401


def test_collector_token_missing_rejected(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    res = client.get("/collector/commands/pending")
    assert res.status_code == 401


def test_collector_config_returns_owner_personal_non_secret_settings(
    clean,
    user_id,
    monkeypatch,
):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])
    other_user = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user, display_name="Other"))
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="github",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=user_id,
                auth_mode="token",
                settings_redacted={
                    "repos": ["ExampleOrg/Orthus-AI"],
                    "max_items": 7,
                    "secret_refs": {"token": "orthus/connectors/secret"},
                    "token_configured": True,
                    "env": "ORTHUS_CONN_GITHUB_TOKEN",
                },
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="local_files",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=user_id,
                auth_mode="local_path",
                settings_redacted={
                    "roots": ["<local>"],
                    "extensions": ".md,.txt",
                    "max_bytes": 4096,
                    "root_count": 1,
                },
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="gws_gmail",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=user_id,
                auth_mode="local_cli",
                settings_redacted={
                    "query": "from:remote@example.com",
                    "max_messages": 3,
                    "command": "gws",
                },
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="github",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=other_user,
                auth_mode="token",
                settings_redacted={"repos": ["Other/Repo"]},
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="slack",
                account_kind="company",
                node_id=COMPANY_NODE,
                scope="company",
                owner_id=None,
                auth_mode="token",
                settings_redacted={"channels": ["C123"]},
            )
        )
        s.commit()

    res = client.get("/collector/config", headers=_headers(token))

    assert res.status_code == 200
    body = res.json()
    assert body["owner_id"] == str(user_id)
    accounts = {item["connector_slug"]: item["settings"] for item in body["accounts"]}
    assert accounts == {
        "github": {"repos": ["ExampleOrg/Orthus-AI"], "max_items": 7},
        "gws_gmail": {"query": "from:remote@example.com", "max_messages": 3},
        "local_files": {
            "roots": ["<local>"],
            "extensions": [".md", ".txt"],
            "max_bytes": 4096,
        },
    }
    serialized = str(body)
    assert "secret_refs" not in serialized
    assert "token_configured" not in serialized
    assert "orthus/connectors/secret" not in serialized
    assert "command" not in serialized
    assert "Other/Repo" not in serialized
    assert "channels" not in serialized


# --- auth-world separation ------------------------------------------------------


def test_ingest_token_cannot_reach_ask_knowledge_endpoint(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    # P8.7b: /ask is dual-auth (session OR a `knowledge`-scoped token). A default
    # ingest-only collector token is a valid bearer but lacks the `knowledge`
    # scope, so it is rejected 403 (wrong scope) rather than reaching a grounded
    # answer. Session callers still need a cookie; a dct_ bearer never mints one.
    token = _make_token(user_id)  # default scopes = {ingest}
    res = client.post("/ask", json={"question": "hello"}, headers=_headers(token))
    assert res.status_code == 403


def test_demo_x_user_id_rejected_on_collector_endpoint(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    res = client.get("/collector/commands/pending", headers={"X-User-Id": str(user_id)})
    assert res.status_code == 401


def test_session_cookie_rejected_on_collector_token_endpoint(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    # The browser session is valid for session endpoints but must not satisfy the
    # collector-token ingest endpoint.
    res = local.post("/collector/ingest/documents", json={"documents": [_doc()]})
    assert res.status_code == 401


# --- ingest ---------------------------------------------------------------------


def test_collector_ingest_stamps_owner_and_personal_scope(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    res = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    assert res.status_code == 200
    result = res.json()["results"][0]
    assert result["status"] == "created"
    assert result["source_external_id"] == "msg-1"

    with session() as s:
        rows = s.execute(
            select(
                documents.c.user_id,
                documents.c.scope,
                documents.c.project,
                documents.c.source,
                documents.c.markdown,
                documents.c.source_last_edited_at,
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user_id
    assert row.scope == "personal"
    assert row.project == "company"
    assert row.source == "gws_gmail"
    assert row.markdown == "owner-only body"
    assert row.source_last_edited_at is None


def test_collector_ingest_double_push_is_idempotent(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    first = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    second = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    third = client.post(
        "/collector/ingest/documents",
        json={
            "documents": [
                _doc(
                    title="Edited title",
                    content_md="edited body",
                    source_last_edited_at="2026-05-29T01:11:12Z",
                )
            ]
        },
        headers=_headers(token),
    )

    assert first.json()["results"][0]["status"] == "created"
    assert second.json()["results"][0]["status"] == "unchanged"
    assert third.json()["results"][0]["status"] == "updated"

    with session() as s:
        count = s.execute(select(documents.c.doc_id)).all()
        row = s.execute(
            select(
                documents.c.title,
                documents.c.markdown,
                documents.c.source_last_edited_at,
            )
        ).first()
    assert len(count) == 1
    assert row.title == "Edited title"
    assert row.markdown == "edited body"
    assert row.source_last_edited_at == datetime(2026, 5, 29, 1, 11, 12, tzinfo=UTC)


def test_collector_ingest_uses_payload_project_when_given(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    res = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc(project="company")]},
        headers=_headers(token),
    )
    assert res.status_code == 200
    with session() as s:
        row = s.execute(select(documents.c.project)).first()
    assert row.project == "company"


def test_collector_ingest_rejects_sources_outside_p8_thin_collector(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)

    # notion is still not a thin-collector ingest source. chat_exports/email_exports
    # were re-activated for the daemon, so they are no longer rejected here.
    for source in ("notion",):
        res = client.post(
            "/collector/ingest/documents",
            json={"documents": [_doc(source=source, source_external_id=f"{source}-1")]},
            headers=_headers(token),
        )
        assert res.status_code == 422
        assert "unsupported collector source" in res.json()["detail"]


def test_collector_ingest_accepts_reactivated_export_sources(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    for source in ("chat_exports", "email_exports"):
        res = client.post(
            "/collector/ingest/documents",
            json={"documents": [_doc(source=source, source_external_id=f"{source}-1")]},
            headers=_headers(token),
        )
        assert res.status_code == 200, res.text
        assert res.json()["results"][0]["status"] == "created"


def test_collector_ingest_batch_limit_enforced(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    docs = [_doc(source_external_id=f"msg-{i}") for i in range(201)]
    res = client.post(
        "/collector/ingest/documents",
        json={"documents": docs},
        headers=_headers(token),
    )
    assert res.status_code == 422


def test_collector_ingest_owner_docs_rate_limit_is_owner_scoped(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_ingest_owner_docs_limit", 1)
    monkeypatch.setattr(settings, "collector_ingest_owner_bytes_limit", 10_000)
    other_user_id = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user_id, display_name="Other Owner"))
        s.commit()
    token_a = _make_token(user_id)
    token_a_second = _make_token(user_id)
    token_b = _make_token(other_user_id)

    first = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc(source_external_id="owner-a-1")]},
        headers=_headers(token_a),
    )
    second_same_owner = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc(source_external_id="owner-a-2")]},
        headers=_headers(token_a_second),
    )
    other_owner = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc(source_external_id="owner-b-1")]},
        headers=_headers(token_b),
    )
    control_plane = client.get("/collector/config", headers=_headers(token_a))

    assert first.status_code == 200
    assert second_same_owner.status_code == 429
    assert second_same_owner.headers["retry-after"]
    assert "collector ingest rate limited: docs" == second_same_owner.json()["detail"]
    assert other_owner.status_code == 200
    assert control_plane.status_code == 200
    with session() as s:
        owner_a_docs = s.execute(
            select(func.count()).select_from(documents).where(documents.c.user_id == user_id)
        ).scalar_one()
        owner_b_docs = s.execute(
            select(func.count()).select_from(documents).where(documents.c.user_id == other_user_id)
        ).scalar_one()
        audit_rows = s.execute(
            select(audit_log.c.phase, audit_log.c.meta).where(
                audit_log.c.node == "collector.rate_limit"
            )
        ).all()
    assert owner_a_docs == 1
    assert owner_b_docs == 1
    assert any(
        row.phase == "exit"
        and row.meta.get("kind") == "ingest"
        and row.meta.get("reason") == "docs"
        for row in audit_rows
    )


def test_collector_ingest_owner_bytes_rate_limit_writes_nothing(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_ingest_owner_docs_limit", 10)
    monkeypatch.setattr(settings, "collector_ingest_owner_bytes_limit", 4)
    token = _make_token(user_id)

    res = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc(content_md="12345")]},
        headers=_headers(token),
    )

    assert res.status_code == 429
    assert res.json()["detail"] == "collector ingest rate limited: bytes"
    with session() as s:
        assert s.execute(select(func.count()).select_from(documents)).scalar_one() == 0


def test_collector_ingest_records_audit(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    with session() as s:
        rows = s.execute(
            select(audit_log.c.phase, audit_log.c.meta).where(
                audit_log.c.node == "collector.ingest"
            )
        ).all()
    assert any(r.phase == "exit" and r.meta.get("created") == 1 for r in rows)


# --- command lifecycle ----------------------------------------------------------


def test_command_lifecycle_pending_claimed_done(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    created = local.post(
        "/collector/commands",
        json={"kind": "connector_sync", "payload": {"connector": "gws_gmail"}},
    )
    assert created.status_code == 200
    command_id = created.json()["command_id"]
    owner_id = created.json()["user_id"]
    assert created.json()["status"] == "pending"

    token = _make_token(uuid.UUID(owner_id))
    pending = local.get("/collector/commands/pending", headers=_headers(token))
    assert pending.json()["count"] == 1

    claimed = local.post(f"/collector/commands/{command_id}/claim", headers=_headers(token))
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "claimed"

    # Re-claim is no longer possible.
    reclaim = local.post(f"/collector/commands/{command_id}/claim", headers=_headers(token))
    assert reclaim.status_code == 409

    completed = local.post(
        f"/collector/commands/{command_id}/complete",
        json={"status": "done", "result": {"fetched": 3}},
        headers=_headers(token),
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    assert completed.json()["result"] == {"fetched": 3}


def test_command_create_coalesces_duplicate_active_command(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    payload = {"connector": "gws_gmail"}

    first = local.post("/collector/commands", json={"kind": "connector_sync", "payload": payload})
    second = local.post("/collector/commands", json={"kind": "connector_sync", "payload": payload})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["command_id"] == first.json()["command_id"]

    listed = local.get("/collector/commands", params={"status": "pending"})
    assert listed.json()["count"] == 1


def test_stale_claimed_command_requeues_for_recovery(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    created = local.post(
        "/collector/commands",
        json={"kind": "connector_sync", "payload": {"connector": "local_files"}},
    )
    command_id = uuid.UUID(created.json()["command_id"])
    owner_id = uuid.UUID(created.json()["user_id"])
    token = _make_token(owner_id)

    claimed = local.post(f"/collector/commands/{command_id}/claim", headers=_headers(token))
    assert claimed.status_code == 200

    stale_time = datetime.now(UTC) - timedelta(hours=1)
    with session() as s:
        s.execute(
            update(collector_commands)
            .where(collector_commands.c.command_id == command_id)
            .values(claimed_at=stale_time)
        )
        s.commit()

    pending = local.get("/collector/commands/pending", headers=_headers(token))
    assert pending.status_code == 200
    assert pending.json()["count"] == 1
    assert pending.json()["items"][0]["status"] == "pending"
    assert pending.json()["items"][0]["claimed_at"] is None

    reclaimed = local.post(f"/collector/commands/{command_id}/claim", headers=_headers(token))
    assert reclaimed.status_code == 200
    assert reclaimed.json()["status"] == "claimed"


def test_non_operator_cannot_create_command(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    settings = get_settings()
    # Make the logged-in account a non-manager role so require_node_operator rejects.
    monkeypatch.setattr(settings, "central_admin_emails", "admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    # Add a member-role account via allowlist and log in as them.
    from orthus.auth import normalize_email
    from orthus.tables import auth_allowlist

    with session() as s:
        s.execute(
            insert(auth_allowlist).values(
                allowlist_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                email=normalize_email("member@example.com"),
                role="member",
            )
        )
        s.commit()
    member = TestClient(app)
    _login(member, "member@example.com")
    res = member.post("/collector/commands", json={"kind": "connector_sync", "payload": {}})
    assert res.status_code == 403


def test_command_queue_is_owner_isolated(clean, user_id, monkeypatch):
    # User A creates a command; user B's collector token must not see/claim it.
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    created = local.post(
        "/collector/commands", json={"kind": "raw_repush", "payload": {"source": "local_files"}}
    )
    command_id = created.json()["command_id"]

    other_user = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user, display_name="Other"))
        s.commit()
    other_token = _make_token(other_user)

    pending = local.get("/collector/commands/pending", headers=_headers(other_token))
    assert pending.json()["count"] == 0

    claim = local.post(f"/collector/commands/{command_id}/claim", headers=_headers(other_token))
    assert claim.status_code == 409

    complete = local.post(
        f"/collector/commands/{command_id}/complete",
        json={"status": "done", "result": {}},
        headers=_headers(other_token),
    )
    assert complete.status_code == 409


def test_command_list_filters_status_and_records_audit(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    local.post(
        "/collector/commands",
        json={"kind": "connector_sync", "payload": {"connector": "gws_gmail"}},
    )

    listed = local.get("/collector/commands", params={"status": "pending"})
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    empty = local.get("/collector/commands", params={"status": "done"})
    assert empty.json()["count"] == 0

    with session() as s:
        rows = s.execute(
            select(audit_log.c.node).where(audit_log.c.node == "collector.command.create")
        ).all()
    assert rows


def test_session_operator_issues_lists_and_revokes_collector_token(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    user_id = _login(local, "admin@example.com")

    issued = local.post(
        "/collector/tokens",
        json={"name": "mac-mini", "scopes": ["ingest", "knowledge"]},
    )
    assert issued.status_code == 200, issued.text
    body = issued.json()
    plaintext = body["token"]
    token_id = body["item"]["token_id"]
    assert plaintext.startswith("dct_")
    assert body["item"]["name"] == "mac-mini"
    assert body["item"]["scopes"] == ["ingest", "knowledge"]
    assert body["item"]["last_polled_at"] is None
    assert body["item"]["last_status_at"] is None
    assert body["item"]["scheduler_installed"] is None
    assert body["item"]["scheduler_loaded"] is None
    assert body["item"]["scheduler_interval_seconds"] is None
    assert body["item"]["last_status_error"] is None

    with session() as s:
        row = s.execute(
            select(
                collector_tokens.c.user_id,
                collector_tokens.c.token_hash,
                collector_tokens.c.revoked_at,
            ).where(collector_tokens.c.token_id == uuid.UUID(token_id))
        ).one()
    assert row.user_id == user_id
    assert row.token_hash == hash_collector_token(plaintext)
    assert row.token_hash != plaintext
    assert row.revoked_at is None

    pending = local.get("/collector/commands/pending", headers=_headers(plaintext))
    assert pending.status_code == 200
    status = local.post(
        "/collector/status",
        headers=_headers(plaintext),
        json={
            "scheduler_installed": True,
            "scheduler_loaded": False,
            "scheduler_interval_seconds": 900,
        },
    )
    assert status.status_code == 200, status.text
    assert status.json()["scheduler_installed"] is True
    assert status.json()["scheduler_loaded"] is False
    assert status.json()["scheduler_interval_seconds"] == 900
    uninstalled = local.post(
        "/collector/status",
        headers=_headers(plaintext),
        json={"scheduler_installed": False, "scheduler_loaded": False},
    )
    assert uninstalled.status_code == 200, uninstalled.text
    assert uninstalled.json()["scheduler_installed"] is False
    assert uninstalled.json()["scheduler_loaded"] is False
    assert uninstalled.json()["scheduler_interval_seconds"] is None

    listed = local.get("/collector/tokens")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    listed_token = listed.json()["items"][0]
    assert listed_token["last_used_at"] is not None
    assert listed_token["last_polled_at"] is not None
    assert listed_token["last_status_at"] is not None
    assert listed_token["scheduler_installed"] is False
    assert listed_token["scheduler_loaded"] is False
    assert listed_token["scheduler_interval_seconds"] is None
    assert listed_token["last_status_error"] is None
    assert "token" not in listed_token
    assert "token_hash" not in listed_token
    assert "secret_refs" not in listed_token

    with session() as s:
        stamped = s.execute(
            select(
                collector_tokens.c.last_polled_at,
                collector_tokens.c.last_status_at,
                collector_tokens.c.scheduler_installed,
                collector_tokens.c.scheduler_loaded,
                collector_tokens.c.scheduler_interval_seconds,
            ).where(collector_tokens.c.token_id == uuid.UUID(token_id))
        ).one()
    assert stamped.last_polled_at is not None
    assert stamped.last_status_at is not None
    assert stamped.scheduler_installed is False
    assert stamped.scheduler_loaded is False
    assert stamped.scheduler_interval_seconds is None

    revoked = local.post(f"/collector/tokens/{token_id}/revoke")
    assert revoked.status_code == 200
    revoked_body = revoked.json()
    assert revoked_body["revoked_at"] is not None
    assert "token" not in revoked_body
    assert "token_hash" not in revoked_body
    assert "secret_refs" not in revoked_body

    rejected = local.get("/collector/commands/pending", headers=_headers(plaintext))
    assert rejected.status_code == 401


def test_collector_token_routes_are_owner_scoped(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="owner@example.com,other@example.com")
    owner = TestClient(app)
    other = TestClient(app)
    _login(owner, "owner@example.com")
    _login(other, "other@example.com")

    issued = owner.post("/collector/tokens", json={"name": "owner-token"})
    assert issued.status_code == 200, issued.text
    token_id = issued.json()["item"]["token_id"]

    other_list = other.get("/collector/tokens")
    assert other_list.status_code == 200
    assert other_list.json()["count"] == 0

    other_revoke = other.post(f"/collector/tokens/{token_id}/revoke")
    assert other_revoke.status_code == 404


def test_collector_token_issue_rejects_unknown_scope(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")

    res = local.post("/collector/tokens", json={"name": "bad", "scopes": ["root"]})
    assert res.status_code == 422


def _member_client(email: str = "member@example.com") -> tuple[TestClient, uuid.UUID]:
    """Log in a non-operator (role=member) session user via the allowlist."""
    from orthus.auth import normalize_email
    from orthus.tables import auth_allowlist

    with session() as s:
        s.execute(
            insert(auth_allowlist).values(
                allowlist_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                email=normalize_email(email),
                role="member",
            )
        )
        s.commit()
    member = TestClient(app)
    member_id = _login(member, email)
    return member, member_id


def test_member_can_issue_self_bound_collector_token(clean, monkeypatch):
    # P10.8: members self-onboard the CLI — POST /collector/tokens no longer
    # requires operator role because the token is always self-bound.
    _session_settings(monkeypatch, admin_email="admin@example.com")
    member, member_id = _member_client()

    issued = member.post(
        "/collector/tokens",
        json={
            "name": "member-cli",
            "scopes": ["ingest", "commands", "knowledge", "agent_task", "ingest"],
            "device_label": "Member Mac",
        },
    )
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["token"].startswith("dct_")
    # Duplicate scope deduped by _normalize_token_scopes; order preserved.
    assert body["item"]["scopes"] == ["ingest", "commands", "knowledge", "agent_task"]

    with session() as s:
        row = s.execute(
            select(collector_tokens.c.user_id).where(
                collector_tokens.c.token_id == uuid.UUID(body["item"]["token_id"])
            )
        ).one()
    assert row.user_id == member_id


def test_member_token_list_and_revoke_are_strictly_self_scoped(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    admin = TestClient(app)
    _login(admin, "admin@example.com")
    member, _member_id = _member_client()

    admin_issued = admin.post("/collector/tokens", json={"name": "admin-token"})
    assert admin_issued.status_code == 200, admin_issued.text
    admin_token_id = admin_issued.json()["item"]["token_id"]

    # Member sees only their own rows (none yet), never the admin's.
    listed = member.get("/collector/tokens")
    assert listed.status_code == 200
    assert listed.json()["count"] == 0

    member_issued = member.post("/collector/tokens", json={"name": "member-token"})
    assert member_issued.status_code == 200, member_issued.text
    member_token_id = member_issued.json()["item"]["token_id"]

    listed = member.get("/collector/tokens")
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["token_id"] == member_token_id

    # Member cannot revoke another user's token (owner-scope 404 convention).
    cross_revoke = member.post(f"/collector/tokens/{admin_token_id}/revoke")
    assert cross_revoke.status_code == 404

    # Member can revoke their own token.
    own_revoke = member.post(f"/collector/tokens/{member_token_id}/revoke")
    assert own_revoke.status_code == 200
    assert own_revoke.json()["revoked_at"] is not None

    # Operator regression: admin still sees only their own row and revokes it.
    admin_list = admin.get("/collector/tokens")
    assert admin_list.json()["count"] == 1
    assert admin_list.json()["items"][0]["token_id"] == admin_token_id
    admin_revoke = admin.post(f"/collector/tokens/{admin_token_id}/revoke")
    assert admin_revoke.status_code == 200


def test_member_token_issue_rejects_unknown_scope(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    member, _member_id = _member_client()

    res = member.post("/collector/tokens", json={"name": "bad", "scopes": ["root"]})
    assert res.status_code == 422


def test_collector_evidence_surfaces_null_account_docs_and_owner_boundary(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com,otheradmin@example.com")
    owner_client = TestClient(app)
    other_client = TestClient(app)
    owner_id = _login(owner_client, "admin@example.com")
    other_id = _login(other_client, "otheradmin@example.com")
    now = datetime(2026, 6, 12, 8, 30, tzinfo=UTC)
    owner_last_polled = datetime.now(UTC) - timedelta(minutes=10)
    owner_last_status = datetime.now(UTC) - timedelta(minutes=9)
    other_last_polled = datetime.now(UTC) - timedelta(minutes=2)
    owner_gmail_doc_id = uuid.uuid4()

    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=owner_id,
                node_id=COMPANY_NODE,
                name="owner-active",
                token_hash=hash_collector_token("dct_owner_active"),
                scopes=["ingest"],
                last_used_at=owner_last_polled,
                last_polled_at=owner_last_polled,
                last_status_at=owner_last_status,
                scheduler_installed=True,
                scheduler_loaded=True,
                scheduler_interval_seconds=900,
            )
        )
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=owner_id,
                node_id=COMPANY_NODE,
                name="owner-revoked",
                token_hash=hash_collector_token("dct_owner_revoked"),
                scopes=["ingest"],
                last_used_at=other_last_polled,
                last_polled_at=other_last_polled,
                revoked_at=other_last_polled,
            )
        )
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=other_id,
                node_id=COMPANY_NODE,
                name="other-active",
                token_hash=hash_collector_token("dct_other_active"),
                scopes=["ingest"],
                last_used_at=other_last_polled,
                last_polled_at=other_last_polled,
            )
        )
        s.execute(
            insert(documents).values(
                doc_id=owner_gmail_doc_id,
                user_id=owner_id,
                title="Owner null account Gmail",
                block_json=[],
                markdown="owner gmail body",
                source="gws_gmail",
                source_account_id=None,
                source_external_id="gmail:null-account",
                schema_version=SCHEMA_VERSION,
                scope="personal",
                project="company",
                updated_at=now,
            )
        )
        s.execute(
            insert(documents).values(
                doc_id=uuid.uuid4(),
                user_id=owner_id,
                title="Owner local account file",
                block_json=[],
                markdown="owner local body",
                source="local_files",
                source_account_id=None,
                source_external_id="local:account-doc",
                schema_version=SCHEMA_VERSION,
                scope="personal",
                project="company",
                updated_at=now - timedelta(minutes=2),
            )
        )
        s.execute(
            insert(documents).values(
                doc_id=uuid.uuid4(),
                user_id=owner_id,
                title="Owner company scoped Gmail",
                block_json=[],
                markdown="company scoped body",
                source="gws_gmail",
                source_account_id=None,
                source_external_id="gmail:company-scope",
                schema_version=SCHEMA_VERSION,
                scope="company",
                project="company",
                updated_at=now,
            )
        )
        s.execute(
            insert(documents).values(
                doc_id=uuid.uuid4(),
                user_id=other_id,
                title="Other admin Gmail",
                block_json=[],
                markdown="other body",
                source="gws_gmail",
                source_account_id=None,
                source_external_id="gmail:other-admin",
                schema_version=SCHEMA_VERSION,
                scope="personal",
                project="company",
                updated_at=now,
            )
        )
        s.execute(
            insert(collector_commands).values(
                command_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                user_id=owner_id,
                kind="connector_sync",
                payload={"connector": "gws_gmail"},
                status="done",
                result={
                    "source": "gws_gmail",
                    "created": 1,
                    "compile_indexed": 2,
                    "compile_authored": 1,
                    "compile_skipped": 3,
                    "compile_failed": 0,
                },
                created_by=owner_id,
                created_at=now - timedelta(minutes=3),
                completed_at=now - timedelta(minutes=2),
            )
        )
        s.execute(
            insert(collector_commands).values(
                command_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                user_id=owner_id,
                kind="connector_sync",
                payload={"connector": "gws_gmail"},
                status="pending",
                result=None,
                created_by=owner_id,
                created_at=now,
            )
        )
        s.execute(
            insert(collector_commands).values(
                command_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                user_id=other_id,
                kind="connector_sync",
                payload={"connector": "gws_gmail"},
                status="done",
                result={"source": "gws_gmail", "created": 1, "compile_indexed": 9},
                created_by=other_id,
                created_at=now,
                completed_at=now,
            )
        )
        s.commit()

    source_slug, _, page_slug = _seed_derived_wiki(
        owner_id,
        owner_gmail_doc_id,
        "Owner null account Gmail",
    )
    store.write_page(
        WikiPage(
            slug=page_slug,
            title="Company Collision Page",
            definition="company page",
            overview="same slug company page",
            relations=[],
            evidence=[],
            competing=[],
            open_questions=[],
            sources=[],
            backlinks=[],
        ),
        user_id=owner_id,
        scope="company",
        owner_id=None,
        project="company",
    )

    owner_res = owner_client.get("/collector/evidence")

    assert owner_res.status_code == 200
    owner_body = owner_res.json()
    assert owner_body["owner_id"] == str(owner_id)
    assert owner_body["liveness"]["status"] == "live"
    assert owner_body["liveness"]["active_token_count"] == 1
    assert owner_body["liveness"]["revoked_token_count"] == 1
    assert owner_body["liveness"]["pending_command_count"] == 1
    assert owner_body["liveness"]["claimed_command_count"] == 0
    assert datetime.fromisoformat(owner_body["liveness"]["last_polled_at"]) == owner_last_polled
    assert datetime.fromisoformat(owner_body["liveness"]["last_status_at"]) == owner_last_status
    assert owner_body["liveness"]["scheduler_installed"] is True
    assert owner_body["liveness"]["scheduler_loaded"] is True
    assert owner_body["liveness"]["scheduler_interval_seconds"] == 900
    assert owner_body["liveness"]["oldest_pending_command_at"] is not None
    sources = {item["source"]: item for item in owner_body["sources"]}
    assert sources["gws_gmail"]["document_count"] == 1
    assert sources["gws_gmail"]["recent_documents"][0]["title"] == "Owner null account Gmail"
    assert sources["gws_gmail"]["recent_documents"][0]["source_account_id"] is None
    assert sources["gws_gmail"]["recent_documents"][0]["wiki_source_slug"] == source_slug
    assert sources["gws_gmail"]["recent_documents"][0]["wiki_pages"] == [
        {"slug": page_slug, "title": "Derived Page", "scope": "personal"}
    ]
    assert sources["gws_gmail"]["latest_command"]["status"] == "pending"
    assert sources["gws_gmail"]["latest_compile"] == {
        "indexed": 2,
        "authored": 1,
        "skipped": 3,
        "failed": 0,
    }
    assert sources["local_files"]["document_count"] == 1
    assert sources["local_files"]["recent_documents"][0]["wiki_source_slug"] is None
    assert sources["local_files"]["recent_documents"][0]["wiki_pages"] == []
    serialized = str(owner_body)
    assert "Other admin Gmail" not in serialized
    assert "Owner company scoped Gmail" not in serialized

    other_res = other_client.get("/collector/evidence")

    assert other_res.status_code == 200
    other_body = other_res.json()
    assert other_body["liveness"]["active_token_count"] == 1
    assert other_body["liveness"]["last_polled_at"] != owner_body["liveness"]["last_polled_at"]
    other_sources = {item["source"]: item for item in other_body["sources"]}
    assert other_sources["gws_gmail"]["document_count"] == 1
    assert other_sources["gws_gmail"]["recent_documents"][0]["title"] == "Other admin Gmail"
    assert "Owner null account Gmail" not in str(other_body)

    scoped_page = owner_client.get(f"/wiki/pages/{page_slug}?scope=personal")
    default_page = owner_client.get(f"/wiki/pages/{page_slug}")
    assert scoped_page.status_code == 200
    assert default_page.status_code == 200
    assert scoped_page.json()["title"] == "Derived Page"
    assert default_page.json()["title"] == "Company Collision Page"


def test_collector_liveness_counts_pending_outside_recent_command_window(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    user_id = _login(local, "admin@example.com")
    last_polled = datetime.now(UTC) - timedelta(hours=3)
    old_pending_at = datetime.now(UTC) - timedelta(days=3)

    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=COMPANY_NODE,
                name="owner-active",
                token_hash=hash_collector_token("dct_owner_liveness_window"),
                scopes=["ingest"],
                last_used_at=last_polled,
                last_polled_at=last_polled,
            )
        )
        s.execute(
            insert(collector_commands).values(
                command_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                user_id=user_id,
                kind="connector_sync",
                payload={"connector": "local_files"},
                status="pending",
                result=None,
                created_by=user_id,
                created_at=old_pending_at,
            )
        )
        for index in range(45):
            s.execute(
                insert(collector_commands).values(
                    command_id=uuid.uuid4(),
                    node_id=COMPANY_NODE,
                    user_id=user_id,
                    kind="connector_sync",
                    payload={"connector": "gws_gmail", "index": index},
                    status="done",
                    result={"source": "gws_gmail", "created": 0},
                    created_by=user_id,
                    created_at=datetime.now(UTC) - timedelta(minutes=index),
                    completed_at=datetime.now(UTC) - timedelta(minutes=index),
                )
            )
        s.commit()

    res = local.get("/collector/evidence")

    assert res.status_code == 200
    body = res.json()
    assert len(body["recent_commands"]) == 40
    assert all(command["status"] == "done" for command in body["recent_commands"])
    assert body["liveness"]["status"] == "offline"
    assert body["liveness"]["pending_command_count"] == 1
    assert datetime.fromisoformat(body["liveness"]["oldest_pending_command_at"]) == old_pending_at
    assert "pending command" in body["liveness"]["reason"]


def test_session_operator_deletes_own_personal_collector_document_and_derivatives(
    clean, monkeypatch
):
    _session_settings(monkeypatch, admin_email="owner@example.com,other@example.com")
    owner_client = TestClient(app)
    other_client = TestClient(app)
    owner_id = _login(owner_client, "owner@example.com")
    other_id = _login(other_client, "other@example.com")
    title = "Owner delete target"
    doc_id = _seed_personal_collector_doc(owner_id, title=title)
    _seed_index_rows(owner_id, doc_id)
    source_slug, claim_slug, page_slug = _seed_derived_wiki(owner_id, doc_id, title)
    other_doc = _seed_personal_collector_doc(
        other_id,
        title="Other untouched",
        external_id="other-doc",
    )
    company_doc = _seed_personal_collector_doc(
        owner_id,
        title="Company untouched",
        external_id="company-doc",
    )
    _write_source_slug(owner_id, source_slug, company_doc, scope="company")
    _write_source_slug(other_id, source_slug, other_doc, scope="personal")
    with session() as s:
        s.execute(
            update(documents)
            .where(documents.c.doc_id == company_doc)
            .values(scope="company", user_id=owner_id)
        )
        s.commit()

    res = owner_client.delete(f"/collector/documents/{doc_id}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["owner_id"] == str(owner_id)
    assert body["documents_deleted"] == 1
    assert body["connector_items_deleted"] == 1
    assert body["structured_rows_deleted"] == 1
    assert body["corpus_chunks_deleted"] == 1
    assert body["corpus_embeddings_deleted"] == 1
    assert body["wiki_items_deleted"] == 4
    with session() as s:
        remaining_docs = {row.doc_id for row in s.execute(select(documents.c.doc_id)).all()}
        assert doc_id not in remaining_docs
        assert other_doc in remaining_docs
        assert company_doc in remaining_docs
        assert s.execute(select(func.count()).select_from(corpus_chunks)).scalar_one() == 0
        assert (
            s.execute(
                select(func.count())
                .select_from(embeddings)
                .where(embeddings.c.kind == "corpus_chunk")
            ).scalar_one()
            == 0
        )
        assert s.execute(select(func.count()).select_from(connector_items)).scalar_one() == 0
        assert s.execute(select(func.count()).select_from(structured_rows)).scalar_one() == 0
        assert (
            s.execute(
                select(func.count())
                .select_from(wiki_pages)
                .where(wiki_pages.c.owner_id == owner_id)
            ).scalar_one()
            == 0
        )
        audit_rows = s.execute(
            select(audit_log.c.phase, audit_log.c.meta).where(
                audit_log.c.node == "collector.personal_data_delete"
            )
        ).all()
    assert store.load_source(source_slug, scope="personal", owner_id=owner_id) is None
    assert store.load_claim(claim_slug, scope="personal", owner_id=owner_id) is None
    assert store.load_page(page_slug, scope="personal", owner_id=owner_id) is None
    assert store.load_source(source_slug, scope="company", owner_id=None) is not None
    assert store.load_source(source_slug, scope="personal", owner_id=other_id) is not None
    assert any(row.phase == "exit" and row.meta.get("documents_deleted") == 1 for row in audit_rows)
    assert title not in str(audit_rows)
    assert "erase-me" not in str(audit_rows)

    other_res = other_client.delete(f"/collector/documents/{company_doc}")
    assert other_res.status_code == 200
    assert other_res.json()["documents_deleted"] == 0


def test_collector_token_cannot_delete_central_personal_data(clean, user_id, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    token = _make_token(user_id)
    doc_id = _seed_personal_collector_doc(user_id)

    res = client.delete(f"/collector/documents/{doc_id}", headers=_headers(token))

    assert res.status_code == 401


def test_session_operator_prunes_old_personal_collector_documents_by_source(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    owner_id = _login(local, "admin@example.com")
    old = datetime.now(UTC) - timedelta(days=10)
    recent = datetime.now(UTC)
    old_local = _seed_personal_collector_doc(
        owner_id,
        title="Old local",
        source="local_files",
        external_id="old-local",
        updated_at=old,
    )
    recent_local = _seed_personal_collector_doc(
        owner_id,
        title="Recent local",
        source="local_files",
        external_id="recent-local",
        updated_at=recent,
    )
    old_github = _seed_personal_collector_doc(
        owner_id,
        title="Old github",
        source="github",
        external_id="old-github",
        updated_at=old,
    )

    res = local.post(
        "/collector/personal-data/prune",
        json={"older_than_days": 1, "source": "local_files"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["documents_deleted"] == 1
    with session() as s:
        remaining = {row.doc_id for row in s.execute(select(documents.c.doc_id)).all()}
    assert old_local not in remaining
    assert recent_local in remaining
    assert old_github in remaining


def test_personal_data_prune_rejects_unsupported_source(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")

    res = local.post(
        "/collector/personal-data/prune",
        json={"older_than_days": 1, "source": "slack"},
    )

    assert res.status_code == 422


def test_command_create_invalid_kind_rejected(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    res = local.post("/collector/commands", json={"kind": "delete_everything", "payload": {}})
    assert res.status_code == 422
    file_fetch = local.post("/collector/commands", json={"kind": "file_fetch", "payload": {}})
    assert file_fetch.status_code == 422


def test_command_create_rejects_sources_outside_p8_thin_collector(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")

    # notion is not a thin-collector connector; chat_exports/email_exports were
    # re-activated so they pass the command-create source validation now.
    sync = local.post(
        "/collector/commands",
        json={"kind": "connector_sync", "payload": {"connector": "notion"}},
    )
    repush = local.post(
        "/collector/commands",
        json={"kind": "raw_repush", "payload": {"source": "notion"}},
    )

    assert sync.status_code == 422
    assert "unsupported connector" in sync.json()["detail"]
    assert repush.status_code == 422
    assert "unsupported source" in repush.json()["detail"]


def test_command_create_accepts_reactivated_export_sources(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")

    sync = local.post(
        "/collector/commands",
        json={"kind": "connector_sync", "payload": {"connector": "chat_exports"}},
    )
    repush = local.post(
        "/collector/commands",
        json={"kind": "raw_repush", "payload": {"source": "email_exports"}},
    )

    assert sync.status_code == 200, sync.text
    assert repush.status_code == 200, repush.text


def test_collector_commands_create_requires_identity(clean, user_id, monkeypatch):
    # An identity row is needed so dev-login can map email -> user; ensure a stray
    # auth_identity for an unrelated email does not leak commands across owners.
    with session() as s:
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=user_id,
                provider="google",
                provider_subject="x",
                email="someone@example.com",
                email_verified=True,
            )
        )
        s.commit()
    _enable(monkeypatch)
    token = _make_token(user_id)
    # The collector list path only ever returns this owner's rows.
    res = client.get("/collector/commands/pending", headers=_headers(token))
    assert res.json()["count"] == 0


# --- P8.7a token scopes -----------------------------------------------------


def test_default_scope_token_backcompat_ingest_and_command(clean, monkeypatch):
    # A pre-P8.7a token (no explicit scopes -> DB default {ingest}) must still
    # ingest AND run the full command lifecycle. `ingest` implies `commands`.
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    _login(local, "admin@example.com")
    created = local.post(
        "/collector/commands",
        json={"kind": "connector_sync", "payload": {"connector": "gws_gmail"}},
    )
    owner_id = uuid.UUID(created.json()["user_id"])
    command_id = created.json()["command_id"]

    token = _make_token(owner_id)  # default scopes from DB
    ingest = local.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    assert ingest.status_code == 200

    pending = local.get("/collector/commands/pending", headers=_headers(token))
    assert pending.json()["count"] == 1
    claimed = local.post(f"/collector/commands/{command_id}/claim", headers=_headers(token))
    assert claimed.status_code == 200
    completed = local.post(
        f"/collector/commands/{command_id}/complete",
        json={"status": "done", "result": {}},
        headers=_headers(token),
    )
    assert completed.status_code == 200


def test_knowledge_scope_token_forbidden_on_ingest(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    assert res.status_code == 403


def test_knowledge_scope_token_forbidden_on_command_claim(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.post(
        f"/collector/commands/{uuid.uuid4()}/claim",
        headers=_headers(token),
    )
    assert res.status_code == 403


def test_knowledge_scope_token_forbidden_on_command_pending(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.get("/collector/commands/pending", headers=_headers(token))
    assert res.status_code == 403


def test_commands_scope_token_can_poll_but_not_ingest(clean, user_id, monkeypatch):
    # A `commands`-only token must not satisfy the `ingest` scope (no implication
    # in that direction).
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])
    pending = client.get("/collector/commands/pending", headers=_headers(token))
    assert pending.status_code == 200
    ingest = client.post(
        "/collector/ingest/documents",
        json={"documents": [_doc()]},
        headers=_headers(token),
    )
    assert ingest.status_code == 403


def test_session_operator_can_retry_personal_compile(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    user_id = _login(local, "admin@example.com")
    calls = []

    def fake_compile(owner_id, *, limit=None):
        calls.append((owner_id, limit))
        return SimpleNamespace(indexed=1, authored=2, skipped=3, failed=0)

    monkeypatch.setattr("orthus.api.routes.collector.compile_personal_documents", fake_compile)

    res = local.post("/collector/compile/retry", json={"limit": 7})
    assert res.status_code == 200, res.text
    assert res.json() == {"indexed": 1, "authored": 2, "skipped": 3, "failed": 0}
    assert calls == [(user_id, 7)]


def test_collector_compile_owner_cooldown_limits_token_calls(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_compile_owner_window_seconds", 300)
    other_user_id = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user_id, display_name="Other Owner"))
        s.commit()
    token = _make_token(user_id)
    other_token = _make_token(other_user_id)
    calls = []

    def fake_compile(owner_id, *, limit=None):
        calls.append((owner_id, limit))
        return SimpleNamespace(indexed=1, authored=0, skipped=0, failed=0)

    monkeypatch.setattr(
        "orthus.api.routes.collector.personal_compile_has_pending_work", lambda *a, **k: True
    )
    monkeypatch.setattr("orthus.api.routes.collector.compile_personal_documents", fake_compile)

    first = client.post("/collector/compile", json={"limit": 5}, headers=_headers(token))
    second = client.post("/collector/compile", json={"limit": 5}, headers=_headers(token))
    other_owner = client.post(
        "/collector/compile", json={"limit": 5}, headers=_headers(other_token)
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "collector compile rate limited: runs"
    assert other_owner.status_code == 200
    assert calls == [(user_id, 5), (other_user_id, 5)]


def test_session_operator_compile_retry_shares_token_owner_cooldown(clean, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    local = TestClient(app)
    user_id = _login(local, "admin@example.com")
    token = _make_token(user_id)
    calls = []

    def fake_compile(owner_id, *, limit=None):
        calls.append((owner_id, limit))
        return SimpleNamespace(indexed=1, authored=0, skipped=0, failed=0)

    monkeypatch.setattr(
        "orthus.api.routes.collector.personal_compile_has_pending_work", lambda *a, **k: True
    )
    monkeypatch.setattr("orthus.api.routes.collector.compile_personal_documents", fake_compile)

    first = client.post("/collector/compile", json={}, headers=_headers(token))
    second = local.post("/collector/compile/retry", json={})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "collector compile_retry rate limited: runs"
    assert calls == [(user_id, None)]


def test_collector_token_cannot_retry_compile_session_route(clean, user_id, monkeypatch):
    _session_settings(monkeypatch, admin_email="admin@example.com")
    token = _make_token(user_id)

    res = client.post("/collector/compile/retry", json={}, headers=_headers(token))
    assert res.status_code == 401


def test_require_scope_missing_scope_revoked_and_session_separation(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    # Missing scope -> 403.
    knowledge = _make_token(user_id, scopes=["knowledge"])
    assert client.get("/collector/commands/pending", headers=_headers(knowledge)).status_code == 403
    # Revoked token -> still 401 (auth fails before the scope check).
    revoked = _make_token(user_id, scopes=["commands"], revoked=True)
    assert client.get("/collector/commands/pending", headers=_headers(revoked)).status_code == 401
    # A session cookie never reaches the scoped token endpoint.
    res = client.get("/collector/commands/pending", headers={"X-User-Id": str(user_id)})
    assert res.status_code == 401


def test_effective_scopes_ingest_implies_commands():
    assert effective_scopes(frozenset({"ingest"})) == frozenset({"ingest", "commands"})
    assert effective_scopes(frozenset({"commands"})) == frozenset({"commands"})
    assert effective_scopes(frozenset({"knowledge"})) == frozenset({"knowledge"})
    # All vocabulary members are well-defined.
    assert "knowledge:write" in ALLOWED_SCOPES
