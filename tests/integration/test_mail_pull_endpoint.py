"""POST /mail/pull — on-demand company-mail pull-ingest ('새로고침' trigger)."""

from __future__ import annotations

from fastapi.testclient import TestClient

import orthus.api.routes.mail as mail_routes
from orthus.api.main import app
from orthus.mail.pull import PullIngestResult
from orthus.settings import get_settings

client = TestClient(app)
DEMO_USER = "00000000-0000-4000-8000-000000000001"
DEMO_HEADERS = {"X-User-Id": DEMO_USER}


def _company(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")


def test_mail_pull_runs_and_aggregates(clean, monkeypatch):
    _company(monkeypatch)
    monkeypatch.setattr(
        mail_routes,
        "pull_ingest_all",
        lambda s: [
            PullIngestResult(
                backend="acme", enabled=True, listed=3, ingested=2, skipped=1, errors=0
            ),
            PullIngestResult(
                backend="nova", enabled=True, listed=0, ingested=0, skipped=0, errors=0
            ),
        ],
    )
    r = client.post("/mail/pull", headers=DEMO_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["ingested"] == 2
    assert len(body["backends"]) == 2
    assert body["backends"][0]["backend"] == "acme"
    assert body["backends"][0]["ingested"] == 2


def test_mail_pull_404_on_personal(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "personal")
    called = False

    def _should_not_run(_s):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(mail_routes, "pull_ingest_all", _should_not_run)
    r = client.post("/mail/pull", headers=DEMO_HEADERS)
    assert r.status_code == 404
    assert called is False
