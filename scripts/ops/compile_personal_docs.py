#!/usr/bin/env python
"""P8.4 central compile for one owner's personal documents (operator helper).

Resolves the owner user (by --user-email via auth_identities/users, or by
explicit --user-id), then runs `compile_personal_documents`: corpus index +
wiki authoring for personal docs the collector pushed but that are not yet
compiled. Idempotent — a second run on a clean owner reports zeros. Prints the
{indexed, authored, skipped} counts.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from sqlalchemy import select

from orthus.auth import normalize_email
from orthus.collector.compile import compile_personal_documents
from orthus.db import session
from orthus.tables import auth_identities, users


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    user_id = _resolve_user_id(args)
    result = compile_personal_documents(user_id, limit=args.limit)
    print(
        f"user_id={user_id} indexed={result.indexed} "
        f"authored={result.authored} skipped={result.skipped}"
    )
    return 0


def _resolve_user_id(args: argparse.Namespace) -> UUID:
    if args.user_id and args.user_email:
        raise SystemExit("use --user-id or --user-email, not both")
    if args.user_id:
        try:
            return UUID(args.user_id)
        except ValueError as exc:
            raise SystemExit(f"invalid --user-id: {args.user_id}") from exc
    if args.user_email:
        return _user_id_for_email(args.user_email)
    raise SystemExit("--user-id or --user-email required")


def _user_id_for_email(email: str) -> UUID:
    normalized = normalize_email(email)
    with session() as s:
        row = s.execute(
            select(auth_identities.c.user_id)
            .join(users, users.c.user_id == auth_identities.c.user_id)
            .where(auth_identities.c.email == normalized)
            .order_by(auth_identities.c.created_at.desc())
            .limit(1)
        ).first()
    if not row:
        raise SystemExit(f"no user found for email {normalized}")
    return row.user_id


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P8.4 central compile for personal documents")
    parser.add_argument("--user-email")
    parser.add_argument("--user-id")
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
