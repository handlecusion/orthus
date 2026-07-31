"""S4-4a 수용 게이트 — 배치 ids 조회 검증 + recipient 이중 payload 추출 +
SubAnswer/AgentWorkIntakeResult chip enrich 역주입.

DB 없이: extract_command_recipient 순수 + route ids 파싱/상한 + _run_leaf 역주입.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

import orthus.api.routes.agent_work as awr
import orthus.router.decompose as d
from orthus.agentwork.service import extract_command_recipient
from orthus.schemas.canonical import SubQuestion

_USER = uuid4()


# ── recipient 이중 payload 추출 (reviewer Z4) ─────────────────────────────────


class TestExtractRecipient:
    @pytest.mark.parametrize(
        "payload, expected",
        [
            ({"recipient_hint": "a@b.com"}, "a@b.com"),  # assistant_command intake
            ({"email_draft": {"recipient_hint": "c@d.com"}}, "c@d.com"),  # reply draft
            ({"reply_context": {"to_addr": "e@f.com"}}, "e@f.com"),  # P7.1 reply_context
            ({"question": "x"}, None),  # non-email
            (None, None),
            ({"recipient_hint": "  "}, None),  # blank ignored
        ],
    )
    def test_extract(self, payload, expected):
        assert extract_command_recipient(payload) == expected


# ── GET /agent-work?ids= 파싱/상한 ───────────────────────────────────────────


def _call_list(ids):
    current = SimpleNamespace(user_id=_USER, auth_mode="session", role="owner")
    with (
        patch.object(awr, "require_node_operator"),
        patch.object(awr, "list_agent_work_items", return_value=[]) as mock_list,
        patch.object(awr, "_enrich_items", return_value=[]),
    ):
        awr.list_work_items(
            state=None,
            source_kind=None,
            outcome=None,
            scope=None,
            wiki_slug=None,
            ids=ids,
            current=current,
        )
    return mock_list


class TestBatchIds:
    def test_valid_ids_passed_through(self):
        a, b = uuid4(), uuid4()
        mock_list = _call_list(f"{a},{b}")
        passed = mock_list.call_args.kwargs["ids"]
        assert passed == [a, b]

    def test_ids_none_means_no_batch(self):
        mock_list = _call_list(None)
        assert mock_list.call_args.kwargs["ids"] is None

    def test_too_many_ids_400(self):
        many = ",".join(str(uuid4()) for _ in range(awr._MAX_BATCH_IDS + 1))
        with pytest.raises(awr.HTTPException) as exc:
            _call_list(many)
        assert exc.value.status_code == 400

    def test_invalid_uuid_400(self):
        with pytest.raises(awr.HTTPException) as exc:
            _call_list("not-a-uuid")
        assert exc.value.status_code == 400


# ── _run_leaf SubAnswer chip 역주입 ──────────────────────────────────────────


def _work_item(**kw):
    base = dict(
        work_id=uuid4(),
        state="request_more_data",
        policy_outcome="auto_execute",  # held: outcome=auto_execute, state=request_more_data
        reason_codes=["command_split_hold"],
        title="Assistant command: gmail 동기화해줘",
        payload={"question": "gmail 동기화해줘", "recipient_hint": "kim@x.com"},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _run_action_leaf(item):
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
            command_split=True,
        )


class TestRunLeafChipEnrich:
    def test_back_injects_item_fields(self):
        item = _work_item()
        sa = _run_action_leaf(item)
        assert sa.agent_work_id == item.work_id
        assert sa.state == "request_more_data"  # BADGE SoR = state (held truth)
        assert sa.policy_outcome == "auto_execute"  # reference only
        assert sa.reason_codes == ["command_split_hold"]
        assert sa.title == item.title
        assert sa.recipient == "kim@x.com"

    def test_item_none_leaves_chip_fields_unset(self):
        # queue suppressed (None) → agent_work_id None, chip fields default (no chip drawn).
        sa = _run_action_leaf(None)
        assert sa.agent_work_id is None
        assert sa.state is None
        assert sa.recipient is None
        assert sa.reason_codes == []
