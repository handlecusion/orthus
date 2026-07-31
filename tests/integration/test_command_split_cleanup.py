"""C8 저severity cleanup 회귀 (docs/command-split-next-steps.md §C).

- 빈 `?ids=` 배치 폴은 명시적 빈 응답이다 — in_([])로 DB까지 가는 항상-거짓 쿼리도,
  no-filter 전체 목록 오독도 아니다.
- `has_strong_command`는 명시적 위임 프리픽스를 detect_assistant_command_action과
  같은 우선순위로 strong 취급한다 — weak family 조합이 LLM split로 조각나며 위임
  프리픽스가 소실되는 경로 차단.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.agentwork.service import has_strong_command
from orthus.api.main import app
from orthus.db import session
from orthus.tables import agent_work_items

client = TestClient(app)


def _seed_item(user_id: uuid.UUID) -> uuid.UUID:
    work_id = uuid.uuid4()
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id="company",
                node_kind="company",
                owner_id=None,
                source_kind="assistant_command",
                source_ref_id=f"assistant-command-{work_id.hex[:16]}",
                action_family="email_send",
                title="Assistant command: t",
                payload={"question": "t"},
                state="draft_for_review",
                policy_outcome="draft_for_review",
                policy_reason="r",
                reason_codes=[],
                evidence=[],
                correlation_id=uuid.uuid4(),
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
    return work_id


def test_empty_ids_batch_poll_returns_empty_not_full_list(clean, user_id):
    _seed_item(user_id)
    headers = {"X-User-Id": str(user_id)}

    # 빈 ids= → [] (전체 목록이 아님).
    empty = client.get("/agent-work?ids=", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == []
    # 콤마/공백만 있어도 동일.
    blank = client.get("/agent-work?ids=,%20,", headers=headers)
    assert blank.status_code == 200
    assert blank.json() == []
    # ids 미지정은 여전히 no-filter 전체 목록.
    full = client.get("/agent-work", headers=headers)
    assert full.status_code == 200
    assert len(full.json()) == 1


def test_has_strong_command_treats_delegation_prefix_as_strong():
    # weak family(document) 조합이라도 위임 프리픽스면 strong — split 금지.
    assert has_strong_command("위임: 문서 초안 만들어줘 그리고 휴가정책 알려줘") is True
    assert has_strong_command("위임: 회고 정리해줘") is True
    # 프리픽스 없는 weak family는 기존대로 strong 아님.
    assert has_strong_command("문서 초안 만들어줘 그리고 휴가정책 알려줘") is False
