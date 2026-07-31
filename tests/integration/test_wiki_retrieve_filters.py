from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.main import app
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import SCHEMA_VERSION, WikiClaim, WikiPage, WikiSource
from orthus.tables import documents
from orthus.wiki import store
from orthus.wiki.qa import ask
from orthus.wiki.retrieve import retrieve
from orthus.wiki.slugs import source_slug_from_title

client = TestClient(app)


def test_retrieve_filters_by_source_and_date(user_id: UUID) -> None:
    old_doc = _write_doc_and_wiki(
        user_id,
        title="Old Notion Filter Source",
        source="notion",
        edited_at=datetime(2026, 5, 1, tzinfo=UTC),
        page_slug="old-filter-policy",
        text="Filter policy alpha old notion archive.",
    )
    new_doc = _write_doc_and_wiki(
        user_id,
        title="New Github Filter Source",
        source="github",
        edited_at=datetime(2026, 6, 2, tzinfo=UTC),
        page_slug="new-filter-policy",
        text="Filter policy alpha new github release.",
    )

    github_hits = retrieve(user_id, "filter policy alpha", k=5, source="github")
    assert github_hits
    assert {h.page_slug for h in github_hits} <= {
        "new-filter-policy",
        "new-filter-policy-claim",
    }
    assert all(
        source_slug_from_title("New Github Filter Source", new_doc) in h.provenance
        for h in github_hits
    )

    recent_hits = retrieve(
        user_id,
        "filter policy alpha",
        k=5,
        since=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert recent_hits
    assert {h.page_slug for h in recent_hits} <= {
        "new-filter-policy",
        "new-filter-policy-claim",
    }

    old_hits = retrieve(
        user_id,
        "filter policy alpha",
        k=5,
        until=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert old_hits
    assert {h.page_slug for h in old_hits} <= {"old-filter-policy", "old-filter-policy-claim"}

    missing = retrieve(user_id, "filter policy alpha", k=5, source="gws_drive")
    assert missing == []

    malicious_source = retrieve(user_id, "filter policy alpha", k=5, source="github' OR 1=1 --")
    assert malicious_source == []

    assert old_doc != new_doc


def test_wiki_ask_accepts_source_and_date_filters(user_id: UUID) -> None:
    _write_doc_and_wiki(
        user_id,
        title="Route Old Notion Filter Source",
        source="notion",
        edited_at=datetime(2026, 5, 1, tzinfo=UTC),
        page_slug="route-old-filter-policy",
        text="Route filter alpha old notion archive.",
    )
    _write_doc_and_wiki(
        user_id,
        title="Route New Github Filter Source",
        source="github",
        edited_at=datetime(2026, 6, 2, tzinfo=UTC),
        page_slug="route-new-filter-policy",
        text="Route filter alpha new github release.",
    )

    res = client.post(
        "/wiki/ask",
        json={
            "question": "route filter alpha",
            "k": 5,
            "source": "github",
            "since": "2026-06-01",
            "until": "2026-06-02",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["sources"]
    assert {s["page_slug"] for s in body["sources"]} <= {
        "route-new-filter-policy",
        "route-new-filter-policy-claim",
    }

    answer = ask(
        user_id,
        "route filter alpha",
        k=5,
        source="notion",
        since=datetime(2026, 6, 1, tzinfo=UTC),
        chat_model=MockChat(default="not used"),
        learn=False,
    )
    assert answer.sources == []


def _write_doc_and_wiki(
    user_id: UUID,
    *,
    title: str,
    source: str,
    edited_at: datetime,
    page_slug: str,
    text: str,
) -> UUID:
    doc_id = uuid.uuid4()
    source_slug = source_slug_from_title(title, doc_id)
    claim_slug = f"{page_slug}-claim"
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title=title,
                block_json=[],
                markdown=text,
                source=source,
                source_external_id=str(doc_id),
                source_last_edited_at=edited_at,
                schema_version=SCHEMA_VERSION,
                scope="company",
                project="company",
            )
        )
        s.commit()

    store.write_source(
        WikiSource(
            slug=source_slug,
            title=title,
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=edited_at,
            summary=text,
            key_claims=[claim_slug],
            related_pages=[page_slug],
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    store.write_claim(
        WikiClaim(
            slug=claim_slug,
            claim=text,
            supporting=[source_slug],
            confidence="high",
            last_reviewed=edited_at.date(),
            related_pages=[page_slug],
            evidence=text,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    store.write_page(
        WikiPage(
            slug=page_slug,
            title=page_slug,
            definition=text,
            overview=text,
            evidence=[claim_slug],
            sources=[source_slug],
            backlinks=[claim_slug],
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    return doc_id
