from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import auth_allowlist, users


def _load_module():
    path = Path(__file__).parents[2] / "scripts" / "ops" / "auth_allowlist.py"
    spec = importlib.util.spec_from_file_location("auth_allowlist_ops", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mask_email_does_not_return_full_local_part():
    module = _load_module()

    assert module.mask_email("MaskedMe@Gmail.com") == "ma******@gmail.com"
    assert module.mask_email("y@example.com") == "y******@example.com"


def test_upsert_allowlist_scopes_to_current_node_and_reactivates(clean):
    module = _load_module()
    settings = get_settings()
    settings.node_id = "company"
    settings.node_kind = "company"
    settings.central_admin_emails = ""

    row = module.upsert_allowlist(settings, email="Member@Example.com", role="member")

    assert row.email == "member@example.com"
    assert row.role == "member"
    assert row.revoked is False
    with session() as s:
        stored = s.execute(
            select(auth_allowlist.c.node_id, auth_allowlist.c.email, auth_allowlist.c.role)
        ).one()
    assert stored.node_id == "company"
    assert stored.email == "member@example.com"
    assert stored.role == "member"

    row = module.upsert_allowlist(settings, email="member@example.com", role="admin")

    assert row.role == "admin"
    with session() as s:
        rows = s.execute(select(auth_allowlist.c.email, auth_allowlist.c.role)).all()
    assert [(r.email, r.role) for r in rows] == [("member@example.com", "admin")]


def test_list_allowlist_masks_bootstrap_role(clean):
    module = _load_module()
    settings = get_settings()
    settings.node_id = "company"
    settings.node_kind = "company"
    settings.central_admin_emails = "owner@example.com"

    rows = module.list_allowlist(settings)

    assert [(row.email, row.role, row.revoked) for row in rows] == [
        ("owner@example.com", "admin", False)
    ]


@pytest.mark.parametrize(
    ("existing_role", "new_role"),
    [("admin", "member"), ("owner", "viewer")],
)
def test_upsert_refuses_active_manager_demote(clean, existing_role, new_role):
    module = _load_module()
    settings = get_settings()
    settings.node_id = "company"
    settings.node_kind = "company"
    settings.central_admin_emails = ""
    module.upsert_allowlist(settings, email="owner@example.com", role=existing_role)

    with pytest.raises(ValueError, match="cannot demote"):
        module.upsert_allowlist(settings, email="owner@example.com", role=new_role)

    with session() as s:
        row = s.execute(
            select(auth_allowlist.c.email, auth_allowlist.c.role)
            .where(auth_allowlist.c.email == "owner@example.com")
            .limit(1)
        ).one()
    assert (row.email, row.role) == ("owner@example.com", existing_role)


def test_main_reports_manager_demote_without_traceback(clean, monkeypatch):
    module = _load_module()
    settings = get_settings()
    settings.node_id = "company"
    settings.node_kind = "company"
    settings.central_admin_emails = ""
    module.upsert_allowlist(settings, email="owner@example.com", role="admin")
    monkeypatch.setenv("EMAIL", "owner@example.com")
    monkeypatch.setenv("ROLE", "member")

    with pytest.raises(SystemExit, match="cannot demote"):
        module.main(["upsert"])


def test_upsert_preserves_existing_created_by(clean):
    module = _load_module()
    settings = get_settings()
    settings.node_id = "company"
    settings.node_kind = "company"
    settings.central_admin_emails = ""
    creator_id = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=creator_id, display_name="Creator"))
        s.execute(
            insert(auth_allowlist).values(
                allowlist_id=uuid.uuid4(),
                node_id="company",
                email="member@example.com",
                role="member",
                created_by=creator_id,
                revoked_at=None,
            )
        )
        s.commit()

    module.upsert_allowlist(settings, email="member@example.com", role="admin")

    with session() as s:
        row = s.execute(
            select(auth_allowlist.c.role, auth_allowlist.c.created_by)
            .where(auth_allowlist.c.email == "member@example.com")
            .limit(1)
        ).one()
    assert row.role == "admin"
    assert row.created_by == creator_id
