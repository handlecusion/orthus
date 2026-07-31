"""전용 코퍼스 브라우저 (`/corpus`) 통합 테스트.

company-scope 가시성(소유자 무관), source/q 필터 + counts_by_source, owner_name
join, non-company 404, agent_draft 인라인 저장 거부, owner 보존 인라인 저장을
검증한다."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.db import session
from orthus.documents import create_agent_draft_document, save_editor_document
from orthus.tables import documents, users

client = TestClient(app)


def _other_user(display_name: str = "Other Owner") -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name=display_name))
        s.commit()
    return uid


def test_corpus_lists_all_company_docs_regardless_of_owner(user_id):
    # Two users exist, so demo auth needs an explicit caller header.
    headers = {"X-User-Id": str(user_id)}
    other = _other_user()
    mine = save_editor_document(user_id, "내 회사 문서", [{"type": "paragraph"}], "내 본문")
    theirs = save_editor_document(other, "남의 회사 문서", [{"type": "paragraph"}], "남의 본문")

    r = client.get("/corpus/documents", headers=headers)
    assert r.status_code == 200
    body = r.json()
    doc_ids = {item["doc_id"] for item in body["items"]}
    assert str(mine) in doc_ids
    assert str(theirs) in doc_ids

    # owner_name comes from the documents.user_id join, not the caller.
    theirs_item = next(i for i in body["items"] if i["doc_id"] == str(theirs))
    assert theirs_item["owner_name"] == "Other Owner"
    assert theirs_item["preview"] == "남의 본문"


def test_corpus_excludes_personal_scope_docs(user_id):
    company = save_editor_document(user_id, "회사", [{"type": "paragraph"}], "회사 본문")
    personal = save_editor_document(
        user_id, "개인", [{"type": "paragraph"}], "개인 본문", scope="personal"
    )

    doc_ids = {item["doc_id"] for item in client.get("/corpus/documents").json()["items"]}
    assert str(company) in doc_ids
    assert str(personal) not in doc_ids


def test_corpus_source_and_query_filters_and_counts(user_id):
    # editor-sourced doc + a connector-sourced (slack) company doc.
    save_editor_document(user_id, "에디터 알파", [{"type": "paragraph"}], "alpha body")
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=uuid.uuid4(),
                user_id=user_id,
                title="슬랙 베타",
                block_json=[],
                markdown="beta body",
                source="slack",
                schema_version=1,
                scope="company",
                project="company",
            )
        )
        s.commit()

    # counts_by_source ignores the source filter but honors q.
    both = client.get("/corpus/documents").json()
    assert both["counts_by_source"].get("editor") == 1
    assert both["counts_by_source"].get("slack") == 1
    assert both["total"] == 2

    only_slack = client.get("/corpus/documents", params={"source": "slack"}).json()
    assert only_slack["total"] == 1
    assert {i["source"] for i in only_slack["items"]} == {"slack"}
    # chips still show every source for the active search.
    assert only_slack["counts_by_source"].get("editor") == 1

    # q matches title OR markdown.
    only_alpha = client.get("/corpus/documents", params={"q": "alpha"}).json()
    assert only_alpha["total"] == 1
    assert only_alpha["items"][0]["title"] == "에디터 알파"
    assert only_alpha["counts_by_source"] == {"editor": 1}


def test_corpus_sort_title_and_pagination(user_id):
    save_editor_document(user_id, "B 문서", [{"type": "paragraph"}], "b")
    save_editor_document(user_id, "A 문서", [{"type": "paragraph"}], "a")

    by_title = client.get("/corpus/documents", params={"sort": "title"}).json()
    titles = [i["title"] for i in by_title["items"]]
    assert titles == sorted(titles)

    page = client.get("/corpus/documents", params={"limit": 1, "offset": 0}).json()
    assert len(page["items"]) == 1
    assert page["total"] == 2


def test_corpus_get_document_company_only(user_id):
    company = save_editor_document(user_id, "회사", [{"type": "paragraph"}], "회사 본문")
    personal = save_editor_document(
        user_id, "개인", [{"type": "paragraph"}], "개인 본문", scope="personal"
    )

    ok = client.get(f"/corpus/documents/{company}")
    assert ok.status_code == 200
    assert ok.json()["title"] == "회사"
    assert ok.json()["owner_name"] == "Demo User"

    assert client.get(f"/corpus/documents/{personal}").status_code == 404
    assert client.get(f"/corpus/documents/{uuid.uuid4()}").status_code == 404


def test_corpus_inline_save_preserves_owner_and_reindexes(user_id):
    # Two users exist, so demo auth needs an explicit caller header.
    headers = {"X-User-Id": str(user_id)}
    other = _other_user()
    doc_id = save_editor_document(other, "원본", [{"type": "paragraph"}], "원본 본문")

    # a different authenticated user edits the shared company doc.
    r = client.put(
        f"/corpus/documents/{doc_id}",
        json={
            "title": "수정본",
            "block_json": [{"type": "paragraph"}],
            "markdown": "연차 휴가는 15일이다.",
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["doc_id"] == str(doc_id)

    # owner/source are preserved on the row; only content changed.
    with session() as s:
        row = s.execute(
            select(documents.c.user_id, documents.c.source, documents.c.title).where(
                documents.c.doc_id == doc_id
            )
        ).first()
    assert row.user_id == other
    assert row.source == "editor"
    assert row.title == "수정본"

    # save reindexed into corpus + wiki: searchable through the wiki.
    ask = client.post("/wiki/ask", json={"question": "연차 며칠?", "k": 3}, headers=headers)
    assert ask.status_code == 200
    assert ask.json()["sources"]


def test_corpus_rejects_agent_draft_inline_save(user_id):
    draft_id = create_agent_draft_document(user_id, "초안", [{"type": "paragraph"}], "초안 본문")

    r = client.put(
        f"/corpus/documents/{draft_id}",
        json={"title": "x", "block_json": [], "markdown": "y"},
    )
    assert r.status_code == 400
