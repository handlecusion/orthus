"""K7.2 Step 3 — _map_records owner-scope 파생 + wire tripwire (neo4j 불필요).

driver record를 duck-typed fake로 대체해 _map_records의 scope/is_own_personal 파생,
B1 wire tripwire(foreign personal drop), scope-aware dedup, 엣지 scope/owner_id 처리를
고정한다. 실 그래프 경계 증명은 Step 7 boundary matrix(neo4j-test)가 별도로 한다.
"""

from __future__ import annotations

import uuid

from orthus.kg.gate import KgQueryStatus, _map_records, _scope_of, run_kg_template
from orthus.kg.visibility import personal_slug_log_value
from orthus.settings import get_settings


class _FakeNode:
    def __init__(self, element_id: str, label: str, props: dict):
        self.element_id = element_id
        self.labels = [label]
        self._props = props

    def keys(self):
        return self._props.keys()

    def __getitem__(self, k):
        return self._props[k]


class _FakeRel:
    def __init__(self, element_id, rtype, start, end, props):
        self.element_id = element_id
        self.type = rtype
        self.start_node = start
        self.end_node = end
        self._props = props

    def keys(self):
        return self._props.keys()

    def __getitem__(self, k):
        return self._props[k]


class _Rec:
    def __init__(self, *vals):
        self._vals = vals

    def values(self):
        return list(self._vals)


def _wikipage(eid, *, page_id, slug, scope="company", owner_id=None, title="t"):
    props = {"page_id": page_id, "slug": slug, "title": title, "scope": scope}
    if owner_id is not None:
        props["owner_id"] = owner_id
    return _FakeNode(eid, "WikiPage", props)


# --- _scope_of (pure, single source for nodes AND edges) -----------------------


def test_scope_of_company_default_when_absent():
    assert _scope_of({"page_id": "p"}, "caller") == ("company", False)


def test_scope_of_malformed_owner_without_scope():
    # owner_id 있는데 scope 없음 → (None, False) = malformed (호출자가 drop). 노드·엣지 공통.
    assert _scope_of({"page_id": "p", "owner_id": "x"}, "x") == (None, False)
    assert _scope_of({"owner_id": str(uuid.uuid4())}, str(uuid.uuid4())) == (None, False)
    # caller 자신의 owner_id여도 scope가 없으면 신뢰 불가 → 여전히 malformed.
    caller = str(uuid.uuid4())
    assert _scope_of({"owner_id": caller}, caller) == (None, False)


def test_scope_of_own_vs_foreign_personal():
    caller = str(uuid.uuid4())
    other = str(uuid.uuid4())
    assert _scope_of({"scope": "personal", "owner_id": caller}, caller) == ("personal", True)
    assert _scope_of({"scope": "personal", "owner_id": other}, caller) == ("personal", False)
    # null caller never owns a personal node.
    assert _scope_of({"scope": "personal", "owner_id": other}, None) == ("personal", False)
    # un-owned personal (owner_id 부재) → not own.
    assert _scope_of({"scope": "personal"}, caller) == ("personal", False)
    # company 기본/명시.
    assert _scope_of({}, caller) == ("company", False)


# --- _map_records tripwire / scope / dedup -------------------------------------


def test_map_records_keeps_company_and_own_personal():
    caller = uuid.uuid4()
    nodes, edges, dropped = _map_records(
        [
            _Rec(_wikipage("e1", page_id="pc", slug="co", scope="company")),
            _Rec(
                _wikipage("e2", page_id="pp", slug="mine", scope="personal", owner_id=str(caller))
            ),
        ],
        caller,
    )
    by_id = {n.id: n for n in nodes}
    assert by_id["pc"].scope == "company" and by_id["pc"].is_own_personal is False
    assert by_id["pp"].scope == "personal" and by_id["pp"].is_own_personal is True
    assert dropped["boundary_tripwire"] == 0


def test_map_records_drops_foreign_personal_node_tripwire():
    caller = uuid.uuid4()
    other = uuid.uuid4()
    nodes, edges, dropped = _map_records(
        [_Rec(_wikipage("e1", page_id="pf", slug="theirs", scope="personal", owner_id=str(other)))],
        caller,
    )
    assert nodes == []
    assert dropped["boundary_tripwire"] == 1


def test_map_records_drops_malformed_owner_without_scope():
    caller = uuid.uuid4()
    node = _FakeNode("e1", "WikiPage", {"page_id": "pm", "slug": "m", "owner_id": str(caller)})
    nodes, _edges, dropped = _map_records([_Rec(node)], caller)
    assert nodes == []
    assert dropped["boundary_tripwire"] == 1


def test_map_records_scope_aware_dedup_same_slug_diff_scope():
    # 같은 slug(id)·다른 scope placeholder는 (label, scope, id)로 분리 — 합쳐지지 않음.
    caller = uuid.uuid4()
    company_ph = _FakeNode(
        "e1", "WikiPage", {"slug": "dup", "scope": "company", "materialized": False}
    )
    personal_ph = _FakeNode(
        "e2",
        "WikiPage",
        {"slug": "dup", "scope": "personal", "owner_id": str(caller), "materialized": False},
    )
    nodes, _edges, dropped = _map_records([_Rec(company_ph), _Rec(personal_ph)], caller)
    scopes = sorted(n.scope for n in nodes if n.id == "dup")
    assert scopes == ["company", "personal"]  # 둘 다 보존
    assert dropped["boundary_tripwire"] == 0


def test_map_records_edge_scope_set_and_owner_id_stripped():
    caller = uuid.uuid4()
    a = _wikipage("e1", page_id="a", slug="a", scope="company")
    b = _wikipage("e2", page_id="b", slug="b", scope="company")
    rel = _FakeRel(
        "r1", "BACKLINK", a, b, {"scope": "personal", "owner_id": str(caller), "note": "keep"}
    )
    _nodes, edges, dropped = _map_records([_Rec(a), _Rec(b), _Rec(rel)], caller)
    assert len(edges) == 1
    e = edges[0]
    assert e.scope == "personal"
    assert "owner_id" not in e.properties and "scope" not in e.properties
    assert e.properties.get("note") == "keep"  # 비-경계 prop은 보존
    assert dropped["boundary_tripwire"] == 0


def test_map_records_drops_foreign_personal_edge():
    caller = uuid.uuid4()
    other = uuid.uuid4()
    a = _wikipage("e1", page_id="a", slug="a", scope="company")
    b = _wikipage("e2", page_id="b", slug="b", scope="company")
    rel = _FakeRel("r1", "BACKLINK", a, b, {"scope": "personal", "owner_id": str(other)})
    _nodes, edges, dropped = _map_records([_Rec(a), _Rec(b), _Rec(rel)], caller)
    assert edges == []
    assert dropped["boundary_tripwire"] == 1


def test_map_records_drops_malformed_owner_edge_without_scope():
    # owner_id 있는데 scope 없는 엣지(malformed) → drop + tripwire. 양 끝점은 company라
    # 노드 drop으로 인한 제외가 아니라 **엣지 자체의 malformed 가드**가 잡는 것을 본다
    # (코드리뷰 — 엣지 tripwire가 노드와 대칭).
    caller = uuid.uuid4()
    other = uuid.uuid4()
    a = _wikipage("e1", page_id="a", slug="a", scope="company")
    b = _wikipage("e2", page_id="b", slug="b", scope="company")
    rel = _FakeRel("r1", "BACKLINK", a, b, {"owner_id": str(other)})  # scope 누락
    _nodes, edges, dropped = _map_records([_Rec(a), _Rec(b), _Rec(rel)], caller)
    assert edges == []
    assert dropped["boundary_tripwire"] == 1


def test_map_records_edge_incident_to_dropped_node_excluded():
    caller = uuid.uuid4()
    other = uuid.uuid4()
    a = _wikipage("e1", page_id="a", slug="a", scope="company")
    foreign = _wikipage("e2", page_id="pf", slug="x", scope="personal", owner_id=str(other))
    rel = _FakeRel("r1", "BACKLINK", a, foreign, {"scope": "company"})
    _nodes, edges, dropped = _map_records([_Rec(a), _Rec(foreign), _Rec(rel)], caller)
    assert edges == []  # foreign 노드 drop → 인접 엣지도 제외
    assert dropped["boundary_tripwire"] >= 1


def test_map_records_edge_dedup_is_scope_aware():
    # 코드리뷰 #4 — 같은 endpoint-id·rel의 company 엣지와 personal 엣지는 (src,rel,dst,scope)로
    # 분리 보존(노드 dedup `(label,scope,id)`와 대칭). scope 없는 키였다면 한쪽이 사라지고
    # 그 scope 구분이 손실됐다.
    caller = uuid.uuid4()
    a = _wikipage("e1", page_id="a", slug="a", scope="company")
    # 같은 slug 'dup'의 company placeholder + caller 자기 personal placeholder (둘 다 id='dup').
    dup_co = _FakeNode("e2", "WikiPage", {"slug": "dup", "scope": "company", "materialized": False})
    dup_pp = _FakeNode(
        "e3",
        "WikiPage",
        {"slug": "dup", "scope": "personal", "owner_id": str(caller), "materialized": False},
    )
    e_co = _FakeRel("r1", "BACKLINK", a, dup_co, {"scope": "company"})
    e_pp = _FakeRel("r2", "BACKLINK", a, dup_pp, {"scope": "personal", "owner_id": str(caller)})
    _nodes, edges, dropped = _map_records(
        [_Rec(a), _Rec(dup_co), _Rec(dup_pp), _Rec(e_co), _Rec(e_pp)], caller
    )
    # 두 엣지 모두 a→dup BACKLINK지만 scope가 달라 둘 다 보존된다.
    dup_edges = sorted(e.scope for e in edges if e.dst == "dup" and e.rel == "BACKLINK")
    assert dup_edges == ["company", "personal"]
    assert dropped["boundary_tripwire"] == 0


def test_company_framing_template_rejected_when_owner_scope_off(clean, monkeypatch):
    # 코드리뷰 #2 — framing A(path_between_company)는 owner-scope 전용이라 flag-OFF에서
    # 부르면 unbound page_id로 driver_error가 됐다. 이제 깔끔히 owner_scope_required reject.
    s = get_settings()
    monkeypatch.setattr(s, "kg_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_owner_scope_enabled", False, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", False, raising=False)
    r = run_kg_template(
        user_id=None,
        template_name="path_between_company",
        params={"slug_a": "a", "slug_b": "b"},
    )
    assert r.status is KgQueryStatus.REJECTED
    assert r.reject_reason == "owner_scope_required"


def test_personal_slug_log_value_stable_prefixed_and_salted():
    a = uuid.uuid4()
    b = uuid.uuid4()
    h1 = personal_slug_log_value("김대표-연봉협상", a)
    h2 = personal_slug_log_value("김대표-연봉협상", a)
    assert h1 == h2 and h1.startswith("personal:")  # 같은 owner+slug → 안정(진단 상관 보존)
    assert "김대표" not in h1  # raw 제목이 남지 않음
    assert personal_slug_log_value("other", a) != h1  # 다른 slug → 다른 hash
    # caller salt(코드리뷰): 다른 owner가 같은 제목을 질의해도 hash가 달라 cross-owner
    # 동일성 oracle이 차단된다.
    assert personal_slug_log_value("김대표-연봉협상", b) != h1
