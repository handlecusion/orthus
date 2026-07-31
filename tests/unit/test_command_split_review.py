"""Code-review fix 회귀 게이트 (command-split S1–S5 리뷰 후속).

DB 없이 검증 가능한 순수 단위 경로만 고정한다:
- #1 fall-through digest 격리(hold-우회 차단)
- #9 SubAnswer enrich를 command_split에 게이트(flag-off byte-identical)
- #3 command_split should_decompose 우회를 실제 명령 존재에 게이트
- #2 resume_held_command_split_action 가드 + 실행 재개 dispatch

_hold의 policy_outcome 일치(#5/#6), execute 재영속, eager email 복원(#8),
federated 경고(#7)는 DB/route 통합 경로라 integration에서 커버한다(WSL 테스트 DB 부재).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

import orthus.agentwork.service as svc
import orthus.router.decompose as d
from orthus.schemas.canonical import SubQuestion

_USER = uuid4()
_SETTINGS = SimpleNamespace(node_id="company", node_kind="company")


def _cand(*, family, leaf_index=None, command_split_origin=False, question="x 해줘"):
    return svc.assistant_command_candidate(
        question,
        user_id=_USER,
        action_family=family,
        leaf_index=leaf_index,
        command_split_origin=command_split_origin,
        settings=_SETTINGS,
    )


# ── #1 fall-through digest 격리 ───────────────────────────────────────────────


class TestFallthroughDigestIsolation:
    @pytest.mark.parametrize("family", ["connector_sync", "personal_board_cleanup"])
    def test_fallthrough_distinct_from_plain_intake(self, family):
        # 핵심 안전 회귀: command-split fall-through(command_split_origin=True, leaf_index=None)는
        # 평범한 단일 명령(command_split_origin=False, leaf_index=None) 같은 텍스트와 source_ref_id가
        # 달라야 한다 — 같으면 persist on_conflict가 held 행을 auto_execute로 덮어써 실제 실행된다.
        held = _cand(family=family, leaf_index=None, command_split_origin=True, question="동기화 해줘")
        plain = _cand(family=family, leaf_index=None, command_split_origin=False, question="동기화 해줘")
        assert held is not None and plain is not None
        assert held.source_ref_id != plain.source_ref_id

    def test_two_fallthroughs_idempotent(self):
        # 같은 fall-through 두 번 → 같은 source_ref_id(":f" 안정 토큰) → 같은 held 행으로 dedupe.
        a = _cand(family="connector_sync", leaf_index=None, command_split_origin=True, question="동기화 해줘")
        b = _cand(family="connector_sync", leaf_index=None, command_split_origin=True, question="동기화 해줘")
        assert a.source_ref_id == b.source_ref_id

    def test_fallthrough_distinct_from_leaf(self):
        # fall-through(":f")와 fan-out leaf(":0")는 서로 다른 source_ref_id.
        f = _cand(family="connector_sync", leaf_index=None, command_split_origin=True, question="동기화 해줘")
        l0 = _cand(family="connector_sync", leaf_index=0, command_split_origin=True, question="동기화 해줘")
        assert f.source_ref_id != l0.source_ref_id


# ── #9 SubAnswer enrich를 command_split에 게이트 ─────────────────────────────


def _run_action_leaf(item, *, command_split):
    sub_q = SubQuestion(id=uuid4(), text="kim@x.com로 메일 보내줘", scope="company")
    with (
        patch.object(d, "classify_subpart", return_value=("action-intake", "email_send")),
        patch("orthus.router.tools.queue_agent_work", return_value=item),
    ):
        return d._run_leaf(
            sub_q,
            user_id=_USER,
            scope="company",
            project=None,
            chat_model=None,
            context_wiki_slug=None,
            actor_role="owner",
            allow_agentic_in_leaf=False,
            stream_key=None,
            leaf_index=0,
            command_split=command_split,
        )


def _work_item():
    return SimpleNamespace(
        work_id=uuid4(),
        state="request_more_data",
        policy_outcome="request_more_data",
        reason_codes=["command_split_hold"],
        title="Assistant command",
        payload={"question": "kim@x.com로 메일 보내줘", "recipient_hint": "kim@x.com"},
    )


class TestEnrichGatedOnCommandSplit:
    def test_command_split_true_enriches(self):
        sa = _run_action_leaf(_work_item(), command_split=True)
        assert sa.state == "request_more_data"
        assert sa.recipient == "kim@x.com"

    def test_command_split_false_leaves_chip_unset(self):
        # flag-off MA.4 경로: work_item이 non-None이어도 chip 필드는 미설정이어야 한다
        # (byte-identical) — agent_work_id만 이전처럼 설정된다.
        item = _work_item()
        sa = _run_action_leaf(item, command_split=False)
        assert sa.agent_work_id == item.work_id
        assert sa.state is None
        assert sa.recipient is None
        assert sa.reason_codes == []


# ── #3 should_decompose 우회를 실제 명령 존재에 게이트 ────────────────────────


class TestShouldDecomposeBypassGate:
    def _bypass(self, *, command_split, has_command):
        with patch(
            "orthus.agentwork.detect_assistant_command_action",
            return_value=("connector_sync" if has_command else None),
        ):
            return d._command_split_bypasses_should_decompose(
                "환불정책은 뭐고 배송은 어때", command_split
            )

    def test_flag_off_never_bypasses(self):
        assert self._bypass(command_split=False, has_command=True) is False

    def test_pure_question_does_not_bypass(self):
        # command_split=True + 명령 없음 → 우회 안 함(순수 질문 강제분해 방지).
        assert self._bypass(command_split=True, has_command=False) is False

    def test_command_bypasses(self):
        # command_split=True + 실제 명령 → 우회(명령 분할 유지).
        assert self._bypass(command_split=True, has_command=True) is True


# ── #2 resume_held_command_split_action ─────────────────────────────────────


class _FakeItem(SimpleNamespace):
    def model_copy(self, *, update):
        return _FakeItem(**{**self.__dict__, **update})


def _held_item(*, family="connector_sync", held_outcome="auto_execute", reason_codes=None):
    return _FakeItem(
        work_id=uuid4(),
        action_family=family,
        state="resolved",
        policy_outcome="request_more_data",
        reason_codes=reason_codes if reason_codes is not None else ["command_split_hold"],
        payload={"command_split_held_outcome": held_outcome},
        correlation_id=None,
    )


class TestResumeHeldCommandSplitAction:
    def test_non_held_item_untouched(self):
        item = _held_item(reason_codes=[])  # no command_split_hold
        with patch.object(svc, "execute_auto_connector_sync") as ex:
            out = svc.resume_held_command_split_action(_USER, item, settings=_SETTINGS)
        assert out is item
        ex.assert_not_called()

    def test_wrong_held_outcome_untouched(self):
        item = _held_item(held_outcome="draft_for_review")
        with patch.object(svc, "execute_auto_connector_sync") as ex:
            out = svc.resume_held_command_split_action(_USER, item, settings=_SETTINGS)
        assert out is item
        ex.assert_not_called()

    def test_held_connector_resumes_with_forced_auto_execute(self):
        item = _held_item(family="connector_sync")
        sentinel = SimpleNamespace(work_id=item.work_id, state="resolved")
        with patch.object(svc, "execute_auto_connector_sync", return_value=sentinel) as ex:
            out = svc.resume_held_command_split_action(_USER, item, settings=_SETTINGS)
        assert out is sentinel
        passed = ex.call_args.args[1]
        # 원 판정을 in-memory로 복원해 typed execute 가드(state=="auto_execute")를 통과시킨다.
        assert passed.state == "auto_execute"
        assert passed.policy_outcome == "auto_execute"

    def test_held_board_cleanup_resumes(self):
        item = _held_item(family="personal_board_cleanup")
        sentinel = SimpleNamespace(work_id=item.work_id, state="resolved")
        with patch.object(svc, "execute_auto_personal_board_cleanup", return_value=sentinel) as ex:
            out = svc.resume_held_command_split_action(_USER, item, settings=_SETTINGS)
        assert out is sentinel
        ex.assert_called_once()
