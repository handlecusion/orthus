"""distill.py wiring: structured-row claim tautology suppression.

Notion DB row properties render as a flat "**Prop**: Value" markdown dump
(`connectors/notion.py::_render_properties`). Distilling that flattened dump
regenerates self-reference / bare-filler-adjective claims with no synthesis
beyond the raw table cell (docs/agent-chat-answer-quality.md 잔여 항목:
"DB row 동어반복 claim"). `orthus.wiki.task_hygiene.is_structured_row_tautology`
is unit-tested in tests/integration/test_wiki_task_hygiene.py; this file pins
the distill.py wiring boundary end-to-end: the suppression fires only for
structured-row sources (`source_db_name` set) and never touches narrative
documents (e.g. editor docs), even for byte-identical claim text.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import insert

from orthus.db import session
from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.settings import get_settings
from orthus.tables import documents
from orthus.wiki.distill import distill_document


@pytest.fixture
def pg_env(clean, tmp_path):
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield
    settings.wiki_store_path = prev


def _seed_structured_row_doc(user_id, *, title: str, markdown: str = "본문") -> uuid.UUID:
    """Insert a document that looks like a Notion DB row (source_db_name set)."""
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title=title,
                block_json={},
                markdown=markdown,
                source="notion",
                source_db_name="모델 카드 DB",
                schema_version=1,
                scope="company",
                project="company",
            )
        )
        s.commit()
    return doc_id


def _payload(claims: list[dict]) -> str:
    return json.dumps(
        {
            "summary": "요약",
            "key_concepts": [],
            "terminology": [],
            "open_questions": [],
            "claims": claims,
            "entities": [],
        },
        ensure_ascii=False,
    )


_TAUTOLOGY_CLAIMS = [
    {
        "claim": "Wan2.2의 이름은 Wan2.2이다.",
        "headline": "Wan2.2 이름",
        "evidence": "**이름**: Wan2.2",
        "confidence": "high",
        "page": {"slug": "wan2-2", "title": "Wan2.2"},
        "conflicting": [],
    },
    {
        "claim": "우선순위는 높음",
        "headline": "우선순위",
        "evidence": "**우선순위**: 높음",
        "confidence": "high",
        "page": {"slug": "wan2-2", "title": "Wan2.2"},
        "conflicting": [],
    },
]
_INFORMATIVE_CLAIM = {
    "claim": "Wan2.2는 텍스트-투-비디오 생성 모델이다.",
    "headline": "Wan2.2 모델 종류",
    "evidence": "**설명**: 텍스트-투-비디오 생성 모델",
    "confidence": "high",
    "page": {"slug": "wan2-2", "title": "Wan2.2"},
    "conflicting": [],
}


def test_distill_document_suppresses_structured_row_tautology(pg_env, user_id):
    chat = MockChat(default=_payload([*_TAUTOLOGY_CLAIMS, _INFORMATIVE_CLAIM]))
    doc_id = _seed_structured_row_doc(user_id, title="Wan2.2")

    result = distill_document(doc_id, user_id, chat_model=chat)

    claim_texts = [c.claim for c in result.claims]
    assert claim_texts == ["Wan2.2는 텍스트-투-비디오 생성 모델이다."]
    assert result.source.key_claims == [c.slug for c in result.claims]


def test_distill_document_keeps_identical_wording_for_narrative_source(pg_env, user_id):
    """The exact same tautological wording survives when the document is NOT a
    structured row (an editor doc genuinely discussing naming/priority) — the
    suppression is strictly scoped to structured-row sources, never a blanket
    text filter."""
    chat = MockChat(default=_payload(_TAUTOLOGY_CLAIMS))
    doc_id = save_editor_document(
        user_id, "이름 규칙 문서", [{}], "명명 규칙과 우선순위 기준을 설명합니다.", chat_model=chat
    )

    result = distill_document(doc_id, user_id, chat_model=chat)

    claim_texts = [c.claim for c in result.claims]
    assert claim_texts == ["Wan2.2의 이름은 Wan2.2이다.", "우선순위는 높음"]
