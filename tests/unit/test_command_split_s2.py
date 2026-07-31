"""S2 수용 게이트 — leaf_index 멱등 + chat_session_id/intake_source 마커 +
command_split_origin auto-execute 억제(BLOCKER A/C).

DB 없이: assistant_command_candidate digest/payload는 순수 단위테스트,
create_assistant_command_work_item 억제 분기는 executor mock으로 고정.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import orthus.agentwork.service as svc

_USER = uuid4()
_SETTINGS = SimpleNamespace(node_id="company", node_kind="company")


def _cand(
    *,
    family,
    leaf_index=None,
    command_split_origin=False,
    intake_source=None,
    chat_session_id=None,
    question="x 해줘",
):
    return svc.assistant_command_candidate(
        question,
        user_id=_USER,
        action_family=family,
        leaf_index=leaf_index,
        command_split_origin=command_split_origin,
        intake_source=intake_source,
        chat_session_id=chat_session_id,
        settings=_SETTINGS,
    )


# ── BLOCKER A: leaf_index digest (외부쓰기 3-family만) ─────────────────────────


class TestLeafIndexDigest:
    def test_external_families_set(self):
        # 2-family가 아니라 3-family (personal_board_cleanup 포함) 고정.
        assert svc._EXTERNAL_WRITE_FAMILIES == frozenset(
            {"email_send", "connector_sync", "personal_board_cleanup"}
        )

    def test_case2_source_ref_stable(self):
        # leaf_index=None(case2 단일 명령) 반복 → source_ref_id 동일 (byte-identical 멱등).
        a = _cand(family="email_send", leaf_index=None, question="a@b.com로 메일 보내줘")
        b = _cand(family="email_send", leaf_index=None, question="a@b.com로 메일 보내줘")
        assert a.source_ref_id == b.source_ref_id

    def test_external_write_distinct_leaves(self):
        # command_split fan-out(command_split_origin=True) 같은 정제질문·email_send를
        # leaf_index=0,1 → source_ref_id 2개(중복 외부쓰기 방지).
        l0 = _cand(
            family="email_send",
            leaf_index=0,
            command_split_origin=True,
            question="a@b.com로 메일 보내줘",
        )
        l1 = _cand(
            family="email_send",
            leaf_index=1,
            command_split_origin=True,
            question="a@b.com로 메일 보내줘",
        )
        assert l0.source_ref_id != l1.source_ref_id

    def test_leaf_index_only_affects_digest_under_command_split(self):
        # 회귀 방지(리뷰어 b + byte-identical): command_split_origin=False면 leaf_index가
        # digest에 안 들어간다 → 기존 MA.4 decompose-action 경로 byte-identical + held 아이템과
        # non-split 재큐가 다른 source_ref_id라 promotion collision 불가.
        none_ref = _cand(family="email_send", leaf_index=None, question="a@b.com로 메일 보내줘")
        # command_split 아님 + leaf_index 있어도 suffix 없음 → None과 동일.
        off_ref = _cand(
            family="email_send",
            leaf_index=1,
            command_split_origin=False,
            question="a@b.com로 메일 보내줘",
        )
        assert none_ref.source_ref_id == off_ref.source_ref_id
        # command_split이면 분리.
        on_ref = _cand(
            family="email_send",
            leaf_index=1,
            command_split_origin=True,
            question="a@b.com로 메일 보내줘",
        )
        assert on_ref.source_ref_id != none_ref.source_ref_id

    def test_leaf_index_zero_not_falsy(self):
        # leaf_index=0은 falsy이지만 실재 값 — case2(None)와 달라야 한다(strict is not None).
        none_ref = _cand(family="email_send", leaf_index=None, question="a@b.com로 메일 보내줘")
        zero_ref = _cand(
            family="email_send",
            leaf_index=0,
            command_split_origin=True,
            question="a@b.com로 메일 보내줘",
        )
        assert none_ref.source_ref_id != zero_ref.source_ref_id

    def test_non_external_family_collapses(self):
        # document_draft는 외부쓰기 아님 → command_split이어도 leaf_index 무시, 1 row로 수렴.
        l0 = _cand(
            family="document_draft",
            leaf_index=0,
            command_split_origin=True,
            question="회의록 문서로 작성해줘",
        )
        l1 = _cand(
            family="document_draft",
            leaf_index=1,
            command_split_origin=True,
            question="회의록 문서로 작성해줘",
        )
        assert l0.source_ref_id == l1.source_ref_id


# ── T-C4-1: payload 마커 (intake_source + chat_session_id) ────────────────────


class TestPayloadMarkers:
    def test_fanout_marks_intake_source_and_session(self):
        sid = uuid4()
        c = _cand(
            family="email_send",
            leaf_index=0,
            command_split_origin=True,
            intake_source="decompose_fanout",
            chat_session_id=sid,
            question="a@b.com로 메일 보내줘",
        )
        assert c.payload["intake_source"] == "decompose_fanout"
        assert c.payload["chat_session_id"] == str(sid)

    def test_case2_no_markers_even_with_session(self):
        # 회귀 #3(flag-off byte-identical): 535/case2 경로는 chat_session_id를 항상 넘기지만
        # command_split_origin=False라 payload에 마커가 안 생겨야 한다(기존 payload shape 유지).
        sid = uuid4()
        c = _cand(
            family="email_send",
            command_split_origin=False,
            chat_session_id=sid,
            question="a@b.com로 메일 보내줘",
        )
        assert "intake_source" not in c.payload
        assert "chat_session_id" not in c.payload


# ── BLOCKER C: command_split_origin auto-execute 억제 ─────────────────────────


def _run_create(*, command_split_origin, state, family):
    """create_assistant_command_work_item을 mock persist/executor로 호출하고
    (hold 호출?, connector 실행 호출?, agent_task 실행 호출?)를 회수한다. DB 미사용."""
    item = SimpleNamespace(state=state, action_family=family, work_id=uuid4())
    candidate = SimpleNamespace(source_kind="assistant_command")
    with (
        patch.object(svc, "assistant_command_candidate", return_value=candidate),
        patch.object(
            svc,
            "classify_candidate_with_policy_memory",
            return_value=(candidate, MagicMock(), uuid4(), None),
        ),
        patch.object(svc, "persist_agent_work_item", return_value=item),
        patch.object(svc, "_hold_command_split_auto_execute", return_value=item) as mock_hold,
        patch.object(svc, "execute_auto_agent_task", return_value=item) as mock_task,
        patch.object(svc, "execute_auto_connector_sync", return_value=item) as mock_conn,
        patch.object(svc, "execute_auto_personal_board_cleanup", return_value=item) as mock_board,
        patch.object(svc, "ensure_document_draft_for_work_item", return_value=item),
        patch.object(svc, "ensure_email_draft_for_work_item", return_value=item),
        patch.object(svc, "execute_auto_email_send", return_value=item) as mock_send,
    ):
        svc.create_assistant_command_work_item(
            _USER,
            "q",
            action_family=family,
            command_split_origin=command_split_origin,
            settings=_SETTINGS,
        )
    return SimpleNamespace(
        held=mock_hold.called,
        conn=mock_conn.called,
        task=mock_task.called,
        board=mock_board.called,
        send=mock_send.called,
    )


class TestCommandSplitOriginSuppression:
    def test_command_split_holds_connector(self):
        # command_split_origin=True + auto_execute connector → hold, 실행 안 함.
        r = _run_create(command_split_origin=True, state="auto_execute", family="connector_sync")
        assert r.held is True
        assert r.conn is False and r.task is False and r.board is False and r.send is False

    def test_case2_connector_executes(self):
        # command_split_origin=False(case2) → 기존 실행 체인 그대로(byte-identical).
        r = _run_create(command_split_origin=False, state="auto_execute", family="connector_sync")
        assert r.held is False
        assert r.conn is True

    def test_command_split_agent_task_not_dispatched(self):
        # 위임 fragment도 command_split_origin이면 dispatch 억제(hold).
        r = _run_create(command_split_origin=True, state="auto_execute", family="agent_task")
        assert r.held is True
        assert r.task is False

    def test_command_split_email_send_no_auto_send(self):
        # email_send는 auto_execute가 아니라 draft_for_review지만, command_split이면
        # execute_auto_email_send도 절대 안 탄다(방어적 — 리뷰어 D).
        r = _run_create(command_split_origin=True, state="draft_for_review", family="email_send")
        assert r.send is False
