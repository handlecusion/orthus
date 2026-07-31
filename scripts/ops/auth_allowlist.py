#!/usr/bin/env python
"""Node-local auth allowlist ops.

This is a local Mac mini break-glass helper. The web/API allowlist route remains
the normal admin path; this script only touches the current node loaded from
`~/.orthus/nodes/<node>/node.env`.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.auth import ensure_bootstrap_allowlist, normalize_email
from orthus.db import session
from orthus.settings import Settings, get_settings
from orthus.tables import auth_allowlist

ALLOWED_ROLES = {"owner", "admin", "member", "viewer"}
MANAGER_ROLES = {"owner", "admin"}


@dataclass(frozen=True)
class AllowlistRow:
    email: str
    role: str
    revoked: bool


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()

    if args.action == "list":
        rows = list_allowlist(settings)
        active = [row for row in rows if not row.revoked]
        print(f"node={settings.node_id} active={len(active)} total={len(rows)}")
        for row in rows:
            status = "revoked" if row.revoked else "active"
            print(f"{mask_email(row.email)} {row.role} {status}")
        return 0

    email = _email_from_args(args)
    role = (args.role or os.environ.get("ROLE") or "member").strip().lower()
    if role not in ALLOWED_ROLES:
        raise SystemExit(f"invalid ROLE: {role}; expected one of {sorted(ALLOWED_ROLES)}")
    try:
        row = upsert_allowlist(settings, email=email, role=role)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"node={settings.node_id} upserted={mask_email(row.email)} role={row.role} active")
    return 0


def list_allowlist(settings: Settings) -> list[AllowlistRow]:
    ensure_bootstrap_allowlist(settings)
    with session() as s:
        rows = s.execute(
            select(auth_allowlist.c.email, auth_allowlist.c.role, auth_allowlist.c.revoked_at)
            .where(auth_allowlist.c.node_id == settings.node_id)
            .order_by(auth_allowlist.c.email.asc())
        ).all()
    return [
        AllowlistRow(
            email=str(row.email),
            role=str(row.role),
            revoked=row.revoked_at is not None,
        )
        for row in rows
    ]


def upsert_allowlist(settings: Settings, *, email: str, role: str) -> AllowlistRow:
    normalized = normalize_email(email)
    if role not in ALLOWED_ROLES:
        raise ValueError(f"invalid role: {role}")
    ensure_bootstrap_allowlist(settings)
    now = datetime.now(UTC)
    with session() as s:
        current = _current_allowlist_row(s, settings.node_id, normalized)
        if current and current.role in MANAGER_ROLES and role not in MANAGER_ROLES:
            raise ValueError("node-local helper cannot demote an active owner/admin")
        stmt = pg_insert(auth_allowlist).values(
            allowlist_id=uuid.uuid4(),
            node_id=settings.node_id,
            email=normalized,
            role=role,
            created_by=None,
            created_at=now,
            revoked_at=None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[auth_allowlist.c.node_id, auth_allowlist.c.email],
            set_={"role": role, "revoked_at": None},
        ).returning(auth_allowlist.c.email, auth_allowlist.c.role, auth_allowlist.c.revoked_at)
        row = s.execute(stmt).first()
        s.commit()
    if not row:
        raise RuntimeError("allowlist upsert failed")
    return AllowlistRow(
        email=str(row.email), role=str(row.role), revoked=row.revoked_at is not None
    )


def _current_allowlist_row(s, node_id: str, email: str) -> AllowlistRow | None:
    row = s.execute(
        select(auth_allowlist.c.email, auth_allowlist.c.role, auth_allowlist.c.revoked_at).where(
            auth_allowlist.c.node_id == node_id,
            auth_allowlist.c.email == email,
            auth_allowlist.c.revoked_at.is_(None),
        )
    ).first()
    if not row:
        return None
    return AllowlistRow(email=str(row.email), role=str(row.role), revoked=False)


def mask_email(email: str) -> str:
    local, sep, domain = normalize_email(email).partition("@")
    if not sep:
        return "<invalid>"
    if len(local) <= 2:
        prefix = local[:1]
    else:
        prefix = local[:2]
    return f"{prefix}{'*' * 6}@{domain}"


def _email_from_args(args: argparse.Namespace) -> str:
    value = (args.email or os.environ.get("EMAIL") or "").strip()
    file_value = (args.email_file or os.environ.get("EMAIL_FILE") or "").strip()
    if value and file_value:
        raise SystemExit("use EMAIL or EMAIL_FILE, not both")
    if file_value:
        value = Path(file_value).read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit("EMAIL or EMAIL_FILE required for upsert")
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Node-local auth allowlist ops")
    parser.add_argument("action", choices=["list", "upsert"])
    parser.add_argument("--email")
    parser.add_argument("--email-file")
    parser.add_argument("--role", choices=sorted(ALLOWED_ROLES))
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
