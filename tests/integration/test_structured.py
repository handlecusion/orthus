"""P2.2a acceptance: structured(PG) backend — NL→SQL over the JSONB row store
`notion_rows` (docs/architecture-v2.md §1/§5).

Covers the happy path (a grounded JSONB aggregate executes and returns rows) and
scope isolation (user A's personal rows are not visible to user B via the
scope-filtered catalog). The 5-reject regression lives in test_assistant.py.

Postgres must be up on localhost:5433 (run `make up && make migrate`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import insert, select

from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import InternalDocument
from orthus.structured.query import build_notion_catalog, query_structured
from orthus.tables import notion_rows, structured_rows, users


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def _seed_row(user_id, *, db_name, properties, scope, owner_id=None) -> None:
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=uuid.uuid4(),
                db_id=f"db-{db_name}",
                db_name=db_name,
                properties=properties,
                scope=scope,
                owner_id=owner_id,
                user_id=user_id,
            )
        )
        s.commit()


def test_happy_status_counts(user_id):
    """'티켓 상태별 개수' → grounded JSONB aggregate → executes, returns rows."""
    for status in ["진행중", "진행중", "완료"]:
        _seed_row(
            user_id, db_name="티켓", properties={"상태": status, "담당자": "A"}, scope="company"
        )

    sql = (
        "SELECT properties->>'상태' AS status, count(*) AS n "
        "FROM notion_rows WHERE db_name = '티켓' GROUP BY 1"
    )
    chat = MockChat(rules=[("상태별", '{"sql": "%s"}' % sql)])
    result = query_structured(user_id, "티켓 상태별 개수", chat_model=chat)

    assert result.status == "executed"
    assert result.row_count == 2  # 진행중, 완료 buckets
    counts = {row[0]: row[1] for row in result.rows}
    assert counts == {"진행중": 2, "완료": 1}


def test_catalog_lists_db_and_property_keys(user_id):
    """build_notion_catalog surfaces db_name + property keys in the description so
    the model knows what to filter/aggregate on."""
    _seed_row(
        user_id, db_name="티켓", properties={"상태": "진행중", "담당자": "A"}, scope="company"
    )
    catalog = build_notion_catalog(user_id, scope="all")
    desc = catalog["tables"]["notion_rows"]["description"]
    assert "티켓" in desc
    assert "상태" in desc and "담당자" in desc
    # the fixed logical columns are always present for the gate's schema_ok.
    cols = catalog["tables"]["notion_rows"]["columns"]
    assert cols["properties"] == "jsonb"
    assert "db_name" in cols


def test_catalog_disambiguates_internal_team_members(user_id):
    _seed_row(
        user_id,
        db_name="팀원",
        properties={"이름": "박기획", "역할": "PM", "이메일": "p@example.com", "활성": "✓"},
        scope="company",
    )
    _seed_row(
        user_id,
        db_name="직원 ",
        properties={"이름": "협력사직원", "직책": "대표", "전화번호": "010-0000-0000"},
        scope="company",
    )

    catalog = build_notion_catalog(user_id, scope="company")
    desc = catalog["tables"]["notion_rows"]["description"]

    assert "`db_name = '팀원'` means internal Acme/company team members" in desc
    assert "Current internal team member names: 박기획" in desc
    assert "`db_name = '직원 '` mean partner/vendor staff contacts" in desc
    assert "use `structured_rows` contact records as a secondary source" in desc
    assert "do not switch to partner/vendor staff databases" in desc
    assert "prefer `팀원.이메일` over Slack contact rows" in desc


def test_catalog_guides_slack_contacts_as_secondary_non_redacted_source(user_id):
    _seed_row(
        user_id,
        db_name="팀원",
        properties={"이름": "박기획", "역할": "PM", "이메일": "p@example.com", "활성": "✓"},
        scope="company",
    )
    with session() as s:
        s.execute(
            insert(structured_rows).values(
                row_id=uuid.uuid4(),
                source="slack",
                record_type="contact",
                record_key="contact-1",
                properties={
                    "name": "박기획",
                    "email": "p***@example.com",
                    "phone": "010-1234-5678",
                    "is_redacted": True,
                },
                evidence="박기획 p***@example.com 010-1234-5678",
                scope="company",
                project="company",
                user_id=user_id,
            )
        )
        s.commit()

    catalog = build_notion_catalog(user_id, scope="company")
    structured_desc = catalog["tables"]["structured_rows"]["description"]

    assert "use `notion_rows` with `db_name = '팀원'` first" in structured_desc
    assert "when a requested contact field is absent from `팀원`" in structured_desc
    assert "COALESCE(properties->>'is_redacted', 'false') <> 'true'" in structured_desc


def test_personal_rows_isolated_to_owner(user_id):
    """A's personal rows are queryable by A but invisible to B (scope filter)."""
    user_b = _make_user()
    _seed_row(
        user_id,
        db_name="비밀",
        properties={"코드네임": "오로라"},
        scope="personal",
        owner_id=user_id,
    )

    # A sees the db in their catalog.
    cat_a = build_notion_catalog(user_id, scope="all")
    assert "비밀" in cat_a["tables"]["notion_rows"]["description"]

    # B does NOT — the personal row belongs to A.
    cat_b = build_notion_catalog(user_b, scope="all")
    assert "비밀" not in cat_b["tables"]["notion_rows"]["description"]

    # Isolation is enforced at the catalog/grounding layer: the compiler only sees
    # databases in the caller's scoped catalog, so B is never grounded to query A's
    # '비밀' db. A, scoped to personal, can count their own rows.
    sql = "SELECT count(*) AS n FROM notion_rows WHERE db_name = '비밀'"
    chat = MockChat(rules=[("비밀", '{"sql": "%s"}' % sql)])
    res_a = query_structured(user_id, "비밀 개수", scope="personal", chat_model=chat)
    assert res_a.status == "executed"
    assert res_a.rows[0][0] == 1


def test_nl_question_stored_redacted(user_id):
    """S2 regression: email in NL question is redacted in query_runs.nl_question."""
    from sqlalchemy import select
    from orthus.tables import query_runs

    sql = "SELECT count(*) AS n FROM notion_rows"
    chat = MockChat(rules=[("email", '{"sql": "%s"}' % sql)])
    result = query_structured(
        user_id,
        "how many rows for alice@internal.co",
        chat_model=chat,
    )
    # The query may be rejected (no rows seeded) or executed — either way the run
    # is recorded and nl_question must not contain the raw email.
    with session() as s:
        stored = s.execute(
            select(query_runs.c.nl_question).where(query_runs.c.query_id == result.query_id)
        ).scalar_one()
    assert "alice@internal.co" not in stored
    assert "@internal.co" in stored  # domain kept; local-part masked


def test_structured_response_rows_keep_contact_values(user_id):
    _seed_row(
        user_id,
        db_name="팀원",
        properties={
            "이름": "박기획",
            "이메일": "member1@example.com",
            "전화번호": "010-1234-5678",
        },
        scope="company",
    )
    sql = (
        "SELECT properties->>'이메일' AS email, properties->>'전화번호' AS phone "
        "FROM notion_rows WHERE db_name = '팀원' AND properties->>'이름' = '박기획'"
    )
    chat = MockChat(rules=[("박기획", '{"sql": "%s"}' % sql)])

    result = query_structured(user_id, "박기획 이메일 전화번호", chat_model=chat)

    assert result.status == "executed"
    assert result.rows == [["member1@example.com", "010-1234-5678"]]


def test_catalog_includes_generic_structured_rows(user_id):
    with session() as s:
        s.execute(
            insert(structured_rows).values(
                row_id=uuid.uuid4(),
                source="slack",
                record_type="contact",
                record_key="contact-1",
                properties={"name": "박기획", "phone": "010-1234-5678"},
                evidence="박기획 010-1234-5678",
                scope="company",
                project="company",
                user_id=user_id,
            )
        )
        s.commit()

    catalog = build_notion_catalog(user_id, scope="company")
    assert "structured_rows" in catalog["tables"]
    desc = catalog["tables"]["structured_rows"]["description"]
    assert "contact" in desc
    assert "phone" in desc


def test_structured_query_can_read_slack_structured_rows(user_id):
    with session() as s:
        s.execute(
            insert(structured_rows).values(
                row_id=uuid.uuid4(),
                source="slack",
                record_type="contact",
                record_key="contact-1",
                properties={"name": "박기획", "phone": "010-1234-5678"},
                evidence="박기획 010-1234-5678",
                scope="company",
                project="company",
                user_id=user_id,
            )
        )
        s.commit()

    sql = (
        "SELECT properties->>'name' AS name, properties->>'phone' AS phone "
        "FROM structured_rows "
        "WHERE source = 'slack' AND record_type = 'contact' "
        "AND properties->>'name' = '박기획'"
    )
    result = query_structured(
        user_id, "Slack 박기획 전화번호", chat_model=MockChat(default='{"sql": "%s"}' % sql)
    )

    assert result.status == "executed"
    assert result.rows == [["박기획", "010-1234-5678"]]
    assert "FROM (SELECT * FROM structured_rows WHERE" in result.compiled.sql


def test_source_document_structured_rows_are_replaced(user_id):
    first = InternalDocument(
        title="Slack thread",
        markdown="first",
        source="slack",
        source_external_id="slack:C123:1.0",
        source_last_edited_at=datetime(2026, 1, 1, tzinfo=UTC),
        structured_rows=[
            {
                "source": "slack",
                "record_type": "contact",
                "record_key": "m1:email:old@example.com",
                "properties": {"name": "Old", "email": "old@example.com"},
            }
        ],
    )
    doc_id, changed = upsert_source_document(user_id, first, defer_authoring=True)
    assert changed is True

    second = first.model_copy(
        update={
            "markdown": "second",
            "source_last_edited_at": datetime(2026, 1, 2, tzinfo=UTC),
            "structured_rows": [
                {
                    "source": "slack",
                    "record_type": "link",
                    "record_key": "m2:link:1",
                    "properties": {"url": "https://example.com"},
                }
            ],
        }
    )
    same_doc_id, changed = upsert_source_document(user_id, second, defer_authoring=True)
    assert same_doc_id == doc_id
    assert changed is True

    with session() as s:
        rows = s.execute(
            select(structured_rows.c.record_type, structured_rows.c.properties).where(
                structured_rows.c.source_doc_id == doc_id
            )
        ).all()

    assert rows == [("link", {"url": "https://example.com"})]
