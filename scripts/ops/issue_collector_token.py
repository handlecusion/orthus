#!/usr/bin/env python
"""Issue a P8.2 collector token for the personal collector daemon.

Node-local operator helper. Resolves the owner user (by --user-email via
auth_identities/users, or by explicit --user-id), generates a random
`dct_<urlsafe>` bearer token, stores ONLY its sha256 hash in `collector_tokens`,
and prints the plaintext exactly once to stdout. The plaintext is never written
to the DB or logged; store it in the daemon's local Keychain immediately.

P8.7a: --scopes assigns the token's scope vocabulary (default `ingest`).
"""

from __future__ import annotations

import argparse
import secrets
import sys
import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import insert, select

from orthus.auth import normalize_email
from orthus.collector.auth import ALLOWED_SCOPES, COLLECTOR_TOKEN_PREFIX, hash_collector_token
from orthus.db import session
from orthus.settings import Settings, get_settings
from orthus.tables import auth_identities, collector_tokens, users

# 32 random bytes -> ~43 urlsafe chars, comfortably above the 32-byte minimum.
TOKEN_BYTES = 32


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    node_id = (args.node or settings.node_id).strip()
    if not node_id:
        raise SystemExit("node required")

    scopes = _parse_scopes(args.scopes)
    user_id = _resolve_user_id(args)
    plaintext = f"{COLLECTOR_TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"
    token_id = uuid.uuid4()
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=token_id,
                user_id=user_id,
                node_id=node_id,
                name=(args.name or "").strip(),
                token_hash=hash_collector_token(plaintext),
                scopes=scopes,
                created_at=now,
            )
        )
        s.commit()

    print(
        f"node={node_id} user_id={user_id} token_id={token_id} "
        f"name={(args.name or '').strip()} scopes={','.join(scopes)}"
    )
    print(plaintext)
    print("store this token now; it is shown only once and is not recoverable", file=sys.stderr)
    return 0


def _parse_scopes(raw: str) -> list[str]:
    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    if not scopes:
        raise SystemExit("--scopes must list at least one scope")
    unknown = [s for s in scopes if s not in ALLOWED_SCOPES]
    if unknown:
        allowed = ",".join(sorted(ALLOWED_SCOPES))
        raise SystemExit(f"unknown scope(s) {','.join(unknown)}; allowed: {allowed}")
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(scopes))


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
    parser = argparse.ArgumentParser(description="Issue a P8.2 collector token")
    parser.add_argument("--node")
    parser.add_argument("--user-email")
    parser.add_argument("--user-id")
    parser.add_argument("--name", default="")
    parser.add_argument(
        "--scopes",
        default="ingest",
        help=f"comma-separated scopes (allowed: {','.join(sorted(ALLOWED_SCOPES))})",
    )
    return parser.parse_args(argv)


def issue_for_settings(
    settings: Settings,
    *,
    user_id: UUID,
    node_id: str | None = None,
    name: str = "",
    scopes: list[str] | None = None,
) -> str:
    """Programmatic issue helper (tests/bootstrap). Returns the plaintext token."""
    plaintext = f"{COLLECTOR_TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=(node_id or settings.node_id),
                name=name.strip(),
                token_hash=hash_collector_token(plaintext),
                scopes=list(scopes) if scopes else ["ingest"],
                created_at=now,
            )
        )
        s.commit()
    return plaintext


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
