"""Slice B: multi-turn chat memory as a NON-authoritative wiki-branch preamble.

Grounding for the CURRENT question stays compiled-wiki-page-only (AGENTS.md §7).
History is reconstructed server-side and only ever reaches the wiki `ask`/
`answer_from_hits` prompt — never `retrieve()`, never `classify()`, never the
structured branch, never T2 learning (`author_from_qa`).
"""

from __future__ import annotations

import uuid

from orthus.models.adapters.mock import MockChat
from orthus.router import answer
from orthus.schemas.canonical import ChatTurn, WikiSourceRef
from orthus.wiki import qa as qa_module
from orthus.wiki.qa import _build_user_prompt, answer_from_hits, ask


def _hits() -> list[WikiSourceRef]:
    return [
        WikiSourceRef(page_slug="p1", title="T1", kind="page", excerpt="alpha", score=0.9),
        WikiSourceRef(page_slug="p2", title="T2", kind="claim", excerpt="beta", score=0.5),
    ]


def _old_prompt(question: str, hits: list[WikiSourceRef]) -> str:
    """The pre-Slice-B prompt form, reconstructed for byte-identity assertions."""
    joined = "\n\n".join(f"[{i + 1}] ({h.page_slug}) {h.excerpt}" for i, h in enumerate(hits))
    return f"Context passages:\n{joined}\n\nQuestion: {question}\n\nGrounded answer:"


def test_build_user_prompt_history_none_is_byte_identical():
    hits = _hits()
    assert _build_user_prompt("q", hits) == _old_prompt("q", hits)
    assert _build_user_prompt("q", hits, None) == _old_prompt("q", hits)
    assert _build_user_prompt("q", hits, []) == _old_prompt("q", hits)


def test_build_user_prompt_history_preamble_above_context():
    hits = _hits()
    history = [ChatTurn(role="user", text="첫 질문"), ChatTurn(role="agent", text="첫 답변")]
    prompt = _build_user_prompt("두번째", hits, history)
    # Exact preamble shape.
    assert prompt.startswith("이전 대화 (참고용 — 사실의 출처가 아님):\n")
    assert "user: 첫 질문\nagent: 첫 답변\n\n" in prompt
    # The preamble sits ABOVE the grounding context.
    assert prompt.index("이전 대화") < prompt.index("Context passages:")
    # The rest of the prompt is unchanged below the preamble.
    assert prompt.endswith(_old_prompt("두번째", hits))


def test_system_prompt_forbids_answering_from_history():
    system = qa_module._SYSTEM
    assert "ONLY the provided" in system  # original grounding restriction kept
    # The added sentence restricts history to reference resolution only.
    assert "resolve references" in system or "references/pronouns" in system
    assert "never" in system and "prior conversation" in system


def test_answer_from_hits_history_reaches_prompt_only():
    """answer_from_hits with history puts the preamble in the chat prompt, and the
    learning side-effect (author_from_qa) never sees history."""
    captured: dict[str, str] = {}

    class _SpyChat:
        model_id = "spy"

        def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
            captured["system"] = system
            captured["user"] = user
            return "근거 기반 답변."

    uid = uuid.uuid4()
    hits = _hits()
    history = [ChatTurn(role="user", text="이전 질문"), ChatTurn(role="agent", text="이전 답변")]
    # learn=False/record_gaps=False keep the test offline (no DB writes / no T2 path).
    result = answer_from_hits(
        uid,
        "지금 질문",
        hits,
        chat_model=_SpyChat(),
        learn=False,
        record_gaps=False,
        history=history,
    )
    assert result.answer == "근거 기반 답변."
    # Sources/wiki_links derive ONLY from hits (history does not add citations).
    assert [s.page_slug for s in result.sources] == ["p1", "p2"]
    assert {link.slug for link in result.wiki_links} == {"p1", "p2"}
    # The preamble is present in the prompt the model saw.
    assert "이전 대화 (참고용 — 사실의 출처가 아님):" in captured["user"]
    assert "user: 이전 질문" in captured["user"]


def test_ask_passes_current_question_only_to_retrieve(monkeypatch):
    """ask() must call retrieve() with ONLY the current question — history never
    reaches retrieval. The history is forwarded to answer_from_hits unchanged."""
    retrieve_calls: list[dict] = []
    afh_calls: list[dict] = []

    def _fake_retrieve(user_id, question, **kwargs):
        retrieve_calls.append({"question": question, "kwargs": kwargs})
        return _hits()

    def _fake_afh(user_id, question, hits, **kwargs):
        afh_calls.append({"question": question, "history": kwargs.get("history")})
        from orthus.schemas.canonical import WikiAnswer

        return WikiAnswer(question=question, answer="ok", sources=hits)

    monkeypatch.setattr(qa_module, "retrieve", _fake_retrieve)
    monkeypatch.setattr(qa_module, "answer_from_hits", _fake_afh)

    uid = uuid.uuid4()
    history = [ChatTurn(role="user", text="과거 발화")]
    ask(uid, "현재 질문", chat_model=MockChat(default="x"), history=history)

    assert len(retrieve_calls) == 1
    # The retrieved question is the current question, with no history text mixed in.
    assert retrieve_calls[0]["question"] == "현재 질문"
    assert "과거 발화" not in retrieve_calls[0]["question"]
    # answer_from_hits got the history forwarded.
    assert afh_calls[0]["history"] == history


def test_router_does_not_pass_history_to_structured(user_id):
    """A structured-routed question never threads history into the structured branch
    (history is a wiki-only concept). The structured backend has no history kwarg, so
    passing history into a structured route must still succeed."""
    for status in ["진행중", "완료"]:
        from sqlalchemy import insert

        from orthus.db import session
        from orthus.tables import notion_rows

        with session() as s:
            s.execute(
                insert(notion_rows).values(
                    row_id=uuid.uuid4(),
                    db_id="db-티켓",
                    db_name="티켓",
                    properties={"상태": status},
                    scope="company",
                    user_id=user_id,
                )
            )
            s.commit()
    chat = MockChat(
        rules=[
            ("query router", '{"route": "structured"}'),
            ("티켓", '{"sql": "SELECT 상태, count(*) FROM notion_rows GROUP BY 상태"}'),
        ],
        default='{"route": "structured"}',
    )
    history = [ChatTurn(role="user", text="아까 그거")]
    result = answer(
        user_id,
        "협업업무표 상태별 건수",
        chat_model=chat,
        history=history,
    )
    assert result.mode == "structured"
    assert result.structured is not None


def test_router_threads_history_into_wiki_branch(user_id, monkeypatch):
    """A wiki-routed question forwards history to the wiki ask() call."""
    seen: dict[str, object] = {}

    import orthus.router as router_module

    real_ask = router_module.ask

    def _spy_ask(user_id, question, **kwargs):
        seen["history"] = kwargs.get("history")
        return real_ask(user_id, question, **kwargs)

    monkeypatch.setattr(router_module, "ask", _spy_ask)
    history = [ChatTurn(role="user", text="이전 맥락")]
    result = answer(
        user_id,
        "온보딩 절차는 어떻게 진행되나요?",
        chat_model=MockChat(default="근거에 기반한 답변."),
        history=history,
    )
    assert result.mode == "wiki"
    assert seen["history"] == history
