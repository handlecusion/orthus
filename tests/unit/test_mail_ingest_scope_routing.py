"""P6.7.3 per-account ingest_scope routing in `resolve_ingest_route`.

`docs/p6-unified-mail.md` §12.2/§12.11. A mail account row's `ingest_scope` decides
the `(scope, owner_id)`:
  - `owner`   -> `personal` bound to the registrant (account.owner_id). REQUIRES
    `ORTHUS_OWNER_SCOPE_ENABLED`; otherwise `MailIngestScopeError` (fail-closed).
  - `company` -> `company`, owner resolved from `owner_addr` (audit only).
The no-account env path delegates to `ingest_scope_and_owner` (#321, per-backend
`mail_kind`) and is covered by the individual-scope tests; here we pin the
multi-account override `resolve_ingest_route` layers on top.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from orthus.mail.backends import MailIngestScopeError
from orthus.mail.ingest import resolve_ingest_route
from orthus.settings import get_settings

# resolve_ingest_route only reads payload.backend + payload.owner_addr.
_PAYLOAD = SimpleNamespace(backend="nova", owner_addr="me@nova.example")


def _account(ingest_scope: str, owner_id=None) -> dict:
    return {
        "connector_slug": "mail_nova",
        "owner_id": owner_id or uuid4(),
        "settings_redacted": {"ingest_scope": ingest_scope, "owner_addr": "me@nova.example"},
    }


def test_owner_account_scope_is_personal_with_registrant(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    owner = uuid4()
    scope, owner_id = resolve_ingest_route(_PAYLOAD, settings, account=_account("owner", owner))
    assert scope == "personal"
    assert owner_id == owner  # owner-scope binds the registrant, not owner_addr.


def test_owner_account_scope_requires_owner_scope_flag(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "owner_scope_enabled", False)
    # Fail-closed: never silently downgrade an owner-scope mailbox to company-wide.
    with pytest.raises(MailIngestScopeError):
        resolve_ingest_route(_PAYLOAD, settings, account=_account("owner"))


def test_company_account_scope_is_company(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "owner_scope_enabled", False)  # not required for company.
    fixed = uuid4()
    monkeypatch.setattr("orthus.mail.ingest.resolve_mail_owner_user_id", lambda *a, **k: fixed)
    scope, owner_id = resolve_ingest_route(_PAYLOAD, settings, account=_account("company"))
    assert scope == "company"
    assert owner_id == fixed  # company keeps an importing user_id for audit.


def test_unknown_account_scope_defaults_to_owner(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    owner = uuid4()
    # account_ingest_scope defaults unknown/blank to "owner" (safe default).
    scope, owner_id = resolve_ingest_route(_PAYLOAD, settings, account=_account("", owner))
    assert scope == "personal"
    assert owner_id == owner
