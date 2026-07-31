"""LLM이 추론한 메일 위임은 절대 auto_execute되지 않는다 (R1).

왜 이 회귀가 필요한가: `agent_task` 게이트의 검사는 전부 **필드**에 관한 것이다 — instruction이
있는가, assignee가 resolve되는가, daemon이 enrolled인가. 정작 **"이 텍스트가 정말 위임인가"**는
검사하지 않는다. 그 판단은 LLM이 **신뢰할 수 없는 외부 메일 본문** 위에서 내리고, 오탐 1건이면
팀원 Mac에서 **샌드박스 없는 full-access 에이전트**가 돈다(AGENTS.md agent_task carve-out).

신규 적대적 홀드아웃(`experiments/fugu-ko/golden/t10_holdout2.json`, 함정 24개)에서 **가장 좋은
모델(EXAONE)조차 2/24를 오탐**했다 — 회의록 액션아이템과 자기 배정이 각각 실존 팀원을 assignee로
뽑았다(gpt-4o-mini 7, A.X 11). 회의록은 사내 메일에서 가장 흔한 형식이라 꼬리 위험이 아니다.

그래서 결정론 게이트가 **추측을 멈추고 사람에게 묻는다**. 이 테스트가 그 계약을 고정한다.
"""

from __future__ import annotations

from orthus.agentwork import apply_policy
from orthus.schemas.canonical import AgentWorkCandidate


def _agent_task_payload(**overrides: object) -> dict:
    """dispatch 조건을 **전부 충족**한 payload. 여기서 하나만 바꿔 각 가드를 시험한다."""
    payload: dict = {
        "kind": "agent_task",
        "node_id": "company",
        "node_kind": "company",
        "agent_task_enabled": True,
        "assignee_input": "김철수",
        "assignee_user_id": "11111111-1111-1111-1111-111111111111",
        "assignee_node_id": "personal-kim",
        "mode": "code",
        "runner": "claude",
        "instruction": "배포 스크립트 타임아웃 수정",
        "actor_role": "scheduler",
    }
    payload.update(overrides)
    return payload


def _candidate(payload: dict) -> AgentWorkCandidate:
    return AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id="agent-task-test",
        action_family="agent_task",
        title="위임",
        payload=payload,
        evidence=[{"kind": "agent_task", "ref": "agent-task-test"}],
    )


def test_llm_inferred_mail_delegation_never_auto_executes() -> None:
    """★ 핵심 계약: LLM이 메일에서 추론한 위임은 사람 검토를 거친다."""
    decision = apply_policy(_candidate(_agent_task_payload(llm_inferred=True)))

    assert decision.outcome == "draft_for_review", (
        "LLM이 신뢰할 수 없는 메일 본문에서 '이건 위임'이라고 판정한 것을 그대로 실행하면, "
        "오탐 1건이 팀원 PC에서 full-access 에이전트를 띄운다. 반드시 사람이 먼저 본다."
    )
    assert "agent_task_llm_inferred" in decision.reason_codes
    assert "human_review_required" in decision.reason_codes


def test_human_stated_delegation_still_auto_executes() -> None:
    """대조군: 사람이 직접 친 위임("김철수님께 X 맡겨줘")은 영향받지 않는다.

    이 슬라이스는 **LLM 추론**만 막는다. 사람이 이미 의도를 말했으면 그 판단을 다시 물을 이유가 없다.
    이 테스트가 없으면 "안전하게 만든다"가 기능 전체를 죽이는 변경으로 조용히 번질 수 있다.
    """
    decision = apply_policy(_candidate(_agent_task_payload()))  # llm_inferred 없음

    assert decision.outcome == "auto_execute"
    assert "agent_task_dispatch_ready" in decision.reason_codes


def test_llm_inferred_still_fails_the_field_gates_first() -> None:
    """순서: 필드가 비면 draft_for_review가 아니라 request_more_data다.

    `llm_inferred` 검사가 기존 가드를 **가로채면 안 된다** — enrolled daemon이 없는 후보가
    "검토 대기"로 보이면 리뷰어가 승인했을 때 dispatch가 조용히 실패한다.
    """
    decision = apply_policy(
        _candidate(_agent_task_payload(llm_inferred=True, assignee_node_id=None))
    )

    assert decision.outcome == "request_more_data"
    assert "agent_task_dispatch_incomplete" in decision.reason_codes


def test_llm_inferred_on_disabled_flag_is_still_rejected() -> None:
    """fail-closed가 우선한다: 플래그가 꺼져 있으면 검토 큐에도 올리지 않는다."""
    decision = apply_policy(
        _candidate(_agent_task_payload(llm_inferred=True, agent_task_enabled=False))
    )

    assert decision.outcome == "reject"


def test_mail_delegation_marks_the_candidate_as_llm_inferred() -> None:
    """배선 회귀: 메일 경로가 실제로 `llm_inferred=True`를 심는가.

    게이트를 아무리 잘 만들어도 메일 경로가 이 플래그를 안 심으면 무용지물이다.
    `orthus/mail/delegation.py`가 `create_agent_task_work_item(..., llm_inferred=True)`를
    부르는지 **소스 레벨**로 고정한다(그 함수는 DB/LLM을 타므로 여기서 호출하지 않는다).
    """
    import inspect

    from orthus.mail import delegation as mail_delegation

    src = inspect.getsource(mail_delegation)
    assert "llm_inferred=True" in src, (
        "메일發 위임 후보에 llm_inferred 표시가 사라졌다 — 게이트가 이걸 보고 검토를 요구한다."
    )


# ── 승인 → dispatch 경로 (execute_reviewed_agent_task) ────────────────────────
#
# 게이트가 draft_for_review로 보냈으니 dispatch는 **승인 시점**에 일어나야 한다. 안 그러면
# 후보가 큐에서 죽고 기능이 무용지물이 된다. 동시에 그 dispatch는 auto 경로보다 **더** 엄격해야
# 한다 — payload의 actor_role은 "scheduler"(기계)이고, 그건 절대 혼자서 충분하면 안 된다.


def _reviewed_item(**payload_overrides: object):
    from datetime import UTC, datetime
    from uuid import UUID

    from orthus.schemas.canonical import AgentWorkItem

    now = datetime.now(UTC)
    return AgentWorkItem(
        work_id=UUID("22222222-2222-2222-2222-222222222222"),
        node_id="company",
        node_kind="company",
        source_kind="agent_task",
        source_ref_id="agent-task-test",
        action_family="agent_task",
        title="위임",
        state="resolved",  # 승인 직후 상태
        outcome="draft_for_review",
        reason="reviewed",
        reason_codes=["agent_task_llm_inferred"],
        payload=_agent_task_payload(llm_inferred=True, **payload_overrides),
        evidence=[{"kind": "agent_task", "ref": "agent-task-test"}],
        created_at=now,
        updated_at=now,
    )


def test_reviewer_without_operator_authority_cannot_dispatch() -> None:
    """★ 기계(scheduler)가 큐에 넣은 것을 일반 member가 승인해 실행할 수 없다.

    payload의 `actor_role`은 "scheduler"라 auto 경로의 role 검사를 통과한다. 그래서 **리뷰어의**
    권한을 따로 본다 — 이걸 빼면 "사람 검토"가 형식만 남는다.
    """
    from orthus.agentwork import execute_reviewed_agent_task

    item = _reviewed_item()
    out = execute_reviewed_agent_task(
        item.work_id, item, reviewer_role="member", settings=None
    )
    assert out is item, "operator 권한 없는 리뷰어의 승인은 dispatch를 만들면 안 된다"


def test_already_dispatched_item_is_not_dispatched_twice() -> None:
    """멱등: dispatch는 되돌릴 수 없으므로 두 번째 호출은 no-op다."""
    from orthus.agentwork import execute_reviewed_agent_task

    item = _reviewed_item(auto_execution={"kind": "agent_task", "status": "dispatched"})
    out = execute_reviewed_agent_task(item.work_id, item, reviewer_role="owner", settings=None)
    assert out is item


def test_non_llm_inferred_item_is_not_dispatched_by_the_review_path() -> None:
    """이 경로는 **LLM 추론 위임 전용**이다. 다른 agent_task를 승인으로 재실행하지 않는다."""
    from orthus.agentwork import execute_reviewed_agent_task

    item = _reviewed_item()
    item = item.model_copy(
        update={"payload": {**item.payload, "llm_inferred": False}}
    )
    out = execute_reviewed_agent_task(item.work_id, item, reviewer_role="owner", settings=None)
    assert out is item
