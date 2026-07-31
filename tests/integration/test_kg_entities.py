"""K6 PR1 — entity substrate 회귀 (구현 명세 §9.5, kg-model §2).

persist 경로(PG + wiki-store)는 neo4j 없이 돈다(`pg_env`). projection/prune은
neo4j-test(:7688)가 필요하다(`kg_env`, `make kg-test-up && make test`).

고정 성질: 보수적 name 정규화, dedupe 멱등, scope!=company no-op(boundary 쓰기측),
entity_conflict WikiTask emit + conflict-index 비오염 + agentwork enum 무오염,
person redaction, :Entity/MENTIONED_IN/RELATES_TO projection, prune 수렴.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from orthus.db import session
from orthus.kg.entities import (
    _clean_name,
    _demarkup,
    _is_opaque_person_id,
    persist_entities,
)
from orthus.kg.project import _build_conflict_index
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import ExtractedEntity, WikiPage, WikiTask
from orthus.settings import get_settings
from orthus.tables import kg_entities, kg_entity_mentions
from orthus.wiki import store as wiki_store


@pytest.fixture
def pg_env(clean, tmp_path):
    """persist 테스트 — PG(clean, KG-off) + per-test tmp wiki-store(task 격리)."""
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield
    settings.wiki_store_path = prev


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    """projection 테스트 — neo4j-test + PG + tmp wiki-store. fixture 순서 계약:
    clean(KG-off 기본) → kg_clean(test 컨테이너 override)."""
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev


def _seed_page(user_id, slug: str, *, scope="company", owner_id=None) -> uuid.UUID:
    return wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=f"페이지 {slug}",
            definition="정의",
            overview="개요",
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
    )


def _ent(kind: str, name: str) -> ExtractedEntity:
    return ExtractedEntity(kind=kind, name=name)


def _entity_rows():
    with session() as s:
        return s.execute(
            select(kg_entities.c.entity_key, kg_entities.c.display_name, kg_entities.c.owner_id)
        ).all()


# --- 보수적 정규화 (unit, DB 불필요) --------------------------------------------


def test_entity_norm_rules():
    assert _clean_name("  Kim   Minsu ") == "Kim Minsu"  # 공백 collapse
    assert _clean_name("ＡＢＣ") == "ABC"  # NFKC
    # 직함/조사 strip 없음(보수적 — 손상 방지): '김대표'는 그대로
    assert _clean_name("김대표") == "김대표"
    assert _clean_name("김대표님") == "김대표님"
    # name_norm = casefold(display)
    assert _clean_name("Acme").casefold() == "acme"


# --- Slack 표기 정준화 + 불투명 ID 드롭 (E-N1a, unit) ----------------------------


def test_demarkup_slack_notation():
    # user 멘션: 파이프 실명 복원 / bare는 ID로 환원
    assert _demarkup("<@U0ATKMHKZBL|김철수>") == "김철수"
    assert _demarkup("<@U0ATKMHKZBL>") == "U0ATKMHKZBL"
    # 채널/subteam/broadcast/링크
    assert _demarkup("<#C012ABCDEF|공지>") == "공지"
    assert _demarkup("<!subteam^S012|팀명>") == "팀명"
    assert _demarkup("<!here>").strip() == ""
    assert _demarkup("<https://x.com|문서>") == "문서"
    # 일반 텍스트의 `<`/`@`는 불변(Slack 델리미터 아님)
    assert _demarkup("a < b @ c") == "a < b @ c"
    assert _demarkup("kim@example.com") == "kim@example.com"


def test_clean_name_merges_slack_split():
    # <@U…>와 bare U…가 같은 name_norm으로 수렴(중복 split 원천 차단)
    assert _clean_name("<@U0ATKMHKZBL>").casefold() == _clean_name("U0ATKMHKZBL").casefold()


def test_clean_name_preserves_normal_names():
    # 정상 이름은 정준화 후 불변(과잉 strip 방지 회귀)
    for name in ("이철", "Nova", "Twelve Labs", "SphereDiff", "김대표", "김대표님"):
        assert _clean_name(name) == name


def test_is_opaque_person_id():
    assert _is_opaque_person_id("person", "U0ATKMHKZBL") is True
    assert _is_opaque_person_id("person", "W012ABCDEF") is True
    assert _is_opaque_person_id("person", "이철") is False
    assert _is_opaque_person_id("person", "김철수") is False
    # kind 가드 — person 외에는 대상 아님
    assert _is_opaque_person_id("system", "U0ATKMHKZBL") is False
    assert _is_opaque_person_id("org", "U0ATKMHKZBL") is False


def test_persist_drops_opaque_person_id(pg_env, user_id):
    """불투명 Slack ID(person)는 드롭되고, split 중복은 하나로 수렴한다(E-N1a)."""
    pid = _seed_page(user_id, "page-slack")
    ents = [
        _ent("person", "<@U0ATKMHKZBL>"),  # 드롭
        _ent("person", "U0ATKMHKZBL"),  # 드롭(같은 대상)
        _ent("person", "이철"),  # 정상 유지
    ]
    r = persist_entities(ents, page_id=pid, slug="page-slack", scope="company", user_id=user_id)
    assert r.dropped_opaque == 2
    assert r.persisted == 1
    keys = sorted(k for k, _, _ in _entity_rows())
    assert keys == ["person:이철"]


def test_persist_pipe_realname_merges(pg_env, user_id):
    """<@U…|실명>은 실명으로 복원돼 같은 실명 엔티티와 병합된다(드롭 아님)."""
    pid = _seed_page(user_id, "page-pipe")
    ents = [_ent("person", "<@U0ATKMHKZBL|이철>"), _ent("person", "이철")]
    r = persist_entities(ents, page_id=pid, slug="page-pipe", scope="company", user_id=user_id)
    assert r.dropped_opaque == 0
    keys = sorted(k for k, _, _ in _entity_rows())
    assert keys == ["person:이철"]  # 병합 → 1개


def test_person_entity_redaction(pg_env, user_id):
    """이름 속 이메일/전화는 redact_pii로 마스킹된다(carve-out 보상통제).
    redact는 local part를 마스킹한다(kim → k***) — 원본 이메일은 사라지고
    사람 이름은 보존된다(company knowledge)."""
    cleaned = _clean_name("김민수 kim@example.com")
    assert "kim@example.com" not in cleaned  # 원본 이메일 제거
    assert "김민수" in cleaned  # 이름 보존


# --- persist (PG only) ----------------------------------------------------------


def test_persist_entities_dedupe_idempotent(pg_env, user_id):
    pid = _seed_page(user_id, "page-e")
    ents = [_ent("person", "Kim"), _ent("org", "Acme")]
    r1 = persist_entities(ents, page_id=pid, slug="page-e", scope="company", user_id=user_id)
    assert r1.persisted == 2 and r1.mentions == 2
    before = {k: dn for k, dn, _ in _entity_rows()}
    with session() as s:
        upd_before = dict(
            s.execute(select(kg_entities.c.entity_key, kg_entities.c.updated_at)).all()
        )
    # 동일 재-persist → 행/mention 무증가, updated_at 불변(watermark churn 없음)
    r2 = persist_entities(ents, page_id=pid, slug="page-e", scope="company", user_id=user_id)
    assert r2.persisted == 2 and r2.mentions == 0  # mention UNIQUE → 0 신규
    after = {k: dn for k, dn, _ in _entity_rows()}
    assert before == after
    assert len(after) == 2
    with session() as s:
        upd_after = dict(
            s.execute(select(kg_entities.c.entity_key, kg_entities.c.updated_at)).all()
        )
    assert upd_before == upd_after  # display 불변 → updated_at no-op


def test_persist_entities_owner_id_null(pg_env, user_id):
    pid = _seed_page(user_id, "page-o")
    persist_entities(
        [_ent("person", "Kim")], page_id=pid, slug="page-o", scope="company", user_id=user_id
    )
    assert all(owner_id is None for _, _, owner_id in _entity_rows())


def test_persist_entities_scope_noop(pg_env, user_id):
    """scope != company → 쓰기 경계 fail-closed no-op(owner-scope boundary, K7 전)."""
    pid = _seed_page(user_id, "page-p", scope="personal", owner_id=user_id)
    r = persist_entities(
        [_ent("person", "Kim")], page_id=pid, slug="page-p", scope="personal", user_id=user_id
    )
    assert r.noop_scope and r.persisted == 0
    assert _entity_rows() == []
    # unknown scope도 deny
    r2 = persist_entities(
        [_ent("person", "Kim")], page_id=pid, slug="page-p", scope="weird", user_id=user_id
    )
    assert r2.noop_scope and _entity_rows() == []


def test_entity_conflict_creates_wiki_task(pg_env, user_id):
    """같은 name_norm, 다른 entity_kind → WikiTask(kind=entity_conflict), 두 row 보존."""
    pid = _seed_page(user_id, "page-c")
    persist_entities(
        [_ent("person", "김대표")], page_id=pid, slug="page-c", scope="company", user_id=user_id
    )
    r = persist_entities(
        [_ent("org", "김대표")], page_id=pid, slug="page-c", scope="company", user_id=user_id
    )
    assert r.conflicts, "entity_conflict task가 emit돼야 함"
    keys = sorted(k for k, _, _ in _entity_rows())
    assert keys == ["org:김대표", "person:김대표"]  # 두 row 모두 적재
    # WikiTask가 entity_conflict kind로 로드된다(Literal 확장 검증 = agentwork enum 무오염)
    task = wiki_store.load_task(r.conflicts[0], scope="company")
    assert task is not None and task.kind == "entity_conflict"


def test_entity_conflict_task_excluded_from_conflict_index(pg_env, user_id):
    """entity_conflict task의 related 쌍이 CONFLICTS_WITH 엣지 속성 인덱스를
    오염시키지 않는다(검토 BLOCKER — kind 분리)."""
    # 합성: 2-related entity_conflict task를 직접 써서 필터를 직접 검증
    wiki_store.write_task(
        WikiTask(
            slug="entity-conflict-synthetic",
            kind="entity_conflict",
            description="synthetic",
            related=["claim-a", "claim-b"],
            created_at=datetime.now(UTC),
        ),
        user_id=user_id,
    )
    idx = _build_conflict_index({})
    # K8.6 — 키는 (namespace, slug_a, slug_b). entity_conflict는 conflict 인덱스에서 제외.
    assert ("company", "claim-a", "claim-b") not in idx


def test_entity_conflict_policy_outcome_is_draft_for_review():
    """entity_conflict는 cleanup-kind가 아니므로 auto_execute되지 않고 owner review
    draft로 수렴한다(K6 §9.2 정합 — 자동 실행 금지 + 검토 필수를 회귀로 고정)."""
    from orthus.agentwork.service import wiki_task_candidate
    from orthus.agentwork.state import apply_policy

    task = WikiTask(
        slug="entity-conflict-policy",
        kind="entity_conflict",
        description="같은 이름·다른 kind 검토 필요",
        related=["page-a"],
        created_at=datetime.now(UTC),
    )
    cand = wiki_task_candidate(task, scope="company", node_id="company")
    assert cand.payload["cleanup_only"] is False
    decision = apply_policy(cand)
    assert decision.outcome == "draft_for_review"
    assert decision.outcome != "auto_execute"


# --- projection / prune (neo4j-test 필요) ---------------------------------------


def _count(driver, cypher: str, **p) -> int:
    with driver.session(database="neo4j") as s:
        return s.run(cypher, **p).single()["n"]


def test_entity_projection_and_relates_to(kg_env, user_id):
    pid = _seed_page(user_id, "page-rel")
    persist_entities(
        [_ent("person", "Kim"), _ent("org", "Acme")],
        page_id=pid,
        slug="page-rel",
        scope="company",
        user_id=user_id,
    )
    run_rebuild()
    driver = kg_env
    assert _count(driver, "MATCH (e:Entity) RETURN count(e) AS n") == 2
    assert _count(driver, "MATCH (:Entity)-[r:MENTIONED_IN]->(:WikiPage) RETURN count(r) AS n") == 2
    # 같은 page co-mention → RELATES_TO 1쌍(정렬 1방향), evidence_slug=page slug
    assert _count(driver, "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) RETURN count(r) AS n") == 1
    with driver.session(database="neo4j") as s:
        rec = s.run(
            "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) RETURN r.kind AS k, r.evidence_slug AS s"
        ).single()
    assert rec["k"] == "co_mention" and rec["s"] == "page-rel"


def test_prune_removes_stale_entity(kg_env, user_id):
    pid = _seed_page(user_id, "page-prune")
    persist_entities(
        [_ent("person", "Kim"), _ent("org", "Acme")],
        page_id=pid,
        slug="page-prune",
        scope="company",
        user_id=user_id,
    )
    run_rebuild()
    driver = kg_env
    assert _count(driver, "MATCH (e:Entity) RETURN count(e) AS n") == 2
    # org:acme entity + 그 mention/relates 삭제 → rebuild가 노드+엣지 prune
    with session() as s:
        ek = s.execute(
            select(kg_entities.c.entity_id).where(kg_entities.c.entity_key == "org:acme")
        ).scalar_one()
        s.execute(delete(kg_entity_mentions).where(kg_entity_mentions.c.entity_id == ek))
        s.execute(delete(kg_entities).where(kg_entities.c.entity_id == ek))
        s.commit()
    run_rebuild()
    assert _count(driver, "MATCH (e:Entity) RETURN count(e) AS n") == 1  # org:acme 제거
    assert (
        _count(driver, "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) RETURN count(r) AS n") == 0
    )  # co-mention 쌍 사라짐(엣지 prune — BLOCKER 회귀)
    # 남은 person:kim의 MENTIONED_IN은 2회 rebuild 후에도 생존
    run_rebuild()
    assert _count(driver, "MATCH (:Entity)-[r:MENTIONED_IN]->(:WikiPage) RETURN count(r) AS n") == 1
