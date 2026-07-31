from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.federation.query import federated_wiki_answer
from orthus.models.adapters.mock import MockChat
from orthus.router import answer
from orthus.schemas.canonical import AssistantResult, ValidationResult, WikiPage, WikiSourceRef
from orthus.settings import get_settings
from orthus.wiki import store

client = TestClient(app)


def _executed_result(question: str, label: str, rows: list[list]) -> AssistantResult:
    return AssistantResult(
        query_id=uuid.uuid4(),
        question=question,
        compiled=None,
        validation=ValidationResult(
            parsed=True, statement_kind="select", schema_ok=True, read_only_ok=True, explain_ok=True
        ),
        status="executed",
        columns=["label"],
        rows=rows,
        row_count=len(rows),
        message=label,
    )


def test_federation_routes_require_company_service_token(user_id):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.federation_service_token = ""

    disabled = client.post("/federation/wiki/retrieve", json={"question": "x"})
    assert disabled.status_code == 404

    settings.federation_service_token = "secret"
    missing = client.post("/federation/wiki/retrieve", json={"question": "x"})
    assert missing.status_code == 401

    ok = client.post(
        "/federation/wiki/retrieve",
        headers={"Authorization": "Bearer secret"},
        json={"question": "x"},
    )
    assert ok.status_code == 200
    assert ok.json() == []


def test_federation_wiki_page_reads_company_scope(user_id):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.federation_service_token = "secret"
    store.write_page(
        WikiPage(
            slug="company-policy",
            title="Company Policy",
            definition="Company definition",
            overview="Company overview",
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    store.write_page(
        WikiPage(
            slug="personal-policy",
            title="Personal Policy",
            definition="Personal definition",
            overview="Personal overview",
        ),
        user_id=user_id,
        scope="personal",
        owner_id=user_id,
        project="company",
    )

    ok = client.get(
        "/federation/wiki/pages/company-policy",
        headers={"Authorization": "Bearer secret"},
    )
    personal = client.get(
        "/federation/wiki/pages/personal-policy",
        headers={"Authorization": "Bearer secret"},
    )

    assert ok.status_code == 200
    assert ok.json()["title"] == "Company Policy"
    assert personal.status_code == 404


def test_federated_wiki_answer_joins_local_and_central_hits(user_id, monkeypatch):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"

    local_hit = WikiSourceRef(
        page_slug="personal-note",
        title="Personal Note",
        kind="page",
        excerpt="local personal context",
        score=0.8,
        source_scope="personal",
        source_node_id="personal-a",
    )
    central_hit = WikiSourceRef(
        page_slug="company-policy",
        title="Company Policy",
        kind="page",
        excerpt="central company context",
        score=0.9,
        source_scope="company",
        source_node_id="company",
    )
    monkeypatch.setattr("orthus.federation.query.retrieve", lambda *a, **k: [local_hit])
    monkeypatch.setattr(
        "orthus.federation.client.retrieve_company_wiki", lambda *a, **k: [central_hit]
    )
    authored = []
    monkeypatch.setattr("orthus.wiki.qa.author_from_qa", lambda *a, **k: authored.append(True))

    result = federated_wiki_answer(
        user_id,
        "같이 찾아줘",
        chat_model=MockChat(default="joined answer"),
    )

    assert result.answer == "joined answer"
    assert [source.source_scope for source in result.sources] == ["company", "personal"]
    assert authored == [], "central-grounded answers must not mutate personal wiki"


def test_personal_ask_structured_returns_scope_parts(user_id, monkeypatch):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"

    monkeypatch.setattr("orthus.router.classify", lambda *a, **k: "structured")
    monkeypatch.setattr(
        "orthus.federation.query.query_structured",
        lambda uid, question, **kwargs: _executed_result(question, "local", [["personal"]]),
    )
    monkeypatch.setattr(
        "orthus.federation.client.query_company_structured",
        lambda uid, question, **kwargs: _executed_result(question, "central", [["company"]]),
    )

    result = answer(user_id, "상태별 개수", scope="all")

    assert result.mode == "structured"
    assert result.structured is not None
    assert result.structured.message == "central"
    assert [(part.source_scope, part.result.rows) for part in result.structured_parts] == [
        ("personal", [["personal"]]),
        ("company", [["company"]]),
    ]
