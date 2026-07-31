"""Tier i 진행 버블 phase-frame 발행 회귀 테스트.

Orchestrate live SSE는 legacy 지식 경로에서 결정론 시점에
``{"type": "phase", ...}`` 프레임(searching → grounded → answering)을 받는다.
발행은 feedback-only다: stream_id 없으면 콜백 자체가 없고(None), 발행 실패는
어떤 경우에도 답변에 영향을 주면 안 된다(orthus/agentwork/stream.py 계약).

DB 무의존(pure) — audit 기록은 no-op으로 패치하고 retrieve는 스텁한다.
"""

from __future__ import annotations

from uuid import uuid4

import orthus.audit.logger as audit_logger
from orthus.router import _phase_publisher
from orthus.schemas.canonical import WikiSourceRef
from orthus.wiki import qa


def _hit(slug: str = "company/page-a") -> WikiSourceRef:
    return WikiSourceRef(page_slug=slug, title="제목", kind="page", excerpt="내용", score=0.9)


class _StubChat:
    model_id = "stub"

    def complete(self, system: str, user: str) -> str:  # noqa: ARG002 — 시그니처 계약
        return "그라운딩된 답"


def _mute_audit(monkeypatch):
    monkeypatch.setattr(audit_logger, "_write", lambda *a, **k: None)


def test_phase_publisher_is_none_without_stream_id():
    # /ask 검색 전용 경로(stream_id=None)는 콜백조차 만들지 않는다 — 무프레임 불변.
    assert _phase_publisher(uuid4(), None) is None


def test_phase_publisher_publishes_typed_frame(monkeypatch):
    from orthus.agentwork import stream as agent_stream

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        agent_stream, "publish_threadsafe", lambda key, frame: published.append((key, frame))
    )
    uid = uuid4()
    publish = _phase_publisher(uid, "sid-1")
    assert publish is not None
    publish("grounded", evidence_count=3)
    assert published == [
        (f"{uid}:sid-1", {"type": "phase", "stage": "grounded", "evidence_count": 3})
    ]


def test_phase_publisher_swallows_publish_failure(monkeypatch):
    from orthus.agentwork import stream as agent_stream

    def _boom(key, frame):  # noqa: ARG001
        raise RuntimeError("stream down")

    monkeypatch.setattr(agent_stream, "publish_threadsafe", _boom)
    publish = _phase_publisher(uuid4(), "sid-1")
    assert publish is not None
    publish("searching")  # must not raise — feedback-only


def test_qa_ask_emits_phases_in_order(monkeypatch):
    _mute_audit(monkeypatch)
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: [_hit(), _hit("company/page-b")])
    stages: list[tuple[str, dict]] = []
    out = qa.ask(
        uuid4(),
        "회사 위키 질문",
        chat_model=_StubChat(),
        learn=False,
        record_gaps=False,
        on_phase=lambda stage, **extra: stages.append((stage, extra)),
    )
    assert out.answer == "그라운딩된 답"
    assert stages == [
        ("searching", {}),
        ("grounded", {"evidence_count": 2}),
        ("answering", {}),
    ]


def test_qa_ask_no_hits_skips_answering(monkeypatch):
    # 근거 0개면 LLM 호출이 없으므로 answering 단계도 없다.
    _mute_audit(monkeypatch)
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: [])
    stages: list[tuple[str, dict]] = []
    out = qa.ask(
        uuid4(),
        "근거 없는 질문",
        chat_model=_StubChat(),
        learn=False,
        record_gaps=False,
        on_phase=lambda stage, **extra: stages.append((stage, extra)),
    )
    assert "근거 없음" in out.answer  # no-hits sentinel 유지
    assert stages == [("searching", {}), ("grounded", {"evidence_count": 0})]


def test_qa_ask_without_callback_unchanged(monkeypatch):
    # on_phase 미전달(기존 모든 콜사이트) — 시그니처 추가만으로 결과 불변.
    _mute_audit(monkeypatch)
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: [_hit()])
    out = qa.ask(uuid4(), "질문", chat_model=_StubChat(), learn=False, record_gaps=False)
    assert out.answer == "그라운딩된 답"


def test_qa_ask_broken_callback_never_breaks_answer(monkeypatch):
    _mute_audit(monkeypatch)
    monkeypatch.setattr(qa, "retrieve", lambda *a, **k: [_hit()])

    def _broken(stage, **extra):  # noqa: ARG001
        raise ValueError("bad callback")

    out = qa.ask(
        uuid4(),
        "질문",
        chat_model=_StubChat(),
        learn=False,
        record_gaps=False,
        on_phase=_broken,
    )
    assert out.answer == "그라운딩된 답"
