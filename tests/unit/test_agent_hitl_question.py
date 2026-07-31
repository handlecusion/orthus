"""HITL gate-question projection + resume merge-text (deterministic, no DB/LLM).

build_gate_question is a pure projection of a policy-gate request_more_data
outcome into a chat question, and _merge_gate_answer weaves a bare-name answer
into the recipient-shaped text the email gate needs. Both must be deterministic
and must not fire for items that already own a resume path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from orthus.agentwork.hitl import (
    _gate_fields,
    _merge_gate_answer,
    build_gate_question,
    merge_field_values,
)
from orthus.schemas.canonical import AgentWorkItem, UserInputField

_WORK = UUID("11111111-1111-4111-8111-111111111111")


def _item(**over) -> AgentWorkItem:
    base = dict(
        work_id=_WORK,
        node_id="company",
        node_kind="company",
        source_kind="assistant_command",
        source_ref_id="ref1",
        action_family="email_send",
        title="메일 보내기",
        payload={
            "required_data": ["recipient name, address, or thread reference"],
            "question": "요약해서 메일 보내줘",
        },
        state="request_more_data",
        reason_codes=["recipient_required"],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(over)
    return AgentWorkItem(**base)


def test_email_recipient_missing_builds_question():
    uir = build_gate_question(_item())
    assert uir is not None
    assert uir.origin == "policy_gate"
    assert uir.input_type == "text"
    assert uir.status == "waiting"
    assert uir.work_id == str(_WORK)
    assert "recipient name, address, or thread reference" in uir.question
    assert uir.resume["original_question"] == "요약해서 메일 보내줘"
    assert uir.resume["action_family"] == "email_send"


def test_draft_for_review_returns_none():
    assert build_gate_question(_item(state="draft_for_review")) is None


def test_rejected_returns_none():
    assert build_gate_question(_item(state="rejected")) is None


def test_missing_required_data_returns_none():
    assert build_gate_question(_item(payload={"question": "x"})) is None
    assert build_gate_question(_item(payload={"required_data": []})) is None


def test_command_split_hold_returns_none():
    assert (
        build_gate_question(_item(reason_codes=["recipient_required", "command_split_hold"]))
        is None
    )


def test_agent_task_family_returns_none():
    assert build_gate_question(_item(action_family="agent_task")) is None


def test_deterministic_same_input_same_question():
    a = build_gate_question(_item())
    b = build_gate_question(_item())
    assert a is not None and b is not None
    assert a.question == b.question
    assert a.resume == b.resume


def test_merge_email_recipient_weaves_bare_name():
    # A bare-name answer must become "{answer}에게 {original}" so the recipient
    # regex can extract it on the resume turn (else it loops on the same gate).
    merged = _merge_gate_answer(
        "요약해서 메일 보내줘",
        "김민수",
        "email_send",
        {"required_data": ["recipient name, address, or thread reference"]},
    )
    assert merged == "김민수에게 요약해서 메일 보내줘"


def test_merge_non_email_appends_extra_info():
    merged = _merge_gate_answer(
        "커넥터 동기화해줘",
        "notion 계정",
        "connector_sync",
        {"required_data": ["account"]},
    )
    assert merged == "커넥터 동기화해줘\n(사용자 추가 정보: notion 계정)"


def test_merge_empty_answer_returns_original():
    assert _merge_gate_answer("orig", "", "email_send", {}) == "orig"


# ── 후속 B: fill-in-the-blank fields projection ──


def test_email_recipient_question_carries_recipient_field():
    uir = build_gate_question(_item())
    assert [f.model_dump() for f in uir.fields] == [
        UserInputField(
            name="recipient", label="받는 사람", placeholder="이름 또는 이메일", required=True
        ).model_dump()
    ]


def test_non_email_family_question_has_no_fields():
    uir = build_gate_question(
        _item(
            action_family="connector_sync",
            payload={"required_data": ["account"], "question": "커넥터 동기화해줘"},
        )
    )
    assert uir is not None
    assert uir.fields == []


# ── 후속 B: merge_field_values (pure) ──

_RECIPIENT_FIELDS = [
    {"name": "recipient", "label": "받는 사람", "placeholder": "이름 또는 이메일", "required": True}
]


def test_merge_field_values_single_field_bare_value_stripped():
    assert merge_field_values(_RECIPIENT_FIELDS, {"recipient": "  김민수  "}) == "김민수"


def test_merge_field_values_undeclared_key_returns_none():
    assert merge_field_values(_RECIPIENT_FIELDS, {"foo": "bar"}) is None


def test_merge_field_values_blank_required_returns_none():
    assert merge_field_values(_RECIPIENT_FIELDS, {"recipient": "  "}) is None


def test_merge_field_values_empty_fields_returns_none():
    assert merge_field_values([], {"recipient": "김민수"}) is None


def test_merge_field_values_multi_field_label_value_lines():
    fields = [
        {"name": "recipient", "label": "받는 사람", "required": True},
        {"name": "subject", "label": "제목", "required": True},
    ]
    merged = merge_field_values(fields, {"recipient": "김민수", "subject": "주간 보고"})
    assert merged == "받는 사람: 김민수\n제목: 주간 보고"


def test_merge_field_values_optional_field_may_be_omitted():
    fields = [
        {"name": "recipient", "label": "받는 사람", "required": True},
        {"name": "cc", "label": "참조", "required": False},
    ]
    merged = merge_field_values(fields, {"recipient": "김민수"})
    assert merged == "받는 사람: 김민수"


def test_gate_fields_lockstep_with_merge_gate_answer():
    # Every (action_family, required_data) input that yields a recipient form must
    # resume through the recipient weaving branch of _merge_gate_answer.
    family = "email_send"
    required = ["recipient name, address, or thread reference"]
    fields = _gate_fields(family, required)
    assert fields and fields[0].name == "recipient"
    merged = _merge_gate_answer("원명령", "김민수", family, {"required_data": required})
    assert merged.startswith("김민수에게 ")
