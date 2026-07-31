"""C-7 잡 정의 central 미러 — owner-boundary 보안 회귀 (PUT /collector/gateway/jobs
+ GET /agent-work/gateway-jobs).

미러 push는 P10.4b collector-token 라우터의 가드 순서를 그대로 상속한다:
fail-closed flag(404) → agent_task scope(403) → 명시 owner/admin 역할 해석(403).
row는 token.user_id에만 바인딩되고(페이로드로 owner 위조 불가) full-replace는
그 owner의 row만 만진다. session 조회는 P8 fail-closed owner-only — admin도
타인 row를 보지 못한다.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.collector.auth import hash_collector_token
from orthus.collector.rate_limit import reset_collector_owner_rate_limits
from orthus.db import session
from orthus.secrets import clear_memory_secrets
from orthus.settings import get_settings
from orthus.tables import (
    agent_gateway_jobs,
    auth_allowlist,
    auth_identities,
    collector_tokens,
    users,
)

client = TestClient(app)
COMPANY_NODE = "company"


def _enable(monkeypatch, *, actions=True) -> None:
    reset_collector_owner_rate_limits()
    clear_memory_secrets()
    s = get_settings()
    monkeypatch.setattr(s, "collector_api_enabled", True)
    monkeypatch.setattr(s, "node_kind", "company")
    monkeypatch.setattr(s, "node_id", COMPANY_NODE)
    monkeypatch.setattr(s, "agent_gateway_actions_enabled", actions)
    monkeypatch.setattr(s, "secret_backend", "memory")


def _make_operator(role: str | None = "owner") -> uuid.UUID:
    uid = uuid.uuid4()
    email = f"{uid.hex[:8]}@example.com"
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Owner"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=uid,
                provider="google",
                provider_subject=uuid.uuid4().hex,
                email=email,
                email_verified=True,
            )
        )
        if role is not None:
            s.execute(
                insert(auth_allowlist).values(
                    allowlist_id=uuid.uuid4(),
                    node_id=COMPANY_NODE,
                    email=email,
                    role=role,
                )
            )
        s.commit()
    return uid


def _token(user_id: uuid.UUID, *, scopes: list[str] | None = None) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=COMPANY_NODE,
                name="daemon",
                token_hash=hash_collector_token(token),
                scopes=scopes if scopes is not None else ["agent_task", "commands"],
            )
        )
        s.commit()
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _job(job_id: str = "job1", **over) -> dict:
    d = {
        "job_id": job_id,
        "name": "공고 확인",
        "instruction": "새 공고 확인해줘",
        "schedule": {"kind": "daily", "hour": 9, "minute": 0},
        "schedule_human": "매일 09:00",
        "enabled": True,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "next_run_at": "2026-07-18T09:00:00+09:00",
    }
    d.update(over)
    return d


def _put_jobs(token: str, jobs: list[dict]):
    return client.put("/collector/gateway/jobs", json={"jobs": jobs}, headers=_hdr(token))


def _session_get(user_id: uuid.UUID):
    return client.get("/agent-work/gateway-jobs", headers={"X-User-Id": str(user_id)})


# --- flag / scope / role gates (P10.4b 가드 상속) -------------------------------


def test_flag_off_is_404(clean, monkeypatch):
    _enable(monkeypatch, actions=False)
    owner = _make_operator("owner")
    r = _put_jobs(_token(owner), [_job()])
    assert r.status_code == 404


def test_missing_agent_task_scope_is_403(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_operator("owner")
    r = _put_jobs(_token(owner, scopes=["commands"]), [_job()])
    assert r.status_code == 403


def test_non_operator_owner_is_403(clean, monkeypatch):
    """allowlist role이 없는 token owner → 403 (require_node_operator는 토큰에
    no-op이므로 명시 역할 해석이 실질 가드다)."""
    _enable(monkeypatch)
    non_op = _make_operator(role=None)
    r = _put_jobs(_token(non_op), [_job()])
    assert r.status_code == 403


def test_bad_token_is_401(clean, monkeypatch):
    _enable(monkeypatch)
    r = _put_jobs("dct_bogus", [_job()])
    assert r.status_code == 401


# --- owner boundary (heart) -----------------------------------------------------


def test_push_binds_owner_and_isolates_between_owners(clean, monkeypatch):
    """A push 후 B push — 각자 자기 row만 보이고 서로의 미러를 건드리지 못한다."""
    _enable(monkeypatch)
    owner_a = _make_operator("owner")
    owner_b = _make_operator("owner")
    tok_a, tok_b = _token(owner_a), _token(owner_b)

    r_a = _put_jobs(tok_a, [_job("a1"), _job("a2", name="리포트")])
    assert r_a.status_code == 200, r_a.text
    assert r_a.json() == {"mirrored": 2, "deleted": 0}

    r_b = _put_jobs(tok_b, [_job("b1")])
    assert r_b.status_code == 200, r_b.text
    # B의 full-replace는 A의 row를 삭제하지 않는다.
    assert r_b.json() == {"mirrored": 1, "deleted": 0}

    assert {j["job_id"] for j in _session_get(owner_a).json()} == {"a1", "a2"}
    assert {j["job_id"] for j in _session_get(owner_b).json()} == {"b1"}


def test_full_replace_deletes_removed_jobs_only_for_that_owner(clean, monkeypatch):
    _enable(monkeypatch)
    owner_a = _make_operator("owner")
    owner_b = _make_operator("owner")
    tok_a = _token(owner_a)
    _put_jobs(tok_a, [_job("a1"), _job("a2")])
    _put_jobs(_token(owner_b), [_job("b1")])

    # A가 a2를 지운 상태로 재push → a2만 삭제, B 불변.
    r = _put_jobs(tok_a, [_job("a1", last_status="ok")])
    assert r.status_code == 200
    assert r.json() == {"mirrored": 1, "deleted": 1}
    rows_a = _session_get(owner_a).json()
    assert [j["job_id"] for j in rows_a] == ["a1"]
    assert rows_a[0]["last_status"] == "ok"  # upsert가 상태를 갱신
    assert [j["job_id"] for j in _session_get(owner_b).json()] == ["b1"]

    # 빈 push = 그 owner의 전체 미러 삭제 (여전히 B 불변).
    r_empty = _put_jobs(tok_a, [])
    assert r_empty.status_code == 200
    assert r_empty.json() == {"mirrored": 0, "deleted": 1}
    assert _session_get(owner_a).json() == []
    assert [j["job_id"] for j in _session_get(owner_b).json()] == ["b1"]


def test_payload_cannot_override_owner(clean, monkeypatch):
    """페이로드에 owner_user_id를 실어도 무시된다 — row는 token.user_id에만
    바인딩된다 (P8 owner-only 경계)."""
    _enable(monkeypatch)
    owner_a = _make_operator("owner")
    owner_b = _make_operator("owner")
    entry = _job("a1")
    entry["owner_user_id"] = str(owner_b)  # 위조 시도 — pydantic이 버린다
    r = _put_jobs(_token(owner_a), [entry])
    assert r.status_code == 200, r.text
    with session() as s:
        rows = s.execute(select(agent_gateway_jobs.c.owner_user_id)).all()
    assert [row.owner_user_id for row in rows] == [owner_a]
    assert _session_get(owner_b).json() == []


def test_session_get_is_owner_only_even_for_admin(clean, monkeypatch):
    """P8 fail-closed: admin 세션도 타인 미러 row를 보지 못한다 (자기 row만)."""
    _enable(monkeypatch)
    owner_a = _make_operator("owner")
    admin = _make_operator("admin")
    _put_jobs(_token(owner_a), [_job("a1")])
    assert _session_get(admin).json() == []
    # 자기 것은 보인다.
    _put_jobs(_token(admin), [_job("adm1")])
    assert [j["job_id"] for j in _session_get(admin).json()] == ["adm1"]
    # A의 조회는 여전히 자기 것만.
    assert [j["job_id"] for j in _session_get(owner_a).json()] == ["a1"]


def test_seen_set_style_fields_are_not_accepted_silently_as_rows(clean, monkeypatch):
    """미러 스키마엔 seen-set/chat_id 컬럼 자체가 없다 — 보내도 저장될 곳이
    없음을 고정(스키마 회귀 가드)."""
    _enable(monkeypatch)
    assert "chat_id" not in agent_gateway_jobs.c
    assert "seen_keys" not in agent_gateway_jobs.c
    owner = _make_operator("owner")
    entry = _job("a1")
    entry["chat_id"] = 777
    entry["seen_keys"] = ["https://example.com/post/1"]
    r = _put_jobs(_token(owner), [entry])
    assert r.status_code == 200
    row = _session_get(owner).json()[0]
    assert "chat_id" not in row and "seen_keys" not in row
