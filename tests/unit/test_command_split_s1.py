"""S1 수용 게이트 — Command/Question 상위 분할 진입/신호/권한 가드
(docs/company-agent-orchestration.md — command-split, BLOCKER F/G).

라이브 DB/LLM 없이 결정론 헬퍼 + answer() ladder cmd_split 게이트를 고정한다:
- command_split_active: ask_decompose_enabled에 AND 종속(BLOCKER 정합, flag-off byte-identical)
- command_split_signal: 접속/열거 결정론 신호(LLM 0회, 공유 토큰셋 재사용)
- has_strong_command: strong-command 선판정(BLOCKER F)
- _command_split_allowed: session owner/admin 게이트(BLOCKER G)
- ladder cmd_split: (flag × signal × role × auth_mode) 매트릭스
- answer_or_decompose(command_split=True): should_decompose LLM 우회
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from orthus.agentwork import has_strong_command
from orthus.api.routes.agent_work import _command_split_allowed
from orthus.router.decompose import command_split_signal
from orthus.schemas.canonical import RoutedAnswer
from orthus.settings import command_split_active

_USER = uuid4()


@contextmanager
def _noop_audit(_name, **_kw):
    span = MagicMock()
    span.add_meta = MagicMock()
    yield span


# ── command_split_active: AND 종속 (BLOCKER 정합) ────────────────────────────


class TestCommandSplitActive:
    @pytest.mark.parametrize(
        "split_on, decompose_on, expected",
        [
            (True, True, True),
            (True, False, False),  # 상위 flag off → 강등 (silent False)
            (False, True, False),
            (False, False, False),
        ],
    )
    def test_and_dependency(self, split_on, decompose_on, expected):
        settings = SimpleNamespace(
            ask_command_split_enabled=split_on, ask_decompose_enabled=decompose_on
        )
        assert command_split_active(settings) is expected


# ── command_split_signal: 결정론 접속/열거 신호 ───────────────────────────────


class TestCommandSplitSignal:
    @pytest.mark.parametrize(
        "question, expected",
        [
            ("A는 뭐야 그리고 B는 뭐야", True),
            ("진행상황 요약해주고 gmail 동기화해줘", True),
            ("1. 매출 알려줘 2. 메일 보내줘", True),
            ("환불정책이 뭐야", False),
            ("gmail 동기화해줘", False),  # 단일 명령: 신호 없음 → case2
        ],
    )
    def test_signal(self, question, expected):
        assert command_split_signal(question) is expected


# ── has_strong_command: strong-command 선판정 (BLOCKER F) ─────────────────────


class TestHasStrongCommand:
    @pytest.mark.parametrize(
        "question, expected",
        [
            ("gmail 동기화해줘", True),  # connector_sync → 항상 strong
            ("회의록 요약해서 kim@x.com로 메일 초안 작성해", True),  # email + literal recipient
            ("메일 답장 요약해줘", False),  # recipient 없음 → ambiguous
            ("환불정책이 뭐야", False),  # 명령 아님
            ("회의 내용을 문서로 작성해줘", False),  # document_draft = non-strong
        ],
    )
    def test_strong(self, question, expected):
        assert has_strong_command(question) is expected


# ── _command_split_allowed: session owner/admin 게이트 (BLOCKER G) ────────────


class TestCommandSplitAllowed:
    @pytest.mark.parametrize(
        "auth_mode, role, expected",
        [
            ("session", "owner", True),
            ("session", "admin", True),
            ("session", "demo", False),
            ("session", None, False),
            ("demo", "owner", False),  # 비-session은 role이 owner여도 거부(fail-open 봉쇄)
            ("jwt", None, False),
        ],
    )
    def test_allowed(self, auth_mode, role, expected):
        current = SimpleNamespace(auth_mode=auth_mode, role=role)
        assert _command_split_allowed(current) is expected


# ── answer() ladder: cmd_split 게이트 매트릭스 ────────────────────────────────


def _run_ladder(*, split_on, question, actor_role, auth_mode):
    """answer()를 호출하고 answer_or_decompose에 전달된 command_split/actor_role 인자를 회수한다."""
    from orthus.router import answer

    decompose_result = RoutedAnswer(question=question, mode="decomposed")
    with (
        patch("orthus.router.get_settings") as mock_settings,
        patch("orthus.router.should_federate", return_value=False),
        patch(
            "orthus.router.decompose.answer_or_decompose", return_value=decompose_result
        ) as mock_aod,
    ):
        settings = MagicMock()
        settings.ask_decompose_enabled = True
        settings.ask_command_split_enabled = split_on
        settings.agentic_ask_enabled = False
        settings.ask_semantic_cache_enabled = False
        mock_settings.return_value = settings

        answer(
            _USER,
            question,
            scope="company",
            allow_decompose=True,
            actor_role=actor_role,
            auth_mode=auth_mode,
        )
    mock_aod.assert_called_once()
    return mock_aod.call_args.kwargs


class TestLadderCmdSplitGate:
    def test_flag_off_command_split_false(self):
        # BLOCKER: flag off → command_split=False (byte-identical decompose 경로)
        assert (
            _run_ladder(
                split_on=False,
                question="A는 뭐야 그리고 gmail 동기화해줘",
                actor_role="owner",
                auth_mode="session",
            )["command_split"]
            is False
        )

    def test_session_owner_compound_true(self):
        assert (
            _run_ladder(
                split_on=True,
                question="A는 뭐야 그리고 gmail 동기화해줘",
                actor_role="owner",
                auth_mode="session",
            )["command_split"]
            is True
        )

    def test_non_session_degrades(self):
        # BLOCKER G: 비-session은 owner 리터럴이라도 command_split=False
        assert (
            _run_ladder(
                split_on=True,
                question="A는 뭐야 그리고 gmail 동기화해줘",
                actor_role="owner",
                auth_mode="demo",
            )["command_split"]
            is False
        )

    def test_non_operator_role_degrades(self):
        assert (
            _run_ladder(
                split_on=True,
                question="A는 뭐야 그리고 gmail 동기화해줘",
                actor_role="demo",
                auth_mode="session",
            )["command_split"]
            is False
        )

    def test_no_signal_degrades(self):
        # 접속/열거 신호 없는 단일 질문은 command_split 미발화
        assert (
            _run_ladder(
                split_on=True,
                question="환불정책이 뭐야",
                actor_role="owner",
                auth_mode="session",
            )["command_split"]
            is False
        )


# ── F2: flag-off/non-command-split leaf actor_role는 "owner" 리터럴 보존 ──────


class TestLeafActorRoleByteIdentical:
    def test_flag_off_leaf_role_owner(self):
        # 리뷰 F2: flag off인데 session admin이면 leaf actor_role은 "owner" 보존
        # (기존 decompose 경로 byte-identical — policy-gate 입력 불변).
        kwargs = _run_ladder(
            split_on=False,
            question="A는 뭐야 그리고 X에게 메일 보내줘",
            actor_role="admin",
            auth_mode="session",
        )
        assert kwargs["command_split"] is False
        assert kwargs["actor_role"] == "owner"

    def test_command_split_leaf_role_real(self):
        # command_split=True(escalation)일 때만 실제 role(admin) 전달.
        kwargs = _run_ladder(
            split_on=True,
            question="A는 뭐야 그리고 gmail 동기화해줘",
            actor_role="admin",
            auth_mode="session",
        )
        assert kwargs["command_split"] is True
        assert kwargs["actor_role"] == "admin"

    def test_pure_question_decompose_role_owner(self):
        # flag on이지만 신호 없는(=command_split=False) 순수 질문 decompose도 "owner" 보존.
        kwargs = _run_ladder(
            split_on=True,
            question="환불정책이 뭐야",
            actor_role="admin",
            auth_mode="session",
        )
        assert kwargs["command_split"] is False
        assert kwargs["actor_role"] == "owner"


# ── F1: 페더레이션 dead-zone — compound command가 소실되지 않는다 ─────────────


def _run_orchestrate(*, federated, question, split_on=True, create_return="item"):
    """orchestrate_chat_route를 mock 의존성으로 직접 호출하고, 결과 지표를 회수한다. DB 미사용.
    create_return="item"이면 command intake가 item을 만들고, None이면 후보 억제(item 없음)."""
    import orthus.api.routes.agent_work as awr

    current = SimpleNamespace(auth_mode="session", role="owner", user_id=_USER)
    body = SimpleNamespace(
        text=question,
        context_wiki_slug=None,
        scope="all",
        project=None,
        context_mail_id=None,
        stream_id=None,
    )
    item = SimpleNamespace(
        work_id=uuid4(),
        source_kind="assistant_command",
        source_ref_id="assistant-command-x",
        action_family="connector_sync",
        title="t",
        state="draft_for_review",
        policy_outcome="draft_for_review",
        policy_reason=None,
        reason_codes=[],
        payload={"question": "gmail 동기화해줘"},
    )
    settings = MagicMock()
    settings.node_id = "company"
    settings.ask_command_split_enabled = split_on
    settings.ask_decompose_enabled = True
    settings.ask_decompose_command_guard = True
    create_val = item if create_return == "item" else None
    with (
        patch.object(awr, "require_node_operator"),
        patch.object(awr, "get_settings", return_value=settings),
        patch.object(awr, "get_chat_session", return_value=object()),
        patch.object(awr, "should_federate", return_value=federated),
        patch.object(
            awr, "create_assistant_command_work_item", return_value=create_val
        ) as mock_queue,
        patch.object(awr, "answer") as mock_answer,
        patch.object(awr, "load_chat_history", return_value=[]),
        patch.object(awr, "resolve_answer_scope", return_value="all"),
    ):
        mock_answer.return_value = RoutedAnswer(question=question, mode="wiki")
        awr.orchestrate_chat_route(uuid4(), body, current)
    return SimpleNamespace(
        queued=mock_queue.called,
        answered=mock_answer.called,
        answer_kwargs=(mock_answer.call_args.kwargs if mock_answer.called else None),
    )


class TestFederatedDeadZone:
    def test_federated_compound_command_still_queued(self):
        # 리뷰 F1: personal node + scope=all(federate)에서 복합 명령이 소실되면 안 된다.
        # federated면 decompose가 안 도므로 535 single-queue로 명령을 큐잉해야 한다.
        r = _run_orchestrate(federated=True, question="3분기 매출 알려줘 그리고 gmail 동기화해줘")
        assert r.queued is True
        assert r.answered is False

    def test_non_federated_compound_flows_to_decompose(self):
        # 비-federated면 복합(비-strong 판정 아님이지만 strong이어도 non-federated는 split)에서
        # 535를 건너뛰고 answer()로 흘러 fan-out 라우팅에 맡긴다.
        # (strong-command면 guard_degrade로 535를 타므로, 여기선 non-strong 복합으로 확인.)
        r = _run_orchestrate(federated=False, question="환불정책 알려주고 배송정책도 알려줘")
        assert r.queued is False
        assert r.answered is True

    def test_guard_degrade_item_none_does_not_resplit(self):
        # 리뷰 #5: guard_degrade(strong-command 복합)인데 create가 None(후보 억제)이면,
        # answer()로 fall-through하되 allow_decompose=False라야 재-split(BLOCKER F 무력화)이 없다.
        r = _run_orchestrate(
            federated=False,
            question="회의록 요약해서 a@b.com로 메일 보내줘 그리고 gmail 동기화해줘",
            create_return=None,
        )
        assert r.answered is True
        assert r.answer_kwargs["allow_decompose"] is False

    def test_non_guard_compound_keeps_decompose(self):
        # 대조군: guard_degrade 아님(비-strong 복합) + create None → allow_decompose=True 유지.
        r = _run_orchestrate(
            federated=False, question="환불정책 알려주고 배송정책도 알려줘", create_return=None
        )
        assert r.answered is True
        assert r.answer_kwargs["allow_decompose"] is True


# ── answer_or_decompose(command_split=True): should_decompose LLM 우회 ────────


class TestCommandSplitBypass:
    def test_command_split_bypasses_should_decompose(self):
        from orthus.router import decompose as d

        sentinel = RoutedAnswer(question="q", mode="wiki")
        with (
            patch.object(d, "should_decompose", side_effect=AssertionError("must not run")) as sd,
            patch.object(d, "split_question", return_value=[]),  # len<2 → split_empty fall-through
            # split_empty fall-through queues a strong command via queue_agent_work; force it to
            # not-queue (None) so the path returns the _answer sentinel deterministically. Without
            # this the assertion is DB-dependent (CI test DB queues → degrade answer, no DB → None
            # → sentinel), i.e. the failure exercised in CI. The bypass itself (sd.assert_not_called)
            # is the real subject of this test.
            patch("orthus.router.tools.queue_agent_work", return_value=None),
            patch("orthus.router.answer", return_value=sentinel),
            patch.object(d, "audit", side_effect=_noop_audit),
            patch.object(d, "get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.ask_decompose_max_parts = 5
            settings.ask_decompose_max_concurrency = 4
            settings.ask_decompose_leaf_timeout_sec = 30
            settings.ask_decompose_depends_enabled = False
            settings.ask_decompose_full_dag_enabled = False
            settings.ask_decompose_max_depth = 2
            settings.ask_decompose_max_total_leaves = 12
            mock_settings.return_value = settings

            result = d.answer_or_decompose(
                _USER,
                "A는 뭐야 그리고 gmail 동기화해줘",
                scope="company",
                project=None,
                chat_model=None,
                context_wiki_slug=None,
                history=None,
                stream_id=None,
                actor_role="owner",
                allow_agentic_in_leaf=False,
                learn=False,
                record_gaps=False,
                command_split=True,
            )
        # should_decompose는 호출되지 않아야 한다(bypass). split=[] → 단일 answer fall-through.
        sd.assert_not_called()
        assert result is sentinel
