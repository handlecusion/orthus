"""WTR.2: interactive wiki task resolve (POST /wiki/tasks/{slug}/resolve)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from orthus.api.main import app
from orthus.api.routes import wiki as wiki_routes
from orthus.auth import AuthenticatedUser
from orthus.db import session
from orthus.schemas.canonical import WikiClaim, WikiTask
from orthus.settings import get_settings
from orthus.tables import audit_log
from orthus.wiki import store

client = TestClient(app)


# --- helpers ----------------------------------------------------------------


def _company(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "node_kind", "company")


def _headers(user_id) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _write_existing_claim(user_id, slug: str, text: str) -> None:
    store.write_claim(
        WikiClaim(
            slug=slug,
            claim=text,
            supporting=[],
            conflicting=[],
            confidence="medium",
            last_reviewed=date(2026, 5, 1),
            related_pages=[],
            evidence="evidence",
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )


def _write_conflict_task(user_id, *, slug: str, claim_slug: str, incoming: str | None) -> None:
    store.write_task(
        WikiTask(
            slug=slug,
            kind="conflict",
            description=f"Claim '{claim_slug}' differs.",
            related=[claim_slug],
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
            resolved=False,
            incoming_claim=incoming,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )


def _role_override(user_id, role: str):
    return lambda: AuthenticatedUser(
        user_id=user_id,
        auth_mode="session",
        display_name="Member",
        role=role,
        node_id="company",
    )


# --- conflict write-back -----------------------------------------------------


def test_resolve_keep_existing_leaves_claim(user_id, monkeypatch):
    _company(monkeypatch)
    _write_existing_claim(user_id, "claim-x", "original text")
    _write_conflict_task(
        user_id, slug="conflict-claim-x", claim_slug="claim-x", incoming="other text"
    )

    res = client.post(
        "/wiki/tasks/conflict-claim-x/resolve",
        json={"decision": "keep_existing"},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["resolved"] is True
    assert body["resolution"]["decision"] == "keep_existing"
    claim = store.load_claim("claim-x", scope="company")
    assert claim is not None
    assert claim.claim == "original text"  # unchanged


def test_resolve_use_incoming_writes_incoming(user_id, monkeypatch):
    _company(monkeypatch)
    _write_existing_claim(user_id, "claim-y", "original text")
    _write_conflict_task(
        user_id, slug="conflict-claim-y", claim_slug="claim-y", incoming="incoming text"
    )

    res = client.post(
        "/wiki/tasks/conflict-claim-y/resolve",
        json={"decision": "use_incoming"},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["resolution"]["decision"] == "use_incoming"
    assert body["resolution"]["produced_claim_slugs"] == ["claim-y"]
    claim = store.load_claim("claim-y", scope="company")
    assert claim is not None
    assert claim.claim == "incoming text"
    assert claim.last_reviewed == date.today()


def test_resolve_merge_writes_merged_text(user_id, monkeypatch):
    _company(monkeypatch)
    _write_existing_claim(user_id, "claim-z", "original text")
    _write_conflict_task(
        user_id, slug="conflict-claim-z", claim_slug="claim-z", incoming="incoming text"
    )

    res = client.post(
        "/wiki/tasks/conflict-claim-z/resolve",
        json={"decision": "merge", "merged_text": "merged final text"},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    claim = store.load_claim("claim-z", scope="company")
    assert claim is not None
    assert claim.claim == "merged final text"


def test_resolve_merge_without_text_is_422(user_id, monkeypatch):
    _company(monkeypatch)
    _write_existing_claim(user_id, "claim-m", "original")
    _write_conflict_task(user_id, slug="conflict-claim-m", claim_slug="claim-m", incoming="x")

    res = client.post(
        "/wiki/tasks/conflict-claim-m/resolve",
        json={"decision": "merge"},
        headers=_headers(user_id),
    )

    assert res.status_code == 422


def test_resolve_use_incoming_without_incoming_is_422(user_id, monkeypatch):
    _company(monkeypatch)
    _write_existing_claim(user_id, "claim-n", "original")
    _write_conflict_task(user_id, slug="conflict-claim-n", claim_slug="claim-n", incoming=None)

    res = client.post(
        "/wiki/tasks/conflict-claim-n/resolve",
        json={"decision": "use_incoming"},
        headers=_headers(user_id),
    )

    assert res.status_code == 422
    assert "no incoming text recorded" in res.json()["detail"]


# --- open_question answered ---------------------------------------------------


def test_resolve_answered_produces_claim(user_id, monkeypatch):
    _company(monkeypatch)
    store.write_task(
        WikiTask(
            slug="oq-vacation",
            kind="open_question",
            description="휴가는 며칠인가?",
            related=["vacation-policy"],
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )

    res = client.post(
        "/wiki/tasks/oq-vacation/resolve",
        json={"decision": "answered", "answer": "연차는 15일이다."},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["resolved"] is True
    assert body["resolution"]["decision"] == "answered"
    produced = body["resolution"]["produced_claim_slugs"]
    assert produced
    claim = store.load_claim(produced[0], scope="company")
    assert claim is not None


# --- cleanup / dismissed -----------------------------------------------------


def test_resolve_cleanup_flips_task(user_id, monkeypatch):
    _company(monkeypatch)
    store.write_task(
        WikiTask(
            slug="stale-task",
            kind="stale_audit",
            description="Stale entry.",
            related=[],
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )

    res = client.post(
        "/wiki/tasks/stale-task/resolve",
        json={"decision": "cleanup"},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    assert res.json()["resolution"]["decision"] == "cleanup"
    loaded = store.load_task("stale-task", scope="company")
    assert loaded is not None and loaded.resolved is True


def test_resolve_dismissed_any_kind(user_id, monkeypatch):
    _company(monkeypatch)
    _write_existing_claim(user_id, "claim-d", "original")
    _write_conflict_task(
        user_id, slug="conflict-claim-d", claim_slug="claim-d", incoming="incoming"
    )

    res = client.post(
        "/wiki/tasks/conflict-claim-d/resolve",
        json={"decision": "dismissed", "note": "not relevant"},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    assert res.json()["resolution"]["decision"] == "dismissed"
    assert res.json()["resolution"]["note"] == "not relevant"
    claim = store.load_claim("claim-d", scope="company")
    assert claim is not None and claim.claim == "original"  # no write-back


# --- decision/kind mismatch ---------------------------------------------------


def test_resolve_conflict_decision_on_open_question_is_422(user_id, monkeypatch):
    _company(monkeypatch)
    store.write_task(
        WikiTask(
            slug="oq-mismatch",
            kind="open_question",
            description="질문?",
            related=[],
            created_at=datetime(2026, 6, 4, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )

    res = client.post(
        "/wiki/tasks/oq-mismatch/resolve",
        json={"decision": "keep_existing"},
        headers=_headers(user_id),
    )

    assert res.status_code == 422


# --- permission gate ----------------------------------------------------------


def test_member_role_blocked_on_mutation_but_can_dismiss(user_id, monkeypatch):
    _company(monkeypatch)
    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    _write_existing_claim(user_id, "claim-p", "original")
    _write_conflict_task(
        user_id, slug="conflict-claim-p", claim_slug="claim-p", incoming="incoming"
    )

    app.dependency_overrides[wiki_routes.get_current_user] = _role_override(user_id, "member")
    try:
        blocked = client.post(
            "/wiki/tasks/conflict-claim-p/resolve",
            json={"decision": "use_incoming"},
        )
        allowed = client.post(
            "/wiki/tasks/conflict-claim-p/resolve",
            json={"decision": "dismissed"},
        )
    finally:
        app.dependency_overrides.pop(wiki_routes.get_current_user, None)

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    claim = store.load_claim("claim-p", scope="company")
    assert claim is not None and claim.claim == "original"


def test_owner_role_allowed_on_mutation(user_id, monkeypatch):
    _company(monkeypatch)
    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    _write_existing_claim(user_id, "claim-o", "original")
    _write_conflict_task(
        user_id, slug="conflict-claim-o", claim_slug="claim-o", incoming="incoming"
    )

    app.dependency_overrides[wiki_routes.get_current_user] = _role_override(user_id, "owner")
    try:
        res = client.post(
            "/wiki/tasks/conflict-claim-o/resolve",
            json={"decision": "use_incoming"},
        )
    finally:
        app.dependency_overrides.pop(wiki_routes.get_current_user, None)

    assert res.status_code == 200
    claim = store.load_claim("claim-o", scope="company")
    assert claim is not None and claim.claim == "incoming"


# --- reopen audit + resolve audit --------------------------------------------


def test_reopen_records_audit(user_id, monkeypatch):
    _company(monkeypatch)
    store.write_task(
        WikiTask(
            slug="reopen-task",
            kind="conflict",
            description="To reopen.",
            related=[],
            created_at=datetime(2026, 6, 5, tzinfo=UTC),
            resolved=True,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )

    res = client.patch(
        "/wiki/tasks/reopen-task",
        json={"resolved": False},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    assert res.json()["resolved"] is False
    with session() as s:
        rows = s.execute(
            select(audit_log.c.meta).where(audit_log.c.node == "wiki.task.reopen")
        ).all()
    metas = [r.meta for r in rows]
    assert any(m and m.get("slug") == "reopen-task" for m in metas)


def test_resolve_records_audit(user_id, monkeypatch):
    _company(monkeypatch)
    _write_existing_claim(user_id, "claim-a1", "original")
    _write_conflict_task(
        user_id, slug="conflict-claim-a1", claim_slug="claim-a1", incoming="incoming"
    )

    res = client.post(
        "/wiki/tasks/conflict-claim-a1/resolve",
        json={"decision": "use_incoming"},
        headers=_headers(user_id),
    )

    assert res.status_code == 200
    with session() as s:
        rows = s.execute(
            select(audit_log.c.meta).where(audit_log.c.node == "wiki.task.resolve")
        ).all()
    metas = [r.meta for r in rows]
    assert any(
        m and m.get("slug") == "conflict-claim-a1" and m.get("decision") == "use_incoming"
        for m in metas
    )


# --- round-trip with resolution field ----------------------------------------


def test_resolution_roundtrip(user_id, monkeypatch, tmp_path):
    resolution_uid = uuid.uuid4()
    task = WikiTask(
        slug="rt-task",
        kind="conflict",
        description="round-trip",
        related=["claim-rt"],
        created_at=datetime(2026, 6, 6, tzinfo=UTC),
        resolved=True,
        incoming_claim="incoming rt",
    )
    from orthus.schemas.canonical import WikiTaskResolution

    task = task.model_copy(
        update={
            "resolution": WikiTaskResolution(
                decision="use_incoming",
                note="rt note",
                resolved_by=resolution_uid,
                resolved_at=datetime(2026, 6, 6, 12, tzinfo=UTC),
                produced_claim_slugs=["claim-rt"],
            )
        }
    )
    store.write_task(
        task, user_id=user_id, scope="company", owner_id=None, project="company", root=tmp_path
    )
    loaded = store.load_task("rt-task", scope="company", owner_id=None, root=tmp_path)
    assert loaded is not None
    assert loaded.incoming_claim == "incoming rt"
    assert loaded.resolution is not None
    assert loaded.resolution.decision == "use_incoming"
    assert loaded.resolution.produced_claim_slugs == ["claim-rt"]
