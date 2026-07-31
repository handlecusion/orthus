"""S3 수용 게이트 — split 명령 인지화 + keyword_only 불변식14 봉인 + fall-through 보강.

DB/LLM 없이: split 프롬프트 분기·classify_intent keyword_only·fall-through 큐잉 헬퍼를 고정.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import orthus.router.decompose as d
from orthus.router.route import RouteIntent, classify_intent
from orthus.router.tools import classify_subpart

_USER = uuid4()


@contextmanager
def _noop_audit(_name, **_kw):
    span = MagicMock()
    span.add_meta = MagicMock()
    yield span


class _CapChat:
    """chat model that records the system prompt it was called with."""

    def __init__(self, response="[]"):
        self.response = response
        self.system = None

    def complete(self, system, user, *, json_only=False):
        self.system = system
        return self.response


# ── A: split 프롬프트 명령 인지화 (False=byte-identical) ──────────────────────


class TestSplitPromptCommandAware:
    def test_plain_split_uses_original_template(self):
        cap = _CapChat("[]")
        d.split_question("q", k=5, chat_model=cap, command_split=False)
        assert cap.system == d._SPLIT_SYSTEM_TMPL.format(k=5)

    def test_command_split_uses_command_template(self):
        cap = _CapChat("[]")
        d.split_question("q", k=5, chat_model=cap, command_split=True)
        assert cap.system == d._SPLIT_SYSTEM_CMD_TMPL.format(k=5)
        assert cap.system != d._SPLIT_SYSTEM_TMPL.format(k=5)

    def test_deps_plain_vs_command_template(self):
        cap0 = _CapChat("[]")
        d.split_question_with_deps("q", k=5, chat_model=cap0, command_split=False)
        assert cap0.system == d._SPLIT_DEPS_SYSTEM_TMPL.format(k=5)
        cap1 = _CapChat("[]")
        d.split_question_with_deps("q", k=5, chat_model=cap1, command_split=True)
        assert cap1.system == d._SPLIT_DEPS_SYSTEM_CMD_TMPL.format(k=5)

    def test_command_template_preserves_json_schema(self):
        # 스키마 키는 그대로 sub_questions여야 파서가 안 깨진다.
        cap = _CapChat(json.dumps({"sub_questions": ["a@b.com로 메일 보내줘", "매출 알려줘"]}))
        out = d.split_question("q", k=5, chat_model=cap, command_split=True)
        assert out == ["a@b.com로 메일 보내줘", "매출 알려줘"]


# ── B: keyword_only 불변식14 봉인 ─────────────────────────────────────────────


def _intent_via_llm_enum(enum_label, *, keyword_only):
    """detect=None + rule=None으로 강제 → LLM enum 경로만 태운다."""
    chat = _CapChat(json.dumps({"intent": enum_label}))
    with (
        patch("orthus.agentwork.service.detect_assistant_command_action", return_value=None),
        patch("orthus.router.route._rule_based_route", return_value=None),
        patch("orthus.router.route.audit", side_effect=_noop_audit),
    ):
        return classify_intent("q", allow_commands=True, keyword_only=keyword_only, chat_model=chat)


class TestKeywordOnlySeal:
    def test_llm_enum_command_demoted_when_keyword_only(self):
        # keyword_only=True → LLM-enum이 뱉은 command family를 READ로 강등(불변식14).
        intent = _intent_via_llm_enum("email_send", keyword_only=True)
        assert intent.command_family is None
        assert intent.route in ("wiki", "structured", "graph")

    def test_llm_enum_command_kept_when_not_keyword_only(self):
        # 대조군: keyword_only=False면 기존대로 LLM-enum family 채택.
        intent = _intent_via_llm_enum("email_send", keyword_only=False)
        assert intent.command_family == "email_send"

    def test_deterministic_keyword_family_survives_keyword_only(self):
        # 치명 회귀 가드: route.py:227의 결정론 keyword_family return은 keyword_only에
        # 영향받지 않는다(그게 command_split이 의존하는 바로 그 경로).
        with patch(
            "orthus.agentwork.service.detect_assistant_command_action",
            return_value="connector_sync",
        ):
            intent = classify_intent("gmail 동기화해줘", allow_commands=True, keyword_only=True)
        assert intent.command_family == "connector_sync"


class TestClassifySubpartForwardsKeywordOnly:
    def test_command_split_forwards_keyword_only(self):
        with patch(
            "orthus.router.tools.classify_intent",
            return_value=RouteIntent(command_family=None, route="wiki"),
        ) as mci:
            classify_subpart("q", command_split=True)
            assert mci.call_args.kwargs["keyword_only"] is True
            classify_subpart("q", command_split=False)
            assert mci.call_args.kwargs["keyword_only"] is False


# ── D: fall-through 보강 (finding F3) ─────────────────────────────────────────


class TestFallthroughQueue:
    def _run(self, *, command_split, detect_family, queue_side_effect=None):
        item = SimpleNamespace(work_id=uuid4())
        with (
            patch(
                "orthus.agentwork.service.detect_assistant_command_action",
                return_value=detect_family,
            ),
            patch(
                "orthus.router.tools.queue_agent_work",
                side_effect=queue_side_effect,
                return_value=item,
            ) as mq,
        ):
            queued = d._maybe_queue_command_fallthrough(
                _USER,
                "gmail 동기화해줘",
                actor_role="admin",
                context_wiki_slug=None,
                chat_session_id=uuid4(),
                command_split=command_split,
            )
        return queued, mq

    def test_command_split_command_queued(self):
        queued, mq = self._run(command_split=True, detect_family="connector_sync")
        assert queued is True
        kw = mq.call_args.kwargs
        assert kw["command_split_origin"] is True
        assert kw["leaf_index"] is None  # 원문 통째 = 535 single-queue 멱등 (Z1)
        assert kw["intake_source"] == "decompose_fanout"
        assert kw["actor_role"] == "admin"

    def test_no_command_split_no_queue(self):
        queued, mq = self._run(command_split=False, detect_family="connector_sync")
        assert queued is False
        mq.assert_not_called()

    def test_non_command_text_no_queue(self):
        queued, mq = self._run(command_split=True, detect_family=None)
        assert queued is False
        mq.assert_not_called()

    def test_queue_failure_is_best_effort(self):
        # 큐잉이 터져도 False 반환(검색 답변까지 죽이지 않음).
        queued, _ = self._run(
            command_split=True, detect_family="connector_sync", queue_side_effect=RuntimeError("x")
        )
        assert queued is False

    def test_queue_none_suppressed(self):
        # queue_agent_work가 None(_suppress_action_intake) → False.
        with (
            patch(
                "orthus.agentwork.service.detect_assistant_command_action",
                return_value="connector_sync",
            ),
            patch("orthus.router.tools.queue_agent_work", return_value=None),
        ):
            queued = d._maybe_queue_command_fallthrough(
                _USER,
                "gmail 동기화해줘",
                actor_role="owner",
                context_wiki_slug=None,
                chat_session_id=None,
                command_split=True,
            )
        assert queued is False
