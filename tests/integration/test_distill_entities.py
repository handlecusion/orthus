"""K6 PR2 — distill entity 추출 + serial persist wiring 회귀 (구현 명세 §9.2).

distill은 entity 후보를 **추출만** 반환하고(원칙 1), serial write 단계의
`persist_distilled_entities`가 문서가 기여한 모든 company concept page에 mention을
적재한다(페이지 바인딩 결정 2026-06-13: source.related_pages 전부).

고정 성질: 방어 파싱(결측/비-list/비-dict/kind allowlist/name floor), 저신뢰
skip(정규화 본문 등장 < 2), dedup, _MAX_ENTITIES cap, 모든 관련 페이지 바인딩,
personal scope no-op(owner-scope 경계), author→persist→projection end-to-end.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from orthus.db import session
from orthus.documents import save_editor_document
from orthus.kg.entities import persist_distilled_entities
from orthus.kg.rebuild import run_rebuild
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import ExtractedEntity, WikiPage, WikiSource
from orthus.settings import get_settings
from orthus.tables import kg_entities, kg_entity_mentions
from orthus.wiki import author_from_document, store as wiki_store
from orthus.wiki.distill import _MAX_ENTITIES, _extract_entities, distill_document


@pytest.fixture
def pg_env(clean, tmp_path):
    """persist 테스트 — PG(clean, KG-off) + per-test tmp wiki-store(task 격리)."""
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield
    settings.wiki_store_path = prev


@pytest.fixture
def pg_kg_env(clean, tmp_path):
    """persist_distilled_entities 테스트 — KG flag ON(=live distill 경로) + PG +
    tmp wiki-store. wiring 래퍼는 kg_enabled()로 게이트되므로 적재 검증엔 ON 필요.
    Neo4j는 불필요(persist는 PG 전용)."""
    settings = get_settings()
    prev_store, prev_kg = settings.wiki_store_path, settings.kg_enabled
    settings.wiki_store_path = tmp_path / "wiki-store"
    settings.kg_enabled = True
    yield
    settings.wiki_store_path = prev_store
    settings.kg_enabled = prev_kg


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    """projection 테스트 — neo4j-test + PG + tmp wiki-store(clean → kg_clean 순)."""
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev


def _ent(kind: str, name: str) -> ExtractedEntity:
    return ExtractedEntity(kind=kind, name=name)


def _seed_page(user_id, slug: str, *, scope="company", owner_id=None) -> uuid.UUID:
    return wiki_store.write_page(
        WikiPage(slug=slug, title=f"페이지 {slug}", definition="정의", overview="개요"),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
    )


def _source(related: list[str]) -> WikiSource:
    return WikiSource(
        slug="src-x",
        title="문서",
        source_type="corpus_doc",
        source_ref="ref",
        ingested_at=datetime.now(UTC),
        summary="요약",
        related_pages=related,
    )


def _entity_rows():
    with session() as s:
        return s.execute(select(kg_entities.c.entity_key, kg_entities.c.display_name)).all()


def _count(driver, cypher: str) -> int:
    with driver.session(database="neo4j") as s:
        return s.run(cypher).single()["n"]


# --- 방어 파싱 + 저신뢰 skip (unit, DB 불필요) ----------------------------------


def test_extract_entities_defensive_parsing():
    body = "Kim and Kim met Acme. Acme is a company. Kim works at Acme."
    # 결측/비-list/비-dict 루트 → []
    assert _extract_entities(None, body) == []
    assert _extract_entities("not a list", body) == []
    assert _extract_entities({"kind": "person"}, body) == []

    raw = [
        {"kind": "person", "name": "Kim"},  # 본문 3회 → keep
        {"kind": "org", "name": "Acme"},  # 본문 3회 → keep
        {"kind": "alien", "name": "Acme"},  # kind allowlist 밖 → drop
        {"kind": "person", "name": "X"},  # name floor 미만 → drop
        "not a dict",  # per-item 비-dict → drop
        {"kind": "person"},  # name 결측 → drop
        {"kind": "person", "name": "Ghost"},  # 본문 0회 → drop
        {"kind": "person", "name": "Kim"},  # 정규화 dup → drop
    ]
    out = {(e.kind, e.name) for e in _extract_entities(raw, body)}
    assert out == {("person", "Kim"), ("org", "Acme")}


def test_extract_entities_low_confidence_skip():
    # 정규화 본문 등장 < 2면 drop(저신뢰), 2회 이상이면 keep
    assert _extract_entities([{"kind": "org", "name": "Acme"}], "Acme appears once.") == []
    kept = _extract_entities([{"kind": "org", "name": "Acme"}], "Acme and Acme again.")
    assert [(e.kind, e.name) for e in kept] == [("org", "Acme")]


def test_extract_entities_cap():
    raw = [{"kind": "org", "name": f"Org{i}"} for i in range(_MAX_ENTITIES + 5)]
    body = " ".join(f"Org{i} Org{i}" for i in range(_MAX_ENTITIES + 5))
    assert len(_extract_entities(raw, body)) == _MAX_ENTITIES


# --- serial persist 바인딩 (모든 관련 페이지) ----------------------------------


def test_persist_binds_all_related_pages(pg_kg_env, user_id):
    _seed_page(user_id, "page-a")
    _seed_page(user_id, "page-b")
    result = persist_distilled_entities(
        [_ent("person", "Kim"), _ent("org", "Acme")],
        source=_source(["page-a", "page-b"]),
        user_id=user_id,
        scope="company",
    )
    assert result.noop_scope is False
    assert result.persisted == 2  # distinct entity (page-불변)
    assert result.mentions == 4  # 2 entity × 2 page
    assert len(_entity_rows()) == 2
    with session() as s:
        pages = s.execute(select(kg_entity_mentions.c.page_id).distinct()).all()
    assert len(pages) == 2


def test_persist_skips_unresolved_pages(pg_kg_env, user_id):
    # related_pages가 company wiki page로 resolve 안 되면 해당 mention 없음
    _seed_page(user_id, "page-a")
    result = persist_distilled_entities(
        [_ent("person", "Kim")],
        source=_source(["page-a", "page-missing"]),
        user_id=user_id,
        scope="company",
    )
    assert result.mentions == 1  # page-a만 resolve


def test_persist_personal_scope_noop(pg_env, user_id):
    _seed_page(user_id, "p-personal", scope="personal", owner_id=user_id)
    result = persist_distilled_entities(
        [_ent("person", "Kim")],
        source=_source(["p-personal"]),
        user_id=user_id,
        scope="personal",
    )
    assert result.noop_scope is True
    assert _entity_rows() == []


def test_persist_empty_entities_noop(pg_kg_env, user_id):
    _seed_page(user_id, "page-a")
    result = persist_distilled_entities(
        [], source=_source(["page-a"]), user_id=user_id, scope="company"
    )
    assert result.mentions == 0
    assert _entity_rows() == []


def test_persist_kg_disabled_noop(pg_env, user_id):
    # fail-closed: KG off면 company distill이어도 PG entity 적재 skip(person PII 미수집).
    _seed_page(user_id, "page-a")
    result = persist_distilled_entities(
        [_ent("person", "Kim")],
        source=_source(["page-a"]),
        user_id=user_id,
        scope="company",
    )
    assert result.noop_kg_disabled is True
    assert result.mentions == 0
    assert _entity_rows() == []


# --- distill_document 추출 + end-to-end projection -------------------------------


def _distill_json_with_entities() -> str:
    return json.dumps(
        {
            "summary": "김민수와 Acme의 협력 관계.",
            "key_concepts": [],
            "terminology": [],
            "open_questions": [],
            "claims": [
                {
                    "claim": "김민수는 Acme에서 협력 업무를 담당한다.",
                    "evidence": "김민수는 Acme 협력 담당.",
                    "confidence": "high",
                    "page": {"slug": "collab", "title": "협력"},
                    "conflicting": [],
                }
            ],
            "entities": [
                {"kind": "person", "name": "김민수"},
                {"kind": "org", "name": "Acme"},
                {"kind": "person", "name": "Ghost"},  # 본문 0회 → distill에서 drop
            ],
        }
    )


def test_distill_document_extracts_entities(pg_env, user_id):
    chat = MockChat(default=_distill_json_with_entities())
    # 본문에 김민수×2, Acme×2 (Ghost 0회 → 저신뢰 drop)
    markdown = "김민수는 Acme에서 일한다. 김민수와 Acme는 협력한다."
    doc_id = save_editor_document(user_id, "협력 문서", [{}], markdown, chat_model=chat)
    result = distill_document(doc_id, user_id, chat_model=chat)
    extracted = {(e.kind, e.name) for e in result.entities}
    assert extracted == {("person", "김민수"), ("org", "Acme")}


def test_author_from_document_persists_and_projects(kg_env, user_id):
    chat = MockChat(default=_distill_json_with_entities())
    markdown = "김민수는 Acme에서 일한다. 김민수와 Acme는 협력한다."
    doc_id = save_editor_document(user_id, "협력 문서", [{}], markdown, chat_model=chat)
    author_from_document(doc_id, user_id, chat_model=chat)

    rows = {key for key, _ in _entity_rows()}
    assert rows == {"person:김민수", "org:acme"}

    run_rebuild()
    driver = kg_env
    assert _count(driver, "MATCH (e:Entity) RETURN count(e) AS n") == 2
    assert _count(driver, "MATCH (:Entity)-[r:MENTIONED_IN]->(:WikiPage) RETURN count(r) AS n") == 2
    # 같은 page(collab) co-mention → RELATES_TO 1쌍
    assert _count(driver, "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) RETURN count(r) AS n") == 1


def test_zero_claims_creates_no_fallback_claim(pg_env, user_id):
    """claim 0개 문서는 요약을 claim으로 승격하지 않는다 (summary→claim fallback 제거).

    요약은 **문서에 대한 서술**이지 지식이 아니다. claim으로 만들면 "…에 대한 순위를
    정리한 문서입니다" 같은 메타 페이지가 생기고, 그게 다음 질문의 근거로 걸려 사용자에게
    그대로 되돌아온다(2026-07-17 prod 재현). 실측: 살아있는 company 위키의 fallback
    claim 23개가 **예외 없이 전부** 메타 서술이었다(전체 claim 7535개의 0.3%).

    유실 없음의 근거: 원문은 corpus에, 요약은 WikiSource.summary에 그대로 남는다 —
    사라지는 건 가짜 claim과 그게 만들던 페이지뿐이다.
    """
    payload = {
        "summary": "AI 영상관련툴에 대한 순위를 정리한 문서입니다.",
        "key_concepts": ["ai-video-tools"],
        "key_claims": [],
        "terminology": [],
        "open_questions": [],
        "entities": [],
    }
    chat = MockChat(default=json.dumps(payload, ensure_ascii=False))
    doc_id = save_editor_document(
        user_id, "AI 영상관련툴", [{}], "AI관련 순위 정리", chat_model=chat
    )
    result = distill_document(doc_id, user_id, chat_model=chat)

    assert result.claims == []
    assert result.source.key_claims == []
    # 요약 자체는 source에 보존된다 (유실 아님 — 가짜 claim만 사라진다).
    assert result.source.summary == payload["summary"]
