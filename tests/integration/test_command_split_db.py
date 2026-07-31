"""B4 command-split real-DB integration (docs/command-split-next-steps.md §B4).

유닛 스위트(tests/unit/test_command_split_review.py)는 session()/persist를 mock으로
가드까지만 검증했다. 여기서는 실제 Postgres(orthus_test)에 대해 리뷰 수정 4종의
**영속 진실**을 봉인한다:

- #5/#6 `_hold_command_split_auto_execute`가 state와 policy_outcome을 함께
  request_more_data로 재영속(dual-truth 제거)하고 원 판정은
  `payload.command_split_held_outcome`에 보존한다.
- #2 approve 결정이 `resume_held_command_split_action`으로 원 typed 실행을 재개하고
  결과 상태를 다시 영속한다(dismiss는 재개하지 않는다).
- #1 fall-through(`leaf_index=None`)의 `:f` digest가 평문 단일 명령 intake와 실제
  persist on_conflict에서 격리된다(hold-우회 차단).
- #8 non-command-split email intake는 queue 시점 eager 초안을 유지하고(byte-identical),
  command-split fragment만 lazy-on-review다(S5).
- #7 federated 노드의 복합 입력은 명령만 큐잉하고 경고를 남긴다.

네트워크 경계인 `run_connector_account_sync`만 스텁하고(외부 API 호출 방지),
classify/persist/hold/decision/resume 경로는 전부 실 DB로 통과시킨다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.agentwork import create_assistant_command_work_item
from orthus.api.deps import get_current_user
from orthus.api.main import app
from orthus.api.routes import agent_work as agent_work_routes
from orthus.auth import AuthenticatedUser
from orthus.connectors.runner import ConnectorRunResult
from orthus.db import session
from orthus.schemas.canonical import RoutedAnswer
from orthus.settings import get_settings
from orthus.tables import agent_work_decisions, agent_work_items, connector_accounts, connector_runs

client = TestClient(app)

_SLACK_COMMAND = "Slack 동기화해줘"


@pytest.fixture
def flags():
    """command-split 관련 플래그를 테스트별 명시 상태로 두고 원복한다.

    `clean`은 이 플래그들을 리셋하지 않으므로(로컬 .env가 decompose를 켜 둔 환경 존재)
    orchestrate 경로를 타는 테스트는 반드시 이 fixture로 상태를 고정해야 한다.
    """
    settings = get_settings()
    original = (
        settings.ask_decompose_enabled,
        settings.ask_command_split_enabled,
        settings.ask_decompose_command_guard,
        # federated 테스트가 personal 노드로 바꾸므로 노드 정체성도 복원한다
        # (operator review — clean 없이 도는 후속 테스트 오염 방지).
        settings.node_kind,
        settings.node_id,
    )
    settings.ask_decompose_enabled = False
    settings.ask_command_split_enabled = False
    settings.ask_decompose_command_guard = True
    yield settings
    (
        settings.ask_decompose_enabled,
        settings.ask_command_split_enabled,
        settings.ask_decompose_command_guard,
        settings.node_kind,
        settings.node_id,
    ) = original


def _seed_slack_account() -> uuid.UUID:
    account_id = uuid.uuid4()
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug="slack",
                account_kind="company",
                node_id="company",
                scope="company",
                owner_id=None,
                auth_mode="token",
                account_label="Slack",
                status="active",
                settings_redacted={"secret_refs": {"token": "keychain://token"}},
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
    return account_id


def _held_connector_item(user_id: uuid.UUID, *, leaf_index: int | None):
    """configured active 계정 + owner actor로 gate 원판정 auto_execute를 만든 뒤
    command_split_origin=True로 hold까지 통과한 item을 돌려준다."""
    item = create_assistant_command_work_item(
        user_id,
        _SLACK_COMMAND,
        actor_role="owner",
        command_split_origin=True,
        leaf_index=leaf_index,
    )
    assert item is not None
    return item


def _db_row(work_id: uuid.UUID):
    with session() as s:
        row = s.execute(
            select(agent_work_items).where(agent_work_items.c.work_id == work_id)
        ).first()
    assert row is not None
    return row._mapping


def _session_owner_override(user_id: uuid.UUID):
    def _owner():
        return AuthenticatedUser(
            user_id=user_id,
            auth_mode="session",
            display_name="Owner",
            email="owner@example.com",
            role="owner",
            node_id=get_settings().node_id,
        )

    return _owner


def _succeeded_run_stub(account_id: uuid.UUID, calls: list):
    def _stub(acct_id, user_id, **kwargs):
        calls.append({"account_id": acct_id, "user_id": user_id, **kwargs})
        return ConnectorRunResult(
            account_id=acct_id,
            connector_slug="slack",
            run_id=uuid.uuid4(),
            status="succeeded",
            report=None,
        )

    return _stub


# ── #5/#6 hold 재영속: state=policy_outcome=request_more_data + 원 판정 보존 ──


def test_hold_re_persists_request_more_data_without_dual_truth(clean, user_id):
    _seed_slack_account()

    item = _held_connector_item(user_id, leaf_index=0)

    # 반환값과 DB 영속 진실이 모두 held 형태여야 한다(dual-truth 금지).
    assert item.state == "request_more_data"
    assert item.policy_outcome == "request_more_data"
    assert "command_split_hold" in item.reason_codes

    row = _db_row(item.work_id)
    assert row["state"] == "request_more_data"
    assert row["policy_outcome"] == "request_more_data"
    assert "command_split_hold" in row["reason_codes"]
    assert row["payload"]["command_split_hold"] is True
    # 게이트 원 판정은 payload에만 보존된다(approve 시 재개용).
    assert row["payload"]["command_split_held_outcome"] == "auto_execute"

    # hold는 실행 유보다 — queue 시점에 커넥터 run이 생기면 안 된다.
    with session() as s:
        runs = s.execute(select(connector_runs.c.run_id)).all()
    assert runs == []


# ── #2 approve → resume_held_command_split_action 실제 재개 + 상태 재영속 ──


def test_approve_resumes_held_connector_sync_and_re_persists(clean, user_id, monkeypatch):
    account_id = _seed_slack_account()
    item = _held_connector_item(user_id, leaf_index=1)
    calls: list = []
    monkeypatch.setattr(
        "orthus.agentwork.service.run_connector_account_sync",
        _succeeded_run_stub(account_id, calls),
    )

    app.dependency_overrides[get_current_user] = _session_owner_override(user_id)
    try:
        decided = client.post(
            f"/agent-work/{item.work_id}/decision",
            json={"decision": "approve"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert decided.status_code == 200, decided.text
    # 유보된 typed 실행이 approve 시점에 실제로 재개됐다.
    assert len(calls) == 1
    assert calls[0]["account_id"] == account_id

    body = decided.json()["item"]
    assert body["state"] == "resolved"
    assert body["payload"]["auto_execution"]["kind"] == "connector_sync"
    assert body["payload"]["auto_execution"]["status"] == "succeeded"
    assert body["payload"]["auto_execution"]["account_id"] == str(account_id)

    # 실행 결과가 DB에 재영속됐다(응답 in-memory가 아니라 row 진실).
    row = _db_row(item.work_id)
    assert row["state"] == "resolved"
    assert row["payload"]["auto_execution"]["status"] == "succeeded"

    with session() as s:
        decisions = s.execute(
            select(agent_work_decisions.c.decision).where(
                agent_work_decisions.c.work_id == item.work_id
            )
        ).all()
    assert [d.decision for d in decisions] == ["approve"]


def test_dismiss_does_not_resume_held_action(clean, user_id, monkeypatch):
    account_id = _seed_slack_account()
    item = _held_connector_item(user_id, leaf_index=0)
    calls: list = []
    monkeypatch.setattr(
        "orthus.agentwork.service.run_connector_account_sync",
        _succeeded_run_stub(account_id, calls),
    )

    app.dependency_overrides[get_current_user] = _session_owner_override(user_id)
    try:
        decided = client.post(
            f"/agent-work/{item.work_id}/decision",
            json={"decision": "dismiss"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert decided.status_code == 200, decided.text
    assert calls == []
    row = _db_row(item.work_id)
    assert row["state"] == "dismissed"
    assert "auto_execution" not in row["payload"]


# ── #1 fall-through digest 격리: 평문 intake의 on_conflict가 held row를 못 건드린다 ──


def test_fallthrough_digest_isolated_from_plain_intake(clean, user_id, monkeypatch):
    account_id = _seed_slack_account()
    calls: list = []
    monkeypatch.setattr(
        "orthus.agentwork.service.run_connector_account_sync",
        _succeeded_run_stub(account_id, calls),
    )

    # 1) split 실패 fall-through(leaf_index=None) → held row A (":f" digest).
    held = _held_connector_item(user_id, leaf_index=None)
    assert held.payload["command_split_held_outcome"] == "auto_execute"

    # 2) 같은 텍스트의 평문 단일 명령 intake(auto_execute → 즉시 실행). review #1의
    #    hold-우회 시나리오 그대로: 이 upsert가 held row를 auto_execute로 되돌리면 안 된다.
    plain = create_assistant_command_work_item(
        user_id,
        _SLACK_COMMAND,
        actor_role="owner",
        command_split_origin=False,
    )
    assert plain is not None
    assert plain.work_id != held.work_id
    assert plain.source_ref_id != held.source_ref_id
    # 평문 쪽만 실행됐다.
    assert len(calls) == 1
    assert plain.state == "resolved"
    assert plain.payload["auto_execution"]["status"] == "succeeded"

    # held row A는 그대로 유보 상태다(덮어쓰기/실행 없음).
    row = _db_row(held.work_id)
    assert row["state"] == "request_more_data"
    assert row["policy_outcome"] == "request_more_data"
    assert row["payload"]["command_split_hold"] is True
    assert "auto_execution" not in row["payload"]

    # 3) fan-out leaf(":0")는 fall-through(":f")와도, 평문과도 다른 row다.
    leaf = _held_connector_item(user_id, leaf_index=0)
    ref_ids = {held.source_ref_id, plain.source_ref_id, leaf.source_ref_id}
    assert len(ref_ids) == 3

    # 4) fall-through 재시도는 ":f" 안정 토큰으로 row A에 멱등 upsert된다(새 row 없음).
    again = _held_connector_item(user_id, leaf_index=None)
    assert again.work_id == held.work_id
    with session() as s:
        count = s.execute(select(agent_work_items.c.work_id)).all()
    assert len(count) == 3


# ── #8 non-split email eager 유지 + command-split fragment만 lazy(S5) ──


def test_non_split_email_draft_stays_eager_split_fragment_lazy(clean, user_id, flags):
    # flag-off(=prod default) 평문 intake: queue 시점에 초안이 이미 있어야 한다
    # (byte-identical — S5 lazy는 command-split 조각 한정).
    headers = {"X-User-Id": str(user_id)}
    created = client.post("/agent-work/chats", json={}, headers=headers)
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    asked = client.post(
        f"/agent-work/chats/{sid}/orchestrate",
        json={"text": "john에게 이메일 초안 만들어줘"},
        headers=headers,
    )
    assert asked.status_code == 200, asked.text
    eager_id = uuid.UUID(asked.json()["agent_work"]["work_id"])
    eager_row = _db_row(eager_id)
    assert eager_row["state"] == "draft_for_review"
    # GET 없이 DB만 봐도 초안이 존재해야 eager다.
    assert isinstance(eager_row["payload"].get("email_draft"), dict)

    # command-split fragment: queue 시점 초안 없음(lazy) → 리뷰 GET에서 생성/영속.
    fragment = create_assistant_command_work_item(
        user_id,
        "jane에게 이메일 초안 만들어줘",
        actor_role="owner",
        command_split_origin=True,
        leaf_index=0,
    )
    assert fragment is not None
    assert fragment.state == "draft_for_review"
    lazy_row = _db_row(fragment.work_id)
    assert "email_draft" not in lazy_row["payload"]

    opened = client.get(f"/agent-work/{fragment.work_id}", headers=headers)
    assert opened.status_code == 200, opened.text
    assert isinstance(opened.json()["payload"].get("email_draft"), dict)
    # lazy ensure는 영속까지 한다(응답 전용이 아님).
    assert isinstance(_db_row(fragment.work_id)["payload"].get("email_draft"), dict)


# ── #7 federated 노드 복합 입력: 명령만 큐잉 + 질문 미답 경고 ──


def test_federated_compound_queues_command_with_warning(clean, user_id, flags):
    settings = flags
    settings.ask_decompose_enabled = True
    settings.ask_command_split_enabled = True
    settings.node_kind = "personal"
    settings.node_id = "personal-test"

    app.dependency_overrides[get_current_user] = _session_owner_override(user_id)
    try:
        created = client.post("/agent-work/chats", json={})
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        asked = client.post(
            f"/agent-work/chats/{sid}/orchestrate",
            # personal node 기본 scope=all → should_federate → compound_split 강제 False.
            json={"text": "gmail 동기화해줘 그리고 휴가정책 알려줘"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert asked.status_code == 200, asked.text
    body = asked.json()
    assert body["mode"] == "agent_work"
    assert "federated_compound_question_not_answered" in body["warnings"]
    assert "질문 부분은 답변하지 못했습니다" in body["agent_work"]["message"]

    # 명령은 whole-text로 큐잉됐다(질문 소실은 경고로 가시화).
    row = _db_row(uuid.UUID(body["agent_work"]["work_id"]))
    assert row["action_family"] == "connector_sync"
    assert row["node_id"] == "personal-test"


# ---------------------------------------------------------------------------
# SVC — command-preservation net vs read→act 게이트 (실서버 QA 회귀)
#
# 실서버(company, 실코퍼스)에서 발견: 게이트가 가짜 0 위의 명령 리프를 정상 보류
# (`upstream_unavailable`)했는데, net이 "어떤 part에도 agent_work_id가 없다"만 보고
# 명령을 통째로 다시 큐잉했다. FE는 "보류"를 보여주는데 큐에는 item이 존재하는 상태
# 모순이다. net은 **명령이 조용히 사라졌을 때만** 발동해야 한다.
# ---------------------------------------------------------------------------

_QA_TEXT = "NOVA 영입 후보 리스트에 총 몇 명이야? 그리고 그 인원수를 qa-owner@acme.example 로 메일 보내줘"
_QA_COMMAND_LEAF = "그 인원수를 qa-owner@acme.example 로 메일 보내줘"


def _zero_answer_decomposed(command_leaf_text: str | None) -> RoutedAnswer:
    """실측 그대로의 응답: part0 = 가짜 0(grounded), part1 = 게이트가 보류한 명령 리프."""
    from orthus.schemas.canonical import AssistantResult, SubAnswer, ValidationResult

    zero = AssistantResult(
        query_id=uuid.uuid4(),
        question="NOVA 영입 후보 리스트에 총 몇 명이야?",
        source_id=None,
        compiled=None,
        validation=ValidationResult(
            parsed=True,
            statement_kind="select",
            schema_ok=True,
            read_only_ok=True,
            explain_ok=True,
        ),
        status="executed",
        columns=["count"],
        rows=[[0]],  # ← 가짜 0 (정답 19)
        row_count=1,
    )
    part0 = SubAnswer(
        sub_question_id=uuid.uuid4(),
        routed=RoutedAnswer(question=zero.question, mode="structured", structured=zero),
        grounded=True,  # zero-answer는 게이트 통과 + executed라 grounded다
        text=zero.question,
    )
    parts = [part0]
    if command_leaf_text is not None:
        parts.append(
            SubAnswer(
                sub_question_id=uuid.uuid4(),
                grounded=False,
                error="upstream_unavailable",  # ← read→act 게이트가 보류
                text=command_leaf_text,
                depends_on=[0],
            )
        )
    return RoutedAnswer(
        question=_QA_TEXT,
        mode="decomposed",
        parts=parts,
        warnings=["sub_answer_error:upstream_unavailable"] if command_leaf_text else [],
    )


def _agent_work_count() -> int:
    with session() as s:
        return len(s.execute(select(agent_work_items.c.work_id)).all())


def _orchestrate(user_id: uuid.UUID, text: str) -> dict:
    app.dependency_overrides[get_current_user] = _session_owner_override(user_id)
    try:
        created = client.post("/agent-work/chats", json={})
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        asked = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": text})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert asked.status_code == 200, asked.text
    return asked.json()


def _split_flags(flags):
    flags.ask_decompose_enabled = True
    flags.ask_command_split_enabled = True
    # net은 guard-off 경로에서만 도달 가능하다(guard on → take_535가 먼저 반환).
    flags.ask_decompose_command_guard = False
    return flags


def test_gate_held_command_is_not_requeued_by_the_net(clean, user_id, flags, monkeypatch):
    """★ 게이트가 보류한 명령을 net이 되살리지 않는다 — AgentWork가 늘지 않는다."""
    _split_flags(flags)
    monkeypatch.setattr(
        agent_work_routes, "answer", lambda *a, **kw: _zero_answer_decomposed(_QA_COMMAND_LEAF)
    )

    before = _agent_work_count()
    body = _orchestrate(user_id, _QA_TEXT)

    assert body["mode"] == "decomposed"
    assert _agent_work_count() == before  # ← 큐잉 없음 (실서버에서는 31 → 32였다)
    assert "command_held_unreliable_upstream" in body["warnings"]
    assert "command_split_command_recovered" not in body["warnings"]
    # FE 상태와 큐 상태가 일치한다: 명령 part는 보류, item 없음.
    assert body["parts"][1]["error"] == "upstream_unavailable"
    assert body["parts"][1]["agent_work_id"] is None


def test_net_still_recovers_a_genuinely_lost_command(clean, user_id, flags, monkeypatch):
    """과잉 억제 방지: 명령이 **정말로** 사라졌으면(보류 리프 없음) net은 그대로 발동한다."""
    _split_flags(flags)
    monkeypatch.setattr(agent_work_routes, "answer", lambda *a, **kw: _zero_answer_decomposed(None))

    before = _agent_work_count()
    body = _orchestrate(user_id, _QA_TEXT)

    assert _agent_work_count() == before + 1  # 복구 큐잉
    assert "command_split_command_recovered" in body["warnings"]
    assert "command_held_unreliable_upstream" not in body["warnings"]


def test_read_leaf_upstream_unavailable_does_not_suppress_the_net(
    clean, user_id, flags, monkeypatch
):
    """`upstream_unavailable`만 보고 억제하면 과도하다 — 명령과 무관한 **읽기** 리프가
    보류된 경우에는 net이 계속 발동해야 한다(명령은 실제로 소실됐으므로)."""
    _split_flags(flags)
    monkeypatch.setattr(
        agent_work_routes,
        "answer",
        lambda *a, **kw: _zero_answer_decomposed("그럼 그 후보 목록도 알려줘"),  # 읽기 리프
    )

    before = _agent_work_count()
    body = _orchestrate(user_id, _QA_TEXT)

    assert _agent_work_count() == before + 1
    assert "command_split_command_recovered" in body["warnings"]
