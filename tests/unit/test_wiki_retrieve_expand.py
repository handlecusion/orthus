"""넓은 질문 검색 1-hop 확장 (`ORTHUS_WIKI_RETRIEVE_EXPAND_ENABLED`) 단위 경계 테스트.

DB/Neo4j 불필요 — audit/session/KG/retrieve를 전부 격리한다(test_kg_overview 선례).
고정하는 경계: flag OFF 미진입(기존 결과 byte-identical), 확장 병합의 결정론
(페이지 dedupe·0.85 할인·slug-정렬 캡 12·최종 k 8·1차 hits 불변), KG off/scope
personal 시 KG 미호출, 예외 시 fail-open(1차 hits 유지).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

import orthus.kg.client as kg_client
import orthus.kg.gate as kg_gate
from orthus.kg.gate import KgQueryStatus, KgTemplateResult
from orthus.schemas.canonical import KgGraphNode, WikiSourceRef
from orthus.settings import Settings
from orthus.wiki import qa

USER = uuid4()


def _ref(slug: str, score: float = 0.9) -> WikiSourceRef:
    return WikiSourceRef(
        page_slug=slug, title=slug, kind="page", excerpt=f"about {slug}", score=score
    )


@contextmanager
def _noop_audit(*_a, **_k):
    """audit() 대역 — 진짜 audit은 enter에서 DB row를 쓰므로 DB-less 단위 레인에서 격리."""
    yield SimpleNamespace(add_meta=lambda **_kw: None, correlation_id=uuid4())


@contextmanager
def _noop_session():
    yield object()


def _settings(enabled: bool) -> Settings:
    return Settings(wiki_retrieve_expand_enabled=enabled)


def _boom(*_a, **_k):
    raise AssertionError("must not be called")


# --- ask() 훅 경계 --------------------------------------------------------------------


def test_flag_off_expansion_not_entered(monkeypatch):
    """flag OFF면 확장 코드에 진입조차 하지 않고 1차 hits가 그대로 grounding으로 간다."""
    primary = [_ref("a"), _ref("b", 0.7)]
    captured: dict = {}
    monkeypatch.setattr(qa, "get_settings", lambda: _settings(False))
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: list(primary))
    monkeypatch.setattr(qa, "_expand_broad_hits", _boom)
    monkeypatch.setattr(
        qa,
        "answer_from_hits",
        lambda user_id, question, hits, **kw: captured.update(hits=hits) or "sentinel",
    )
    assert qa.ask(USER, "ai 툴 관련 요약해줘") == "sentinel"
    assert captured["hits"] == primary


def test_flag_on_uses_expanded_hits(monkeypatch):
    """flag ON이면 확장 결과가 grounding hits로 전달된다."""
    primary = [_ref("a")]
    expanded = [_ref("a"), _ref("kg-x", 0.5)]
    captured: dict = {}
    monkeypatch.setattr(qa, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: list(primary))
    monkeypatch.setattr(qa, "_expand_broad_hits", lambda *a, **k: list(expanded))
    monkeypatch.setattr(
        qa,
        "answer_from_hits",
        lambda user_id, question, hits, **kw: captured.update(hits=hits) or "sentinel",
    )
    assert qa.ask(USER, "q") == "sentinel"
    assert captured["hits"] == expanded


def test_flag_on_empty_primary_skips_expansion(monkeypatch):
    """1차 hits가 비면 확장할 seed가 없다 — 미진입(no-hits 답변 경로 그대로)."""
    monkeypatch.setattr(qa, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(qa, "_expand_broad_hits", _boom)
    monkeypatch.setattr(qa, "answer_from_hits", lambda user_id, question, hits, **kw: hits)
    assert qa.ask(USER, "q") == []


def test_expansion_exception_fails_open(monkeypatch):
    """확장 블록의 어떤 예외든 1차 hits 그대로 반환한다(fail-open, 답변 미차단)."""
    primary = [_ref("a")]
    captured: dict = {}
    monkeypatch.setattr(qa, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: list(primary))

    def _raise(*_a, **_k):
        raise RuntimeError("neo4j exploded")

    monkeypatch.setattr(qa, "_expand_broad_hits", _raise)
    monkeypatch.setattr(
        qa,
        "answer_from_hits",
        lambda user_id, question, hits, **kw: captured.update(hits=hits) or "sentinel",
    )
    assert qa.ask(USER, "q") == "sentinel"
    assert captured["hits"] == primary


# --- _expand_broad_hits 병합/캡/할인 결정론 -------------------------------------------


def _patch_expand_env(
    monkeypatch,
    *,
    kg_on: bool,
    kg_nodes=None,
    provenance: set[str] | None = None,
    second_hits=None,
    second_capture: dict | None = None,
):
    monkeypatch.setattr(qa, "audit", _noop_audit)
    monkeypatch.setattr(qa, "session", _noop_session)
    monkeypatch.setattr(kg_client, "kg_enabled", lambda: kg_on)
    monkeypatch.setattr(kg_client, "kg_available", lambda: kg_on)
    if kg_on:

        def _run(**kw):
            return KgTemplateResult(status=KgQueryStatus.OK, nodes=list(kg_nodes or []))

        monkeypatch.setattr(kg_gate, "run_kg_template", _run)
    else:
        monkeypatch.setattr(kg_gate, "run_kg_template", _boom)
    monkeypatch.setattr(
        qa,
        "_expand_visible_provenance_slugs",
        lambda s, user_id, seeds, *, scope: set(seeds) | set(provenance or set()),
    )

    def _second_retrieve(user_id, question, **kw):
        if second_capture is not None:
            second_capture.update(kw)
        return list(second_hits or [])

    monkeypatch.setattr(qa, "retrieve", _second_retrieve)


def _call(hits, *, scope="all", k=5):
    return qa._expand_broad_hits(
        USER, "q", hits, k=k, scope=scope, project=None, since=None, until=None, source=None
    )


def test_merge_dedupe_discount_and_primary_preserved(monkeypatch):
    """확장 slug만 2차 retrieve로 가고, 1차와 겹치는 페이지는 dedupe, 확장 유래 hit는
    0.85 할인으로 1차 hits 뒤에 붙는다(1차 순서/점수 불변)."""
    primary = [_ref("seed-1", 0.9), _ref("seed-2", 0.7)]
    kg_nodes = [
        KgGraphNode(id="1", label="WikiPage", slug="kg-a", materialized=True),
        KgGraphNode(id="kg-ghost", label="WikiPage", slug="kg-ghost", materialized=False),
        KgGraphNode(id="2", label="Entity", slug="ent-x", materialized=True),
    ]
    cap: dict = {}
    _patch_expand_env(
        monkeypatch,
        kg_on=True,
        kg_nodes=kg_nodes,
        provenance={"prov-a"},
        second_hits=[_ref("kg-a", 0.8), _ref("seed-1", 0.99), _ref("prov-a", 0.6)],
        second_capture=cap,
    )
    out = _call(primary)
    # 확장셋: materialized WikiPage(kg-a) + provenance(prov-a)만 — placeholder/Entity/seed 제외.
    assert cap["page_slugs"] == {"kg-a", "prov-a"}
    assert cap["k"] == qa._EXPAND_FINAL_K
    # 1차 hits는 순서/점수 그대로 앞에 유지.
    assert out[:2] == primary
    # seed-1 중복은 drop, 확장 유래는 0.85 할인.
    slugs = [h.page_slug for h in out]
    assert slugs == ["seed-1", "seed-2", "kg-a", "prov-a"]
    assert out[2].score == pytest.approx(0.8 * 0.85)
    assert out[3].score == pytest.approx(0.6 * 0.85)


def test_expansion_slug_cap_is_sorted_deterministic(monkeypatch):
    """확장 후보가 캡을 넘으면 slug 정렬 후 앞 12개만 — 실행마다 같은 셋(결정론)."""
    provenance = {f"p-{i:02d}" for i in range(20)}
    cap: dict = {}
    _patch_expand_env(
        monkeypatch, kg_on=False, provenance=provenance, second_hits=[], second_capture=cap
    )
    _call([_ref("seed-1")])
    assert cap["page_slugs"] == set(sorted(provenance)[: qa._EXPAND_SLUG_CAP])
    assert len(cap["page_slugs"]) == 12


def test_final_k_cap_eight(monkeypatch):
    """병합 결과는 최종 8개로 절단 — 1차 5개가 항상 살아남고 확장은 남은 슬롯만 채운다."""
    primary = [_ref(f"seed-{i}", 0.9 - i * 0.01) for i in range(5)]
    second = [_ref(f"x-{i}", 0.8 - i * 0.01) for i in range(8)]
    _patch_expand_env(
        monkeypatch, kg_on=False, provenance={f"x-{i}" for i in range(8)}, second_hits=second
    )
    out = _call(primary)
    assert len(out) == qa._EXPAND_FINAL_K == 8
    assert [h.page_slug for h in out[:5]] == [h.page_slug for h in primary]
    assert [h.page_slug for h in out[5:]] == ["x-0", "x-1", "x-2"]


def test_personal_scope_never_touches_kg(monkeypatch):
    """scope=personal은 KG(company-only 데이터)를 아예 조회하지 않는다 — provenance만."""
    cap: dict = {}
    _patch_expand_env(
        monkeypatch, kg_on=False, provenance={"prov-p"}, second_hits=[], second_capture=cap
    )
    # kg_enabled 자체가 호출되면 안 된다(scope 가드가 먼저).
    monkeypatch.setattr(kg_client, "kg_enabled", _boom)
    monkeypatch.setattr(kg_client, "kg_available", _boom)
    out = _call([_ref("seed-1")], scope="personal")
    assert cap["page_slugs"] == {"prov-p"}
    assert out == [_ref("seed-1")]  # 2차 empty → added 없음 → 1차 그대로


def test_no_candidates_returns_primary_without_second_retrieve(monkeypatch):
    """확장 후보가 없으면 2차 retrieve 없이 1차 hits 그대로."""
    primary = [_ref("seed-1")]
    _patch_expand_env(monkeypatch, kg_on=False, provenance=set(), second_hits=[])
    monkeypatch.setattr(qa, "retrieve", _boom)  # 2차 검색 미발화 고정
    assert _call(primary) == primary


def test_kg_reject_is_skipped_silently(monkeypatch):
    """per-seed 게이트 reject(예: personal seed → slug_not_found)는 조용히 skip — 나머지
    확장(provenance)은 계속 진행된다."""
    cap: dict = {}
    _patch_expand_env(
        monkeypatch,
        kg_on=True,
        kg_nodes=[],
        provenance={"prov-a"},
        second_hits=[],
        second_capture=cap,
    )
    monkeypatch.setattr(
        kg_gate,
        "run_kg_template",
        lambda **kw: KgTemplateResult(
            status=KgQueryStatus.REJECTED, reject_reason="slug_not_found:seed-1"
        ),
    )
    _call([_ref("seed-1")])
    assert cap["page_slugs"] == {"prov-a"}


def test_flag_default_off():
    """flag는 fail-closed default — Settings() 기본값이 OFF다."""
    assert Settings().wiki_retrieve_expand_enabled is False
