"""E-N1b — entity 정규화 backfill 회귀 (docs/kg-entity-normalization-plan.md §8).

`run_entity_normalize`는 PG SoR(`kg_entities`/`kg_entity_mentions`)만 수렴시킨다
(Neo4j 미필요 — neo4j-test 없이 `clean` PG fixture로 돈다). 고정 성질:
- 불투명 Slack person ID 드롭(split 쌍 둘 다 opaque → 0 잔존).
- 파이프 실명/정준화가 같은 new_key로 수렴 → non-canonical 병합 + mention re-point.
- 같은 page 중복 mention은 UNIQUE(entity_id,page_id) 위반 대신 dedup 삭제.
- 정상 이름 불변(재정규화 no-op).
- apply 멱등(2회차 = 변경 0). dry-run은 카운트만·쓰기 0.
- company-scope only(owner-scope row 불변). `ORTHUS_KG_ENABLED=false` → noop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from orthus.db import session
from orthus.kg.client import reset_kg_force_disabled
from orthus.kg.entities import _conflict_task_slug
from orthus.kg.entity_normalize import run_entity_normalize
from orthus.kg.schema import KG_SCHEMA_VERSION
from orthus.schemas.canonical import WikiTask
from orthus.settings import get_settings
from orthus.tables import kg_entities, kg_entity_mentions
from orthus.wiki import store as wiki_store

_T0 = datetime(2026, 7, 7, tzinfo=UTC)


@pytest.fixture
def kg_on(clean, tmp_path):
    """PG(clean, company node) + KG flag on + per-test tmp wiki-store(conflict task 격리).

    backfill은 Neo4j를 안 쓰므로 flag만 켠다. wiki_store_path는 §9.2 stale conflict
    task 탐지(load_task)가 실제 wiki-store를 읽지 않도록 tmp로 격리한다."""
    reset_kg_force_disabled()
    settings = get_settings()
    prev_kg = settings.kg_enabled
    prev_ws = settings.wiki_store_path
    settings.kg_enabled = True
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield
    settings.kg_enabled = prev_kg
    settings.wiki_store_path = prev_ws


def _seed_entity(
    kind: str,
    display: str,
    *,
    scope: str = "company",
    owner_id: uuid.UUID | None = None,
    first_seen: datetime = _T0,
    name_norm: str | None = None,
    entity_key: str | None = None,
) -> uuid.UUID:
    """원문 그대로 kg_entities row 삽입(옛 정규화 시뮬레이션 — 정준화 안 된 display 허용)."""
    eid = uuid.uuid4()
    nn = name_norm if name_norm is not None else display.casefold()
    ek = entity_key if entity_key is not None else f"{kind}:{nn}"
    with session() as s:
        s.execute(
            kg_entities.insert().values(
                entity_id=eid,
                entity_key=ek,
                entity_kind=kind,
                name_norm=nn,
                display_name=display,
                scope=scope,
                owner_id=owner_id,
                first_seen=first_seen,
                last_seen=first_seen,
                schema_version=KG_SCHEMA_VERSION,
                created_at=first_seen,
                updated_at=first_seen,
            )
        )
        s.commit()
    return eid


def _seed_mention(entity_id: uuid.UUID, page_id: uuid.UUID) -> None:
    with session() as s:
        s.execute(
            kg_entity_mentions.insert().values(
                mention_id=uuid.uuid4(),
                entity_id=entity_id,
                page_id=page_id,
                evidence_slug=f"slug-{str(page_id)[:8]}",
                schema_version=KG_SCHEMA_VERSION,
                created_at=_T0,
            )
        )
        s.commit()


def _entity_rows() -> list:
    with session() as s:
        return s.execute(
            select(
                kg_entities.c.entity_id,
                kg_entities.c.entity_key,
                kg_entities.c.entity_kind,
                kg_entities.c.name_norm,
                kg_entities.c.display_name,
                kg_entities.c.scope,
            )
        ).all()


def _count(table) -> int:
    with session() as s:
        return s.execute(select(func.count()).select_from(table)).scalar_one()


def _last_seen(entity_id: uuid.UUID) -> datetime:
    with session() as s:
        return s.execute(
            select(kg_entities.c.last_seen).where(kg_entities.c.entity_id == entity_id)
        ).scalar_one()


def _reconciles(report) -> bool:
    """리포트 카운트가 스캔한 전 행을 계정하는지: considered == 드롭 + 병합 + 갱신 + 불변."""
    return report.considered == (
        report.dropped_opaque + report.merged + report.updated_canonical + report.unchanged
    )


# --- 드롭 -----------------------------------------------------------------


def test_drop_opaque_pair(kg_on):
    """split 쌍(`<@U0ATKMHKZBL>` + bare `U0ATKMHKZBL`)은 둘 다 person 불투명 → 드롭."""
    a = _seed_entity("person", "<@U0ATKMHKZBL>")
    b = _seed_entity("person", "U0ATKMHKZBL")
    _seed_mention(a, uuid.uuid4())
    _seed_mention(b, uuid.uuid4())

    report = run_entity_normalize(apply=True)

    assert report.considered == 2
    assert report.dropped_opaque == 2
    assert report.merged == 0
    assert _count(kg_entities) == 0
    assert _count(kg_entity_mentions) == 0


def test_non_person_opaque_id_kept(kg_on):
    """org/system은 ID 꼴이어도 드롭 대상 아님(person 한정)."""
    _seed_entity("org", "U0ATKMHKZBL")
    report = run_entity_normalize(apply=True)
    assert report.dropped_opaque == 0
    assert _count(kg_entities) == 1


# --- 병합 -----------------------------------------------------------------


def test_merge_pipe_realname_into_bare(kg_on):
    """`<@U012ABCDEF|김철수>`(파이프 실명)과 `김철수`가 같은 new_key로 수렴 → 병합."""
    marked = _seed_entity("person", "<@U012ABCDEF|김철수>", first_seen=_T0 + timedelta(days=1))
    plain = _seed_entity("person", "김철수", first_seen=_T0)  # 더 이른 first_seen → canonical
    _seed_mention(marked, uuid.uuid4())
    _seed_mention(plain, uuid.uuid4())

    report = run_entity_normalize(apply=True)

    assert report.considered == 2
    assert report.merged == 1
    assert report.rerouted_mentions == 1  # marked의 mention이 plain으로 이동
    assert report.dropped_opaque == 0

    rows = _entity_rows()
    assert len(rows) == 1
    (only,) = rows
    assert only.entity_id == plain  # first_seen 최소 → canonical
    assert only.entity_key == "person:김철수"
    assert only.display_name == "김철수"
    # 두 mention 모두 canonical에 붙어 있다(서로 다른 page).
    assert _count(kg_entity_mentions) == 2


def test_merge_dedups_same_page_mention(kg_on):
    """두 엔티티가 같은 page를 언급하면 병합 시 중복 mention 삭제(UNIQUE 위반 회피)."""
    page = uuid.uuid4()
    marked = _seed_entity("person", "<@U012ABCDEF|박영희>", first_seen=_T0 + timedelta(days=1))
    plain = _seed_entity("person", "박영희", first_seen=_T0)
    _seed_mention(marked, page)
    _seed_mention(plain, page)  # 같은 page

    report = run_entity_normalize(apply=True)

    assert report.merged == 1
    assert report.deduped_mentions == 1
    assert report.rerouted_mentions == 0
    assert _count(kg_entities) == 1
    assert _count(kg_entity_mentions) == 1  # dedup


def test_three_way_merge(kg_on):
    """3개 병합에서 canonical 결정론 선정 + mention 전부 수렴."""
    e1 = _seed_entity("person", "<@U001AAA|이수진>", first_seen=_T0 + timedelta(days=2))
    e2 = _seed_entity("person", "이수진", first_seen=_T0)  # canonical
    e3 = _seed_entity("person", "<@U002BBB|이수진>", first_seen=_T0 + timedelta(days=1))
    _seed_mention(e1, uuid.uuid4())
    _seed_mention(e2, uuid.uuid4())
    _seed_mention(e3, uuid.uuid4())

    report = run_entity_normalize(apply=True)

    assert report.merged == 2
    assert report.rerouted_mentions == 2
    rows = _entity_rows()
    assert len(rows) == 1
    assert rows[0].entity_id == e2
    assert _count(kg_entity_mentions) == 3


def test_merge_unions_last_seen(kg_on):
    """canonical(first_seen 최소)이 더 최근 last_seen 멤버를 흡수하면 last_seen을 max로 union."""
    plain = _seed_entity("person", "최유리", first_seen=_T0)  # canonical, last_seen=_T0
    marked = _seed_entity(
        "person", "<@U012ABCDEF|최유리>", first_seen=_T0 + timedelta(days=5)
    )  # last_seen=_T0+5d
    _seed_mention(plain, uuid.uuid4())
    _seed_mention(marked, uuid.uuid4())

    report = run_entity_normalize(apply=True)

    assert report.merged == 1
    # canonical은 값이 안 바뀌므로 updated_canonical 아님 → unchanged, last_seen만 최신화.
    assert report.updated_canonical == 0
    assert report.unchanged == 1
    assert _last_seen(plain) == _T0 + timedelta(days=5)  # 흡수한 멤버의 더 최근 관측
    assert _reconciles(report)


def test_merge_unions_last_seen_when_canonical_changed(kg_on):
    """canonical이 value_changed(display 정규화)이면서도 흡수 멤버의 더 최근 last_seen을 union.

    canonical=마크업 row(first_seen 최소)라 display/norm/key가 바뀌고(updated_canonical),
    동시에 더 최근 last_seen을 가진 plain 멤버를 흡수한다 — 두 last_seen 경로 중
    value_changed 분기(같은 UPDATE에 last_seen 포함)를 고정한다."""
    marked = _seed_entity(
        "person", "<@U012ABCDEF|김철수>", first_seen=_T0
    )  # canonical, last_seen=_T0
    plain = _seed_entity("person", "김철수", first_seen=_T0 + timedelta(days=7))  # last_seen=_T0+7d
    _seed_mention(marked, uuid.uuid4())
    _seed_mention(plain, uuid.uuid4())

    report = run_entity_normalize(apply=True)

    assert report.merged == 1
    assert report.updated_canonical == 1  # canonical display 정규화됨
    assert report.unchanged == 0
    rows = _entity_rows()
    assert len(rows) == 1
    (only,) = rows
    assert only.entity_id == marked
    assert only.display_name == "김철수" and only.entity_key == "person:김철수"
    assert _last_seen(marked) == _T0 + timedelta(days=7)  # value_changed 분기도 last_seen union
    assert _reconciles(report)


# --- 리포트 정합성 ---------------------------------------------------------


def test_report_counts_reconcile(kg_on):
    """considered == dropped + merged + updated_canonical + unchanged (병합 그룹 canonical 포함)."""
    # 드롭 1, 병합 그룹(canonical 불변) 1, 단독 불변 1, 단독 갱신(display-only) 1.
    _seed_entity("person", "<@U0DEADBEEF>")  # 드롭
    plain = _seed_entity("person", "한서준", first_seen=_T0)  # 병합 canonical(불변)
    _seed_entity("person", "<@U012ABCDEF|한서준>", first_seen=_T0 + timedelta(days=1))  # 흡수
    _seed_entity("org", "Nova")  # 단독 불변
    # display-only 변경: 저장은 마크업 포함, 재정규화 시 'Orbit'으로 수렴(단독 그룹).
    _seed_entity("project", "<https://x|Orbit>")
    _seed_mention(plain, uuid.uuid4())

    report = run_entity_normalize(apply=True)

    assert report.considered == 5
    assert report.dropped_opaque == 1
    assert report.merged == 1
    assert report.updated_canonical == 1  # Orbit display-only 수렴
    assert report.unchanged == 2  # 한서준 canonical + Nova
    assert _reconciles(report)


# --- 그룹 간 entity_key swap (two-phase 방어) ------------------------------


def test_cross_group_key_swap(kg_on):
    """저장된 entity_key/name_norm이 display와 어긋난 두 row가 서로의 옛 키로 수렴해도
    UNIQUE(entity_key) 충돌 없이 재배치된다(two-phase key park). E-N1a 델타 자체엔
    없는 상태지만, 재사용 backfill이 향후 정규화 델타에서 만날 수 있는 key swap을 방어."""
    # A: display 'Alice'(→new_key person:alice)지만 현재 person:bob을 쥠(drift 시뮬레이션).
    a = _seed_entity("person", "Alice", name_norm="bob", entity_key="person:bob", first_seen=_T0)
    # B: display 'Bob'(→new_key person:bob)지만 현재 person:alice를 쥠.
    b = _seed_entity(
        "person",
        "Bob",
        name_norm="alice",
        entity_key="person:alice",
        first_seen=_T0 + timedelta(days=1),
    )

    report = run_entity_normalize(apply=True)  # 충돌 없이 완료돼야 한다

    assert report.merged == 0  # 서로 다른 그룹 → 병합 아님
    assert report.updated_canonical == 2
    assert _reconciles(report)
    rows = {r.entity_id: r for r in _entity_rows()}
    assert rows[a].entity_key == "person:alice" and rows[a].display_name == "Alice"
    assert rows[b].entity_key == "person:bob" and rows[b].display_name == "Bob"


# --- §9.2 entity_conflict WikiTask 재평가 (탐지·리포트, 자동 resolve 안 함) -----


def _write_conflict_task(name_norm: str, display: str) -> str:
    """name_norm에 대한 entity_conflict WikiTask를 tmp wiki-store에 쓴다. slug 반환."""
    slug = _conflict_task_slug(name_norm)
    wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="entity_conflict",
            description=f"Entity '{display}' 서로 다른 종류로 분류됨: org, person.",
            related=["some-page"],
            created_at=_T0,
        ),
        user_id=uuid.uuid4(),
        scope="company",
    )
    return slug


def test_stale_conflict_task_detected_on_drop(kg_on):
    """드롭이 모순을 소멸시키면 그 name_norm의 entity_conflict task를 stale로 리포트."""
    # 같은 name_norm 'u0abcdef'에 person + org (모순). person은 불투명 → 드롭됨.
    _seed_entity("person", "U0ABCDEF")  # name_norm 'u0abcdef', 드롭 대상
    _seed_entity("org", "U0ABCDEF")  # name_norm 'u0abcdef', org는 유지
    slug = _write_conflict_task("u0abcdef", "U0ABCDEF")

    report = run_entity_normalize(apply=True)

    assert report.dropped_opaque == 1  # person만 드롭
    # 드롭 후 'u0abcdef'는 org 1종 → 모순 소멸 → task stale.
    assert report.stale_conflict_tasks == [slug]
    # 자동 resolve 금지 — task 파일은 그대로 남아 있어야 한다(운영자 리뷰).
    assert wiki_store.load_task(slug, scope="company").resolved is False


def test_stale_conflict_dry_run_matches_apply(kg_on):
    """stale 탐지는 dry-run/apply 동일(in-memory 그룹 + read-only load_task)."""
    _seed_entity("person", "U0ABCDEF")
    _seed_entity("org", "U0ABCDEF")
    slug = _write_conflict_task("u0abcdef", "U0ABCDEF")

    dry = run_entity_normalize(apply=False)
    assert dry.stale_conflict_tasks == [slug]
    assert wiki_store.load_task(slug, scope="company") is not None  # dry-run은 삭제 안 함


def test_live_conflict_not_flagged_stale(kg_on):
    """모순이 유지되는(양쪽 다 살아남는) name_norm의 task는 stale로 오탐하지 않는다."""
    _seed_entity("person", "Kim")  # name_norm 'kim', 정상 → 유지
    _seed_entity("org", "Kim")  # name_norm 'kim', 유지 → 모순 유효
    _write_conflict_task("kim", "Kim")
    # 무관한 불투명 person 하나 드롭(affected_norms에 'kim' 미포함 확인용).
    _seed_entity("person", "U0DEADBEEF")

    report = run_entity_normalize(apply=True)

    assert report.dropped_opaque == 1
    assert report.stale_conflict_tasks == []  # 'kim' 모순 유효 → stale 아님


def test_resolved_conflict_task_not_flagged(kg_on):
    """이미 resolved된 conflict task는 stale로 리포트하지 않는다."""
    _seed_entity("person", "U0ABCDEF")
    _seed_entity("org", "U0ABCDEF")
    slug = _conflict_task_slug("u0abcdef")
    wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="entity_conflict",
            description="이미 처리됨",
            related=["some-page"],
            created_at=_T0,
            resolved=True,
        ),
        user_id=uuid.uuid4(),
        scope="company",
    )

    report = run_entity_normalize(apply=True)

    assert report.dropped_opaque == 1
    assert report.stale_conflict_tasks == []  # resolved면 제외


# --- 불변 -----------------------------------------------------------------


def test_normal_names_unchanged(kg_on):
    """정상 이름(Slack 표기 없음)은 재정규화 no-op — 병합/드롭/업데이트 0."""
    for kind, name in [
        ("person", "이철"),
        ("org", "Nova"),
        ("project", "Twelve Labs"),
        ("system", "Slack"),
    ]:
        _seed_entity(kind, name)

    report = run_entity_normalize(apply=True)

    assert report.considered == 4
    assert report.dropped_opaque == 0
    assert report.merged == 0
    assert report.updated_canonical == 0
    assert report.unchanged == 4
    assert _count(kg_entities) == 4


def test_dry_run_no_writes(kg_on):
    """dry-run은 카운트만 — DB 미변경."""
    a = _seed_entity("person", "<@U0ATKMHKZBL>")
    marked = _seed_entity("person", "<@U012ABCDEF|정민수>", first_seen=_T0 + timedelta(days=1))
    plain = _seed_entity("person", "정민수", first_seen=_T0)
    _seed_mention(a, uuid.uuid4())
    _seed_mention(marked, uuid.uuid4())
    _seed_mention(plain, uuid.uuid4())

    before_e, before_m = _count(kg_entities), _count(kg_entity_mentions)
    report = run_entity_normalize(apply=False)

    assert report.dry_run is True
    assert report.dropped_opaque == 1
    assert report.merged == 1
    assert report.rerouted_mentions == 1
    assert report.changed is True
    # 쓰기 0.
    assert _count(kg_entities) == before_e == 3
    assert _count(kg_entity_mentions) == before_m == 3


def test_apply_idempotent(kg_on):
    """apply 두 번 — 2회차는 변경 0(멱등)."""
    a = _seed_entity("person", "<@U0ATKMHKZBL>")
    marked = _seed_entity("person", "<@U012ABCDEF|한지우>", first_seen=_T0 + timedelta(days=1))
    plain = _seed_entity("person", "한지우", first_seen=_T0)
    _seed_mention(a, uuid.uuid4())
    _seed_mention(marked, uuid.uuid4())
    _seed_mention(plain, uuid.uuid4())

    first = run_entity_normalize(apply=True)
    assert first.changed is True

    second = run_entity_normalize(apply=True)
    assert second.dropped_opaque == 0
    assert second.merged == 0
    assert second.updated_canonical == 0
    assert second.changed is False


# --- 경계 -----------------------------------------------------------------


def test_owner_scope_untouched(kg_on):
    """owner-scope(personal) entity row는 backfill 대상 아님(company-scope only)."""
    owner = uuid.uuid4()
    personal = _seed_entity("person", "<@U0ATKMHKZBL>", scope="personal", owner_id=owner)
    company = _seed_entity("person", "<@U0COMPANY9>")
    _seed_mention(personal, uuid.uuid4())
    _seed_mention(company, uuid.uuid4())

    report = run_entity_normalize(apply=True)

    assert report.considered == 1  # company row만
    assert report.dropped_opaque == 1
    rows = _entity_rows()
    assert len(rows) == 1
    assert rows[0].entity_id == personal  # personal 불변
    assert rows[0].scope == "personal"


def test_kg_disabled_noop(clean):
    """ORTHUS_KG_ENABLED=false → 정규화 대상 없음(noop), 쓰기 0."""
    reset_kg_force_disabled()
    settings = get_settings()
    prev = settings.kg_enabled
    settings.kg_enabled = False
    try:
        _seed_entity("person", "<@U0ATKMHKZBL>")
        report = run_entity_normalize(apply=True)
        assert report.noop_kg_disabled is True
        assert report.considered == 0
        assert _count(kg_entities) == 1  # 미변경
    finally:
        settings.kg_enabled = prev
