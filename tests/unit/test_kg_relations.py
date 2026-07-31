"""Slice 1 — KG 관계 read 툴(orthus-mcp `kg_relations`/`entity_relations` + in-process
Solar engine). 라이브 Neo4j/Bedrock 없이 계약 핵심 seam을 고정한다:

- `relation` enum → typed 템플릿 매핑(neighbors/page_conflicts/entity_neighbors/
  path_between/entity_mentions)이 드리프트하지 않는다,
- 잘못된 입력(slug/slug_b/name 누락, 미지원 relation)은 게이트를 타지 않고 reject,
- depth/max_hops 는 템플릿 상한에 맞게 clamp되어 게이트로 넘어간다,
- `supported_from` 은 KG 미가용(hard)과 입력 reject(soft)를 구분한다,
- KG 툴은 bedrock 엔진 + KG 가용일 때만 advertise된다(cli 는 orthus-mcp 경유).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import orthus.kg.relations as relations
import orthus.router.agentic.loop as agentic_loop
import orthus.router.agentic.tools as agentic_tools
from orthus.kg.gate import KgQueryStatus, KgTemplateResult
from orthus.schemas.canonical import KgGraphEdge, KgGraphNode

_USER = UUID("cbd68a41-cdb7-4fd7-8ed1-605f7ecff93f")


class _RecordingAgentChat:
    def __init__(self, script):
        self._script = script
        self.tools_seen: list[dict] = []
        self.system_seen = ""

    def run_tool_loop(self, *, system, question, tools, dispatch, on_event=None, max_turns=6):
        self.tools_seen = list(tools)
        self.system_seen = system
        return self._script(dispatch)


def _loop_settings():
    return SimpleNamespace(
        node_id="company",
        node_kind="company",
        mail_send_enabled=False,
    )


# --- relation → template mapping -----------------------------------------------


def test_relation_maps_to_expected_templates(monkeypatch):
    seen: list[tuple[str, dict]] = []

    def _capture(*, user_id, template_name, params):
        seen.append((template_name, params))
        return KgTemplateResult(status=KgQueryStatus.OK)

    monkeypatch.setattr(relations, "run_kg_template", _capture)

    relations.run_kg_relation(user_id=_USER, relation="neighbors", slug="a")
    relations.run_kg_relation(user_id=_USER, relation="conflicts", slug="a")
    relations.run_kg_relation(user_id=_USER, relation="related", slug="a")
    relations.run_kg_relation(user_id=_USER, relation="path", slug="a", slug_b="b")

    templates = [t for t, _ in seen]
    assert templates == ["neighbors", "page_conflicts", "entity_neighbors", "path_between"]
    # path 은 slug→slug_a 로 매핑된다.
    assert seen[-1][1] == {"slug_a": "a", "slug_b": "b", "max_hops": 4}


def test_mentions_normalizes_name(monkeypatch):
    seen: dict = {}

    def _capture(*, user_id, template_name, params):
        seen.update(template_name=template_name, params=params)
        return KgTemplateResult(status=KgQueryStatus.OK)

    monkeypatch.setattr(relations, "run_kg_template", _capture)
    relations.run_kg_relation(user_id=_USER, relation="mentions", name="  Yunseo HYUN ")
    assert seen["template_name"] == "entity_mentions"
    # name_norm 은 정규화(casefold/strip)된 단일 키로 전달된다.
    assert set(seen["params"]) == {"name_norm"}
    assert seen["params"]["name_norm"] == seen["params"]["name_norm"].strip().lower()


def test_depth_and_hops_clamped_to_template_bounds(monkeypatch):
    seen: list[dict] = []

    def _capture(*, user_id, template_name, params):
        seen.append(params)
        return KgTemplateResult(status=KgQueryStatus.OK)

    monkeypatch.setattr(relations, "run_kg_template", _capture)
    relations.run_kg_relation(user_id=_USER, relation="neighbors", slug="a", depth=9)
    relations.run_kg_relation(user_id=_USER, relation="path", slug="a", slug_b="b", max_hops=99)
    relations.run_kg_relation(user_id=_USER, relation="path", slug="a", slug_b="b", max_hops=1)
    assert seen[0]["depth"] == 2  # depth ∈ {1,2}
    assert seen[1]["max_hops"] == 4  # max_hops ∈ {2,3,4}
    assert seen[2]["max_hops"] == 2


# --- input rejects (게이트 미진입) ---------------------------------------------


def test_bad_input_rejects_without_hitting_gate(monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - 입력 reject 는 게이트를 타면 안 됨
        raise AssertionError("bad input must not reach run_kg_template")

    monkeypatch.setattr(relations, "run_kg_template", _boom)

    assert relations.run_kg_relation(user_id=_USER, relation="bogus", slug="a").reject_reason == (
        "unknown_relation"
    )
    assert relations.run_kg_relation(user_id=_USER, relation="neighbors").reject_reason == (
        "missing_slug"
    )
    assert (
        relations.run_kg_relation(user_id=_USER, relation="path", slug="a").reject_reason
        == "missing_slug_b"
    )
    assert (
        relations.run_kg_relation(user_id=_USER, relation="mentions", name="   ").reject_reason
        == "missing_name"
    )


# --- supported_from: hard(미가용) vs soft(입력) ---------------------------------


def test_supported_from_distinguishes_unavailable_from_input_reject():
    def rej(reason):
        return KgTemplateResult(status=KgQueryStatus.REJECTED, reject_reason=reason)

    assert relations.supported_from(KgTemplateResult(status=KgQueryStatus.OK)) is True
    assert relations.supported_from(rej("slug_not_found:x")) is True
    assert relations.supported_from(rej("missing_slug")) is True
    assert relations.supported_from(rej("kg_disabled")) is False
    assert relations.supported_from(rej("kg_unavailable")) is False
    assert relations.supported_from(rej("driver_error:ServiceUnavailable")) is False
    assert (
        relations.supported_from(
            KgTemplateResult(status=KgQueryStatus.TIMEOUT, reject_reason="timeout")
        )
        is False
    )


# --- model-facing renderer -----------------------------------------------------


def test_renderer_formats_nodes_and_edges():
    result = KgTemplateResult(
        status=KgQueryStatus.OK,
        nodes=[
            KgGraphNode(id="p1", label="WikiPage", slug="a", title="A 페이지"),
            KgGraphNode(id="p2", label="WikiPage", slug="b", title="B 페이지"),
        ],
        edges=[KgGraphEdge(src="p1", dst="p2", rel="BACKLINK")],
    )
    rendered = agentic_tools.kg_relation_result_for_model(result)
    assert "A 페이지" in rendered and "B 페이지" in rendered
    assert "BACKLINK" in rendered


def test_renderer_reports_reject_reason():
    rejected = KgTemplateResult(status=KgQueryStatus.REJECTED, reject_reason="slug_not_found:x")
    assert "slug_not_found:x" in agentic_tools.kg_relation_result_for_model(rejected)


# --- loop wiring ---------------------------------------------------------------


def test_loop_advertises_kg_tools_on_bedrock_when_kg_available(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    monkeypatch.setattr("orthus.kg.client.kg_enabled", lambda: True)
    monkeypatch.setattr("orthus.kg.client.kg_available", lambda: True)
    chat = _RecordingAgentChat(lambda dispatch: "done")
    agentic_loop.run_agentic_answer(_USER, "A 와 B 관계 알려줘", agent_chat=chat)
    names = {t["name"] for t in chat.tools_seen}
    assert {"kg_relations", "entity_relations"} <= names
    assert "kg_relations" in chat.system_seen


def test_loop_hides_kg_tools_when_kg_unavailable(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    monkeypatch.setattr("orthus.kg.client.kg_enabled", lambda: True)
    monkeypatch.setattr("orthus.kg.client.kg_available", lambda: False)
    chat = _RecordingAgentChat(lambda dispatch: "done")
    agentic_loop.run_agentic_answer(_USER, "아무거나", agent_chat=chat)
    names = {t["name"] for t in chat.tools_seen}
    assert "kg_relations" not in names and "entity_relations" not in names


def test_loop_dispatch_routes_kg_relations(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    monkeypatch.setattr("orthus.kg.client.kg_enabled", lambda: True)
    monkeypatch.setattr("orthus.kg.client.kg_available", lambda: True)
    captured: dict = {}

    def _fake(user_id, raw_input):
        captured.update(user_id=user_id, raw_input=raw_input)
        return KgTemplateResult(status=KgQueryStatus.OK)

    monkeypatch.setattr(agentic_tools, "call_kg_relations", _fake)

    def script(dispatch):
        dispatch("kg_relations", {"slug": "a", "relation": "neighbors"})
        return "확인했습니다."

    agentic_loop.run_agentic_answer(_USER, "A 주변 관계", agent_chat=_RecordingAgentChat(script))
    assert captured["user_id"] == _USER
    assert captured["raw_input"]["slug"] == "a"
