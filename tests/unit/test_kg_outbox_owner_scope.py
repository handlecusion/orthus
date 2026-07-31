"""K7.1 — outbox owner-scope seam 단위 테스트 (DB 불필요).

enqueue 분기, exact-scope DELETE Cypher 형태(write-leak 경로 — read leak보다 나쁜
cross-tenant 파괴 벡터), schema-version hold를 가짜 session/tx로 고정한다. enqueue→
claim 라운드트립과 worker apply는 PG/neo4j integration에서 본다.
"""

from __future__ import annotations

import uuid

import orthus.kg.outbox as outbox_mod
from orthus.kg.outbox import KGOutboxWorker, OutboxRow, enqueue_kg_event
from orthus.kg.store import KgMeta


class _FakeSession:
    def __init__(self):
        self.stmts = []

    def execute(self, stmt):
        self.stmts.append(stmt)
        return None


def _params(stmt) -> dict:
    return stmt.compile().params


def _set_flags(monkeypatch, *, enabled: bool, owner_scope: bool):
    monkeypatch.setattr(outbox_mod, "kg_enabled", lambda: enabled)
    monkeypatch.setattr(outbox_mod, "kg_owner_scope_enabled", lambda: owner_scope)


def test_enqueue_noops_on_personal_when_flag_off(monkeypatch):
    # §1.5 보존: owner-scope off면 personal은 K7 이전과 동일하게 no-op.
    _set_flags(monkeypatch, enabled=True, owner_scope=False)
    s = _FakeSession()
    enqueue_kg_event(
        s,
        entity_kind="wiki_page",
        entity_id=uuid.uuid4(),
        scope="personal",
        owner_id=uuid.uuid4(),
    )
    assert s.stmts == []


def test_enqueue_noops_when_kg_disabled(monkeypatch):
    # `or not kg_enabled()` 절 보존 — flag on이어도 KG off면 적재 안 함.
    _set_flags(monkeypatch, enabled=False, owner_scope=True)
    s = _FakeSession()
    enqueue_kg_event(
        s,
        entity_kind="wiki_page",
        entity_id=uuid.uuid4(),
        scope="personal",
        owner_id=uuid.uuid4(),
    )
    assert s.stmts == []


def test_enqueue_personal_when_flag_on_carries_owner(monkeypatch):
    _set_flags(monkeypatch, enabled=True, owner_scope=True)
    s = _FakeSession()
    owner = uuid.uuid4()
    enqueue_kg_event(
        s, entity_kind="wiki_page", entity_id=uuid.uuid4(), scope="personal", owner_id=owner
    )
    assert len(s.stmts) == 1
    p = _params(s.stmts[0])
    assert p["scope"] == "personal"
    assert p["owner_id"] == owner


def test_enqueue_personal_without_owner_noops(monkeypatch):
    # owner 부재 personal은 적재하지 않는다(방어 — ns 경계가 흐려짐).
    _set_flags(monkeypatch, enabled=True, owner_scope=True)
    s = _FakeSession()
    enqueue_kg_event(s, entity_kind="wiki_page", entity_id=uuid.uuid4(), scope="personal")
    assert s.stmts == []


def test_enqueue_company_nulls_owner(monkeypatch):
    # company 이벤트는 owner를 넘겨도 NULL로 적재(exact-scope DELETE가 company만 보도록).
    _set_flags(monkeypatch, enabled=True, owner_scope=True)
    s = _FakeSession()
    enqueue_kg_event(
        s,
        entity_kind="document",
        entity_id=uuid.uuid4(),
        scope="company",
        owner_id=uuid.uuid4(),
    )
    assert len(s.stmts) == 1
    p = _params(s.stmts[0])
    assert p["scope"] == "company"
    assert p["owner_id"] is None


# --- exact-scope DELETE (write-leak 경로) ------------------------------------------


class _FakeResult:
    def consume(self):
        return None


class _FakeTx:
    def __init__(self):
        self.calls = []

    def run(self, cypher, **kw):
        self.calls.append((cypher, kw))
        return _FakeResult()


def _delete_row(scope: str, owner_id=None) -> OutboxRow:
    return OutboxRow(
        outbox_id=uuid.uuid4(),
        entity_kind="wiki_page",
        entity_id=uuid.uuid4(),
        op="delete",
        attempts=0,
        scope=scope,
        owner_id=owner_id,
    )


def test_delete_company_event_is_unconditional():
    # 코드리뷰 #1 — company 삭제는 유니크 키만으로 무가드 DETACH(v1 동작). scope WHERE를
    # 박으면 v1-shape(scope 속성 없는) 노드에서 `null='company'`→0 매칭으로 삭제가 조용히
    # 실패해 노드가 부활한다. 전역 유니크 키가 이미 cross-scope 오삭제를 막는다.
    worker = KGOutboxWorker(poll_seconds=0)
    tx = _FakeTx()
    worker._apply_change(tx, _delete_row("company"))
    cypher, kw = tx.calls[0]
    assert "DETACH DELETE n" in cypher
    assert "n.scope" not in cypher  # company 삭제엔 scope 가드가 없다
    assert "owner_id" not in cypher  # company 삭제는 owner를 보지 않는다


def test_delete_personal_event_exact_owner_scope():
    # B-write twin: personal delete는 그 owner 노드만 — `company OR this-owner`가 아니다.
    owner = uuid.uuid4()
    worker = KGOutboxWorker(poll_seconds=0)
    tx = _FakeTx()
    worker._apply_change(tx, _delete_row("personal", owner_id=owner))
    cypher, kw = tx.calls[0]
    assert "n.scope = 'personal'" in cypher
    assert "n.owner_id = $owner_id" in cypher
    assert kw["owner_id"] == str(owner)
    # company-scope DETACH(무가드)로 떨어지지 않는다 — 이벤트 scope에서 유도.
    assert "n.scope = 'company'" not in cypher


# --- upsert reload filter는 이벤트 scope에서 유도 (flag-flip race, 코드리뷰 #5) --------


def _upsert_row(scope: str, owner_id=None) -> OutboxRow:
    return OutboxRow(
        outbox_id=uuid.uuid4(),
        entity_kind="document",  # load_one에 slug 없이도 단순한 kind
        entity_id=uuid.uuid4(),
        op="upsert",
        attempts=0,
        scope=scope,
        owner_id=owner_id,
    )


def _capture_load_one_filter(monkeypatch):
    """`_apply_change` upsert 경로가 `load_one`에 넘기는 scope_filter를 가로챈다."""
    import orthus.kg.project as project_mod

    seen = {}

    def _fake_load_one(entity_kind, entity_id, *, scope_filter=None):
        seen["scope_filter"] = scope_filter
        return project_mod.SourceRows(full=False)

    monkeypatch.setattr(project_mod, "load_one", _fake_load_one)
    monkeypatch.setattr(project_mod, "build", lambda rows: object())
    monkeypatch.setattr(outbox_mod.kg_store, "merge_all", lambda tx, plan: None)
    monkeypatch.setattr(outbox_mod.kg_store, "replace_page_link_edges", lambda *a: None)
    return seen


def test_upsert_company_event_reloads_with_company_filter(monkeypatch):
    from orthus.kg.project import ScopeFilter

    seen = _capture_load_one_filter(monkeypatch)
    KGOutboxWorker(poll_seconds=0)._apply_change(_FakeTx(), _upsert_row("company"))
    assert seen["scope_filter"] is ScopeFilter.COMPANY


def test_upsert_personal_event_reloads_owner_inclusive_regardless_of_live_flag(monkeypatch):
    # flag-flip race(코드리뷰 #5): personal 이벤트는 live flag가 꺼져 있어도 owner-inclusive로
    # 다시 읽어야 노드가 안착한다(enqueue 시점에 이미 flag-on이었음). live flag를 off로 둔다.
    from orthus.kg.project import ScopeFilter

    monkeypatch.setattr(outbox_mod, "kg_owner_scope_enabled", lambda: False)
    seen = _capture_load_one_filter(monkeypatch)
    KGOutboxWorker(poll_seconds=0)._apply_change(
        _FakeTx(), _upsert_row("personal", owner_id=uuid.uuid4())
    )
    assert seen["scope_filter"] is ScopeFilter.COMPANY_PLUS_OWNER


# --- schema-version hold -----------------------------------------------------------


def test_schema_version_held_only_on_mismatch():
    worker = KGOutboxWorker(poll_seconds=0)
    from orthus.kg.schema import KG_SCHEMA_VERSION

    assert worker._schema_version_held(KgMeta(kg_schema_version=KG_SCHEMA_VERSION - 1)) is True
    assert worker._schema_version_held(KgMeta(kg_schema_version=KG_SCHEMA_VERSION)) is False
    # meta 부재(빈 그래프)는 hold하지 않는다 — 기존 동작 유지, rebuild가 수렴.
    assert worker._schema_version_held(None) is False
    # version IS NULL(KgMeta 있으나 rebuild 전/증분 populate 중)도 hold 안 함 —
    # v1 rebuild가 없었으니 v1/v2 혼합 없음(integration 회귀로 확인).
    assert worker._schema_version_held(KgMeta(kg_schema_version=None)) is False
