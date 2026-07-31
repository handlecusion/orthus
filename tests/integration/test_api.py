import uuid

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api import routes
from orthus.api.knowledge_token import get_session_user_or_knowledge_token
from orthus.api.main import app
from orthus.auth import AuthenticatedUser
from orthus.connectors.base import Connector
from orthus.connectors.manifest import ConnectorManifest, FactoryConnectorProvider
from orthus.connectors.registry import register_connector_provider
from orthus.connectors.state import ConnectorAccountSpec, create_connector_account
from orthus.db import session
from orthus.schemas.canonical import (
    AssistantResult,
    CompiledQuery,
    InternalDocument,
    RoutedAnswer,
    ValidationResult,
    WikiPage,
)
from orthus.settings import get_settings
from orthus.tables import (
    agent_work_items,
    audit_log,
    connector_runs,
    corpus_chunks,
    documents,
    notion_rows,
    personal_board_tasks,
    query_runs,
    users,
)
from orthus.wiki import store

client = TestClient(app)


def _new_chat(headers: dict | None = None) -> str:
    """Create an owner-scoped agent-work chat session and return its id."""
    created = client.post("/agent-work/chats", json={}, headers=headers)
    assert created.status_code == 200, created.text
    return created.json()["session_id"]


def _orchestrate(question: str, session_id: str, *, headers: dict | None = None, **body):
    """POST a command/compound question to the agent-work chat orchestrator — the home
    of command intake + decompose, moved off the search-only /ask."""
    return client.post(
        f"/agent-work/chats/{session_id}/orchestrate",
        json={"text": question, **body},
        headers=headers,
    )


class _AgentWorkMemConnector(Connector):
    def __init__(self, slug: str):
        self.slug = slug

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        del since
        yield InternalDocument(
            title="Agent Work connector item",
            markdown="Agent Work connector body",
            source=self.slug,
            source_external_id="item-1",
            source_last_edited_at=datetime(2026, 6, 6, tzinfo=UTC),
            project="company",
            wiki_authoring=False,
        )


def _seed_ticket(user_id) -> None:
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=uuid.uuid4(),
                db_id="db-tickets",
                db_name="티켓",
                properties={"상태": "진행중"},
                scope="company",
                owner_id=None,
                user_id=user_id,
            )
        )
        s.commit()


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_document_crud_and_wiki_exposure(user_id):
    r = client.post(
        "/documents",
        json={
            "title": "휴가 정책",
            "block_json": [{"type": "paragraph"}],
            "markdown": "연차 휴가는 입사 1년차부터 15일 부여된다.",
        },
    )
    assert r.status_code == 201
    doc_id = r.json()["doc_id"]

    got = client.get(f"/documents/{doc_id}")
    assert got.status_code == 200
    assert got.json()["title"] == "휴가 정책"

    assert any(d["doc_id"] == doc_id for d in client.get("/documents").json())

    # saved doc is searchable through the wiki (editor save -> corpus -> RAG)
    ask = client.post("/wiki/ask", json={"question": "연차 며칠?", "k": 3})
    assert ask.status_code == 200
    body = ask.json()
    assert body["sources"]
    assert body["wiki_links"]


def test_ask_routes_structured_and_executes(user_id, monkeypatch):
    _seed_ticket(user_id)
    # Force structured routing (no mock keyword heuristic). The structured backend's
    # default compile emits `SELECT 1`, which passes the gate + executes.
    monkeypatch.setattr("orthus.router.classify", lambda *a, **k: "structured")
    r = client.post("/ask", json={"question": "티켓 개수"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "structured"
    assert body["wiki"] is None
    assert body["structured"]["status"] == "executed"
    assert body["structured"]["validation"]["rejected_reason"] is None

    # the structured run is retrievable for audit at its new path.
    qid = body["structured"]["query_id"]
    run = client.get(f"/ask/runs/{qid}")
    assert run.status_code == 200
    assert run.json()["status"] == "executed"
    assert run.json()["source_id"] is None


def test_ask_structured_endpoint_runs_gate(user_id):
    # The recursion-free structured tool surface for orthus-mcp runs query_structured
    # directly (gate inside) and never calls the router. The mock backend compiles
    # `SELECT 1` -> passes the gate -> executes; returns 200 with the structured shape.
    _seed_ticket(user_id)
    r = client.post("/ask/structured", json={"question": "티켓 개수", "scope": "company"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executed"
    assert body["validation"]["rejected_reason"] is None
    assert "compiled" in body


def test_ask_routes_wiki_for_knowledge_question(user_id):
    # unscripted mock classify -> no "route" key -> classify() fail-safes to 'wiki'.
    r = client.post("/ask", json={"question": "연차 정책은 어떻게 동작하나요?"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "wiki"
    assert body["structured"] is None
    assert "answer" in body["wiki"]
    assert "wiki_links" in body["wiki"]


def test_ask_route_override_selects_backend_without_changing_default(user_id, monkeypatch):
    """`route` is a backend SELECTOR: omitting it keeps auto-routing exactly as before,
    while route='wiki' forces wiki grounding regardless of the classifier. This is the
    orthus-mcp wiki_ask fix — a knowledge question the auto-router sends to the structured
    NL→SQL backend must instead ground on the wiki. classify is pinned to 'structured' so
    the contrast is unambiguous: same question, two outcomes driven only by `route`."""
    _seed_ticket(user_id)
    monkeypatch.setattr("orthus.router.classify", lambda *a, **k: "structured")
    q = "회사가 하는 주요 서비스 3가지"

    # No route → unchanged auto-routing → structured (classify verdict honored).
    auto = client.post("/ask", json={"question": q})
    assert auto.status_code == 200
    assert auto.json()["mode"] == "structured"

    # route='wiki' → forces the wiki backend, bypassing classify → wiki grounding.
    forced = client.post("/ask", json={"question": q, "route": "wiki"})
    assert forced.status_code == 200
    body = forced.json()
    assert body["mode"] == "wiki"
    assert body["structured"] is None
    assert body["wiki"] is not None and "answer" in body["wiki"]


def test_ask_invalid_route_is_rejected(user_id):
    """An unknown `route` value is a 422 validation error, not a silent auto-route:
    a misconfigured caller fails loud rather than getting an unexpected backend."""
    r = client.post("/ask", json={"question": "안녕", "route": "banana"})
    assert r.status_code == 422


def test_orchestrate_command_queues_agent_work(user_id):
    sid = _new_chat()
    r = _orchestrate("Gmail 동기화해줘", sid)

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "agent_work"
    assert body["wiki"] is None
    assert body["structured"] is None
    work = body["agent_work"]
    assert work["source_kind"] == "assistant_command"
    assert work["action_family"] == "connector_sync"
    assert work["state"] == "request_more_data"
    assert work["policy_outcome"] == "request_more_data"
    assert "connector_missing_config_or_auth" in work["reason_codes"]

    with session() as s:
        row = s.execute(
            select(agent_work_items).where(agent_work_items.c.work_id == uuid.UUID(work["work_id"]))
        ).first()
    assert row is not None
    assert row._mapping["payload"]["intake"] == "ask"
    assert row._mapping["payload"]["policy_memory"]["used_for_outcome"] is False


def test_ask_is_search_only_does_not_queue_command(clean, user_id):
    """/ask is search-only: a command-shaped request is answered as search, never
    queued as Agent Work (intake moved to the agent-work chat orchestrator)."""
    r = client.post("/ask", json={"question": "Gmail 동기화해줘"})

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] != "agent_work"
    assert body.get("agent_work") is None
    with session() as s:
        rows = s.execute(select(agent_work_items.c.work_id)).all()
    assert rows == []


def test_orchestrate_missing_session_returns_404(user_id):
    r = _orchestrate("Gmail 동기화해줘", str(uuid.uuid4()))
    assert r.status_code == 404


def test_orchestrate_plain_question_answers_as_search(user_id):
    sid = _new_chat()
    r = _orchestrate("연차 정책은 어떻게 동작하나요?", sid)

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] in {"wiki", "structured", "graph"}
    assert body.get("agent_work") is None


def test_orchestrate_command_stores_valid_context_wiki_slug(user_id):
    sid = _new_chat()
    r = _orchestrate("Gmail 동기화해줘", sid, context_wiki_slug="회사-미팅")

    assert r.status_code == 200
    work_id = uuid.UUID(r.json()["agent_work"]["work_id"])
    with session() as s:
        payload = s.execute(
            select(agent_work_items.c.payload).where(agent_work_items.c.work_id == work_id)
        ).scalar_one()
    assert payload["context"]["wiki_slug"] == "회사-미팅"


def test_orchestrate_command_drops_invalid_context_wiki_slug(user_id):
    sid = _new_chat()
    r = _orchestrate("Gmail 동기화해줘", sid, context_wiki_slug="../bad slug")

    assert r.status_code == 200
    work_id = uuid.UUID(r.json()["agent_work"]["work_id"])
    with session() as s:
        payload = s.execute(
            select(agent_work_items.c.payload).where(agent_work_items.c.work_id == work_id)
        ).scalar_one()
    assert "context" not in payload


def test_ask_context_wiki_slug_does_not_leak_to_structured_query_run(user_id, monkeypatch):
    _seed_ticket(user_id)
    monkeypatch.setattr("orthus.router.classify", lambda *a, **k: "structured")

    r = client.post(
        "/ask",
        json={"question": "티켓 개수", "context_wiki_slug": "회사-미팅"},
    )

    assert r.status_code == 200
    query_id = uuid.UUID(r.json()["structured"]["query_id"])
    with session() as s:
        row = s.execute(
            select(query_runs.c.nl_question, query_runs.c.result_meta).where(
                query_runs.c.query_id == query_id
            )
        ).one()
        work_rows = s.execute(select(agent_work_items.c.work_id)).all()
    assert row.nl_question == "티켓 개수"
    assert "회사-미팅" not in str(row.result_meta)
    assert work_rows == []


def test_wiki_page_read_endpoint_uses_node_scope(user_id):
    settings = get_settings()
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

    r = client.get("/wiki/pages/company-policy")
    assert r.status_code == 200
    assert r.json()["title"] == "Company Policy"

    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    store.write_page(
        WikiPage(
            slug="personal-note",
            title="Personal Note",
            definition="Personal definition",
            overview="Personal overview",
        ),
        user_id=user_id,
        scope="personal",
        owner_id=user_id,
        project="company",
    )
    other_owner = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_owner, display_name="Other User"))
        s.commit()
    store.write_page(
        WikiPage(
            slug="other-personal-note",
            title="Other Personal Note",
            definition="Other definition",
            overview="Other overview",
        ),
        user_id=other_owner,
        scope="personal",
        owner_id=other_owner,
        project="company",
    )

    headers = {"X-User-Id": str(user_id)}
    own = client.get("/wiki/pages/personal-note", headers=headers)
    company_fallback = client.get("/wiki/pages/company-policy", headers=headers)
    other = client.get("/wiki/pages/other-personal-note", headers=headers)

    assert own.status_code == 200
    assert own.json()["title"] == "Personal Note"
    assert company_fallback.status_code == 200
    assert company_fallback.json()["title"] == "Company Policy"
    assert other.status_code == 404


def test_personal_wiki_page_read_falls_back_to_central(user_id, monkeypatch):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"

    monkeypatch.setattr(
        "orthus.api.routes.wiki.central_client.get_company_wiki_page",
        lambda slug: WikiPage(
            slug=slug,
            title="Central Page",
            definition="Central definition",
            overview="Central overview",
        ),
    )

    r = client.get("/wiki/pages/company-policy")

    assert r.status_code == 200
    assert r.json()["title"] == "Central Page"


def test_ask_configured_connector_command_auto_executes_sync(user_id):
    slug = f"agent_sync_{uuid.uuid4().hex[:8]}"
    manifest = ConnectorManifest(
        slug=slug,
        label="Agent Sync",
        source_kind="local_app",
        account_kinds=("company",),
        auth_modes=("local_path",),
        capabilities=("read", "manual"),
    )

    def _factory(account: Mapping[str, object], state) -> Connector:
        del state
        return _AgentWorkMemConnector(str(account["connector_slug"]))

    register_connector_provider(FactoryConnectorProvider(manifest, _factory))
    account_id = create_connector_account(
        ConnectorAccountSpec(
            connector_slug=slug,
            account_kind="company",
            node_id="company",
            auth_mode="local_path",
            account_label="Agent Sync",
        )
    )

    sid = _new_chat()
    r = _orchestrate(f"{slug} connector sync 해줘", sid)

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "agent_work"
    work = body["agent_work"]
    assert work["source_kind"] == "assistant_command"
    assert work["action_family"] == "connector_sync"
    assert work["state"] == "resolved"
    assert work["policy_outcome"] == "auto_execute"
    assert "connector_configured" in work["reason_codes"]
    assert body["agent_work"]["message"] == "Executed by Agent Work policy gate."

    with session() as s:
        item = s.execute(
            select(agent_work_items).where(agent_work_items.c.work_id == uuid.UUID(work["work_id"]))
        ).one()
        run = s.execute(select(connector_runs)).one()
        doc = s.execute(
            select(documents.c.source, documents.c.source_account_id, documents.c.title)
        ).one()
        audit_nodes = [
            row.node
            for row in s.execute(
                select(audit_log.c.node).where(
                    audit_log.c.node.in_(["agent_work.auto_execute", "connector.command"])
                )
            ).all()
        ]

    payload = item._mapping["payload"]
    assert payload["connector_slug"] == slug
    assert payload["account_id"] == str(account_id)
    assert payload["auto_execution"]["kind"] == "connector_sync"
    assert payload["auto_execution"]["run_id"] == str(run.run_id)
    assert payload["auto_execution"]["status"] == "succeeded"
    assert run.account_id == account_id
    assert run.connector_slug == slug
    assert run.reason == "manual"
    assert run.status == "succeeded"
    assert doc.source == slug
    assert doc.source_account_id == account_id
    assert doc.title == "Agent Work connector item"
    assert "agent_work.auto_execute" in audit_nodes
    assert "connector.command" in audit_nodes

    duplicate = _orchestrate(f"{slug} connector sync 해줘", sid)
    assert duplicate.status_code == 200
    assert duplicate.json()["agent_work"]["work_id"] == work["work_id"]
    with session() as s:
        run_count = s.execute(select(connector_runs.c.run_id)).all()
        repeated_payload = s.execute(
            select(agent_work_items.c.payload).where(
                agent_work_items.c.work_id == uuid.UUID(work["work_id"])
            )
        ).scalar_one()
    assert len(run_count) == 1
    assert repeated_payload["auto_execution"]["run_id"] == str(run.run_id)


def test_ask_personal_board_cleanup_auto_archives_done_tasks(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    wiki_docs: list[InternalDocument] = []

    def fake_upsert(_user_id: uuid.UUID, doc: InternalDocument, *, scope: str, **_kwargs):
        assert scope == "personal"
        wiki_docs.append(doc)
        return uuid.uuid4(), True

    monkeypatch.setattr("orthus.personal_board.upsert_source_document", fake_upsert)
    headers = {"X-User-Id": str(user_id)}
    done = client.post(
        "/personal-board/tasks",
        json={"title": "완료된 정리 대상", "scheduled_date": "2026-06-06"},
        headers=headers,
    )
    assert done.status_code == 201
    done_task_id = done.json()["task_id"]
    open_task = client.post(
        "/personal-board/tasks",
        json={"title": "아직 진행 중", "scheduled_date": "2026-06-06"},
        headers=headers,
    )
    assert open_task.status_code == 201
    open_task_id = open_task.json()["task_id"]
    completed = client.patch(
        f"/personal-board/tasks/{done_task_id}",
        json={"status": "done"},
        headers=headers,
    )
    assert completed.status_code == 200

    sid = _new_chat(headers=headers)
    r = _orchestrate("개인 보드 정리해줘", sid, headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "agent_work"
    work = body["agent_work"]
    assert work["action_family"] == "personal_board_cleanup"
    assert work["state"] == "resolved"
    assert work["policy_outcome"] == "auto_execute"
    assert "reversible" in work["reason_codes"]
    assert body["agent_work"]["message"] == "Executed by Agent Work policy gate."

    with session() as s:
        item = s.execute(
            select(agent_work_items).where(agent_work_items.c.work_id == uuid.UUID(work["work_id"]))
        ).one()
        statuses = {
            str(row.task_id): row.status
            for row in s.execute(
                select(personal_board_tasks.c.task_id, personal_board_tasks.c.status).where(
                    personal_board_tasks.c.task_id.in_(
                        [uuid.UUID(done_task_id), uuid.UUID(open_task_id)]
                    )
                )
            ).all()
        }
        audit_nodes = [
            row.node
            for row in s.execute(
                select(audit_log.c.node).where(
                    audit_log.c.node.in_(["agent_work.auto_execute", "personal_board.cleanup"])
                )
            ).all()
        ]

    payload = item._mapping["payload"]
    assert payload["cleanup_kind"] == "archive_done_tasks"
    assert payload["auto_execution"]["kind"] == "personal_board_cleanup"
    assert payload["auto_execution"]["archived_count"] == 1
    assert payload["auto_execution"]["archived_tasks"][0]["task_id"] == done_task_id
    assert statuses[done_task_id] == "archived"
    assert statuses[open_task_id] == "open"
    assert "agent_work.auto_execute" in audit_nodes
    assert "personal_board.cleanup" in audit_nodes
    assert wiki_docs

    duplicate = _orchestrate("개인 보드 정리해줘", sid, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["agent_work"]["work_id"] == work["work_id"]
    with session() as s:
        repeated_payload = s.execute(
            select(agent_work_items.c.payload).where(
                agent_work_items.c.work_id == uuid.UUID(work["work_id"])
            )
        ).scalar_one()
    assert repeated_payload["auto_execution"]["archived_count"] == 1


def test_orchestrate_document_draft_command_creates_editor_draft_without_indexing(user_id):
    sid = _new_chat()
    r = _orchestrate("문서 초안 만들어줘", sid)

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "agent_work"
    work = body["agent_work"]
    duplicate = _orchestrate("문서 초안 만들어줘", sid)
    assert duplicate.status_code == 200
    assert duplicate.json()["agent_work"]["work_id"] == work["work_id"]
    assert work["action_family"] == "document_draft"
    assert work["state"] == "draft_for_review"

    with session() as s:
        item = s.execute(
            select(agent_work_items).where(agent_work_items.c.work_id == uuid.UUID(work["work_id"]))
        ).first()
        assert item is not None
        draft_doc_id = uuid.UUID(item._mapping["payload"]["draft_document"]["doc_id"])
        draft = s.execute(
            select(
                documents.c.title,
                documents.c.block_json,
                documents.c.markdown,
                documents.c.source,
                documents.c.scope,
                documents.c.project,
            ).where(documents.c.doc_id == draft_doc_id)
        ).first()
        indexed_chunks = s.execute(
            select(corpus_chunks.c.chunk_id).where(corpus_chunks.c.doc_id == draft_doc_id)
        ).all()
        draft_docs = s.execute(
            select(documents.c.doc_id).where(documents.c.source == "agent_draft")
        ).all()

    assert draft is not None
    assert draft.source == "agent_draft"
    assert draft.scope == "company"
    assert draft.project == "company"
    assert draft.block_json[0]["content"][0]["text"].startswith("Draft:")
    assert draft.block_json[-1]["content"][0]["text"] == "문서 초안 만들어줘"
    assert "Original request" in draft.markdown
    assert indexed_chunks == []
    assert [row.doc_id for row in draft_docs] == [draft_doc_id]


def test_agent_draft_save_does_not_index_until_publish(user_id):
    r = _orchestrate("문서 초안 만들어줘", _new_chat())
    assert r.status_code == 200
    work = r.json()["agent_work"]

    with session() as s:
        item = s.execute(
            select(agent_work_items.c.payload).where(
                agent_work_items.c.work_id == uuid.UUID(work["work_id"])
            )
        ).first()
    assert item is not None
    draft_doc_id = item.payload["draft_document"]["doc_id"]

    draft_body = {
        "title": "검토 중인 초안",
        "block_json": [{"type": "paragraph", "content": [{"type": "text", "text": "수정본"}]}],
        "markdown": "수정본입니다. 아직 central wiki에는 반영하지 않습니다.",
    }
    saved = client.put(f"/documents/{draft_doc_id}", json=draft_body)
    assert saved.status_code == 200

    got = client.get(f"/documents/{draft_doc_id}")
    assert got.status_code == 200
    assert got.json()["title"] == "검토 중인 초안"
    assert got.json()["source"] == "agent_draft"

    with session() as s:
        draft = s.execute(
            select(documents.c.source, documents.c.markdown).where(
                documents.c.doc_id == uuid.UUID(draft_doc_id)
            )
        ).first()
        indexed_chunks = s.execute(
            select(corpus_chunks.c.chunk_id).where(
                corpus_chunks.c.doc_id == uuid.UUID(draft_doc_id)
            )
        ).all()
    assert draft is not None
    assert draft.source == "agent_draft"
    assert draft.markdown == draft_body["markdown"]
    assert indexed_chunks == []

    publish_body = {
        "title": "검토 완료 초안",
        "block_json": [{"type": "paragraph", "content": [{"type": "text", "text": "최종본"}]}],
        "markdown": "최종본입니다. 이제 central wiki에 반영할 수 있습니다.",
    }
    published = client.post(f"/documents/{draft_doc_id}/publish", json=publish_body)
    assert published.status_code == 200

    got = client.get(f"/documents/{draft_doc_id}")
    assert got.status_code == 200
    assert got.json()["title"] == "검토 완료 초안"
    assert got.json()["source"] == "editor"

    with session() as s:
        published_doc = s.execute(
            select(documents.c.source, documents.c.markdown).where(
                documents.c.doc_id == uuid.UUID(draft_doc_id)
            )
        ).first()
        indexed_chunks = s.execute(
            select(corpus_chunks.c.chunk_id).where(
                corpus_chunks.c.doc_id == uuid.UUID(draft_doc_id)
            )
        ).all()
        work_state = s.execute(
            select(agent_work_items.c.state).where(
                agent_work_items.c.work_id == uuid.UUID(work["work_id"])
            )
        ).scalar_one()
        publish_audit = s.execute(
            select(audit_log.c.meta).where(
                audit_log.c.node == "document.publish",
                audit_log.c.phase == "exit",
            )
        ).first()
    assert published_doc is not None
    assert published_doc.source == "editor"
    assert published_doc.markdown == publish_body["markdown"]
    assert indexed_chunks
    assert work_state == "resolved"
    assert publish_audit is not None
    assert publish_audit.meta["agent_work_items_resolved"] == 1


def test_publish_rejects_non_draft_document(user_id):
    created = client.post(
        "/documents",
        json={
            "title": "일반 문서",
            "block_json": [{"type": "paragraph"}],
            "markdown": "일반 editor 문서입니다.",
        },
    )
    assert created.status_code == 201
    doc_id = created.json()["doc_id"]

    r = client.post(
        f"/documents/{doc_id}/publish",
        json={
            "title": "일반 문서",
            "block_json": [{"type": "paragraph"}],
            "markdown": "일반 editor 문서입니다.",
        },
    )
    assert r.status_code == 409


def test_publish_rejects_cross_user_agent_draft(user_id):
    r = _orchestrate("문서 초안 만들어줘", _new_chat())
    assert r.status_code == 200
    work = r.json()["agent_work"]

    with session() as s:
        item = s.execute(
            select(agent_work_items.c.payload).where(
                agent_work_items.c.work_id == uuid.UUID(work["work_id"])
            )
        ).first()
        other_user = uuid.uuid4()
        s.execute(insert(users).values(user_id=other_user, display_name="Other User"))
        s.commit()
    assert item is not None
    draft_doc_id = item.payload["draft_document"]["doc_id"]

    r = client.post(
        f"/documents/{draft_doc_id}/publish",
        headers={"X-User-Id": str(other_user)},
        json={
            "title": "다른 사용자 publish",
            "block_json": [{"type": "paragraph"}],
            "markdown": "다른 사용자는 publish할 수 없습니다.",
        },
    )
    assert r.status_code == 404

    with session() as s:
        source = s.execute(
            select(documents.c.source).where(documents.c.doc_id == uuid.UUID(draft_doc_id))
        ).scalar_one()
    assert source == "agent_draft"


def test_ask_command_shaped_knowledge_question_falls_back_to_wiki(user_id):
    r = client.post("/ask", json={"question": "문서 작성 가이드 알려줘"})

    assert r.status_code == 200
    assert r.json()["mode"] == "wiki"
    with session() as s:
        count = s.execute(select(agent_work_items.c.work_id)).all()
    assert count == []


def test_ask_command_member_falls_back_without_queue_in_session_mode(clean, user_id):
    def member_user():
        return AuthenticatedUser(
            user_id=user_id,
            auth_mode="session",
            display_name="Member",
            email="member@example.com",
            role="member",
            node_id="company",
        )

    app.dependency_overrides[get_session_user_or_knowledge_token] = member_user
    try:
        r = client.post("/ask", json={"question": "Gmail 동기화해줘"})
    finally:
        app.dependency_overrides.pop(get_session_user_or_knowledge_token, None)

    assert r.status_code == 200
    assert r.json()["mode"] == "wiki"
    with session() as s:
        count = s.execute(select(agent_work_items.c.work_id)).all()
    assert count == []


def test_company_ask_clamps_scope_to_company(user_id, monkeypatch):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    captured = {}

    def _answer(uid, question, *, scope, project, context_wiki_slug=None, history=None, **_kwargs):
        captured["user_id"] = uid
        captured["question"] = question
        captured["scope"] = scope
        captured["project"] = project
        captured["context_wiki_slug"] = context_wiki_slug
        return RoutedAnswer(question=question, mode="wiki")

    monkeypatch.setattr(routes.ask, "answer", _answer)

    r = client.post("/ask", json={"question": "범위 확인", "scope": "all"})

    assert r.status_code == 200
    assert captured["user_id"] == user_id
    assert captured["scope"] == "company"
    assert captured["context_wiki_slug"] is None


def test_ask_structured_reject_returns_422(user_id, monkeypatch):
    def _rejected(*a, **k):
        return RoutedAnswer(
            question="drop everything",
            mode="structured",
            structured=AssistantResult(
                query_id=uuid.uuid4(),
                question="drop everything",
                source_id=None,
                compiled=CompiledQuery(
                    question="x", source_id=None, dialect="postgres", sql="DELETE FROM notion_rows"
                ),
                validation=ValidationResult(
                    parsed=True, statement_kind="other", rejected_reason="non_select_statement"
                ),
                status="rejected",
                message="only SELECT is allowed",
            ),
        )

    monkeypatch.setattr(routes.ask, "answer", _rejected)
    r = client.post("/ask", json={"question": "drop"})
    assert r.status_code == 422
    assert r.json()["structured"]["status"] == "rejected"
    assert r.json()["structured"]["validation"]["rejected_reason"] == "non_select_statement"


def test_old_assistant_query_route_is_gone(user_id):
    # POST /assistant/query was superseded by /ask in P2.2b.
    assert client.post("/assistant/query", json={"question": "ping"}).status_code == 404
