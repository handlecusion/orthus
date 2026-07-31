"""P2.5 personal→central promote gate."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.db import session
from orthus.promote import (
    approve_promotion_stage,
    export_personal_document,
    stage_promotion_package,
)
from orthus.schemas.canonical import SCHEMA_VERSION, PromotionPackage
from orthus.settings import get_settings
from orthus.tables import documents, promote_staging

client = TestClient(app)


def _seed_personal_doc(user_id) -> uuid.UUID:
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title="Private jane@example.com token=sk-secretsecretsecretsecret",
                block_json=[],
                markdown=(
                    "Shareable note. Contact jane@example.com. api_key=sk-secretsecretsecretsecret"
                ),
                source="local_files",
                source_external_id="private/path.md",
                schema_version=SCHEMA_VERSION,
                scope="personal",
                project="company",
            )
        )
        s.commit()
    return doc_id


def test_export_stage_approve_promotes_only_after_approval(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    doc_id = _seed_personal_doc(user_id)

    package = export_personal_document(doc_id, user_id, settings)

    assert package.source_node_id == "personal-a"
    assert package.source_scope == "personal"
    assert "j***@example.com" in package.sanitized_markdown
    assert "jane@example.com" not in package.sanitized_markdown
    assert "[REDACTED_SECRET]" in package.sanitized_markdown
    assert "sk-secret" not in package.sanitized_markdown
    export_diff = package.source_meta["export_sanitization"]
    assert export_diff["title"]["changed"] is True
    assert export_diff["markdown"]["changed"] is True
    assert export_diff["markdown"]["secret_markers"] == 1
    assert export_diff["markdown"]["pii_changed"] is True

    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    stage = stage_promotion_package(package, user_id, settings)

    assert stage.status == "pending"
    central_diff = stage.source_meta["central_sanitization"]
    assert central_diff["markdown"]["changed"] is False
    assert central_diff["markdown"]["secret_markers"] == 1
    assert [(event.node, event.status) for event in stage.audit_events] == [("promote.stage", "ok")]
    with session() as s:
        assert (
            s.execute(
                select(documents.c.doc_id).where(documents.c.source == "promoted_personal")
            ).first()
            is None
        )

    approved = approve_promotion_stage(stage.stage_id, user_id, settings)

    assert approved.status == "approved"
    assert approved.promoted_doc_id is not None
    assert [event.node for event in approved.audit_events] == [
        "promote.stage",
        "promote.approve",
    ]
    approve_event = approved.audit_events[-1]
    assert approve_event.status == "ok"
    assert approve_event.meta["stage_id"] == str(stage.stage_id)
    assert approve_event.meta["promoted_doc_id"] == str(approved.promoted_doc_id)
    with session() as s:
        doc = s.execute(
            select(documents.c.source, documents.c.scope, documents.c.markdown).where(
                documents.c.doc_id == approved.promoted_doc_id
            )
        ).one()
        row = s.execute(
            select(promote_staging.c.status, promote_staging.c.promoted_doc_id).where(
                promote_staging.c.stage_id == stage.stage_id
            )
        ).one()
    assert doc.source == "promoted_personal"
    assert doc.scope == "company"
    assert "jane@example.com" not in doc.markdown
    assert "sk-secret" not in doc.markdown
    assert row.status == "approved"
    assert row.promoted_doc_id == approved.promoted_doc_id


def test_promote_gate_enforces_node_boundary(user_id, monkeypatch):
    settings = get_settings()
    doc_id = _seed_personal_doc(user_id)

    monkeypatch.setattr(settings, "node_kind", "company")
    try:
        export_personal_document(doc_id, user_id, settings)
    except ValueError as exc:
        assert "personal-node only" in str(exc)
    else:
        raise AssertionError("company node must not export personal promotion package")

    package = PromotionPackage(
        source_node_id="personal-a",
        source_doc_id=doc_id,
        source_owner_id=user_id,
        source_scope="personal",
        source_title="Sanitized",
        sanitized_title="Sanitized",
        sanitized_markdown="Sanitized body",
        target_project="company",
    )
    monkeypatch.setattr(settings, "node_kind", "personal")
    try:
        stage_promotion_package(package, user_id, settings)
    except ValueError as exc:
        assert "company-node only" in str(exc)
    else:
        raise AssertionError("personal node must not stage promotion package")


def test_promote_api_exports_stages_and_approves(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    doc_id = _seed_personal_doc(user_id)

    exported = client.post(
        "/promote/export",
        json={"doc_id": str(doc_id)},
        headers={"X-User-Id": str(user_id)},
    )
    assert exported.status_code == 200

    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    staged = client.post(
        "/promote/stage",
        json=exported.json(),
        headers={"X-User-Id": str(user_id)},
    )
    assert staged.status_code == 200
    assert staged.json()["status"] == "pending"
    assert staged.json()["source_meta"]["export_sanitization"]["markdown"]["changed"] is True
    assert staged.json()["source_meta"]["central_sanitization"]["markdown"]["changed"] is False

    listed = client.get("/promote/stage", headers={"X-User-Id": str(user_id)})
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    approved = client.post(
        f"/promote/stage/{staged.json()['stage_id']}/approve",
        headers={"X-User-Id": str(user_id)},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["promoted_doc_id"]
    assert [event["node"] for event in approved.json()["audit_events"]] == [
        "promote.stage",
        "promote.approve",
    ]


def test_session_member_cannot_stage_list_approve_or_reject_promotion(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8820")
    monkeypatch.setattr(settings, "auth_web_base_url", "http://localhost:3820")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_central")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "central_admin_emails", "admin@example.com")

    admin_client = TestClient(app)
    assert (
        admin_client.post("/auth/dev-login", json={"email": "admin@example.com"}).status_code == 200
    )
    invited = admin_client.post(
        "/auth/allowlist",
        json={"email": "member@example.com", "role": "member"},
    )
    assert invited.status_code == 201

    package = PromotionPackage(
        source_node_id="personal-a",
        source_doc_id=uuid.uuid4(),
        source_owner_id=uuid.uuid4(),
        source_scope="personal",
        source_title="Share candidate",
        sanitized_title="Share candidate",
        sanitized_markdown="Sanitized body",
        target_project="company",
    )
    staged = admin_client.post("/promote/stage", json=package.model_dump(mode="json"))
    assert staged.status_code == 200
    stage_id = staged.json()["stage_id"]

    member_client = TestClient(app)
    login = member_client.post("/auth/dev-login", json={"email": "member@example.com"})
    assert login.status_code == 200
    assert login.json()["role"] == "member"

    list_res = member_client.get("/promote/stage")
    assert list_res.status_code == 403
    assert list_res.json()["detail"] == "node operator required"

    stage_res = member_client.post("/promote/stage", json=package.model_dump(mode="json"))
    assert stage_res.status_code == 403
    assert stage_res.json()["detail"] == "node operator required"

    approve = member_client.post(f"/promote/stage/{stage_id}/approve")
    assert approve.status_code == 403
    assert approve.json()["detail"] == "node operator required"

    reject = member_client.post(f"/promote/stage/{stage_id}/reject")
    assert reject.status_code == 403
    assert reject.json()["detail"] == "node operator required"
