"""K7.4 §6 (merge-blocking): collector-token `/ask`가 `$caller`를 **토큰 소유자**에 바인딩.

knowledge-scope collector token으로 `/ask`를 치면, 다운스트림 grounding/graph/missing_link
전체가 받는 user_id(`$caller`)는 **토큰에 묶인 단일 owner**여야 한다 — 요청 본문이나 hit에서
유도된 어떤 값도 아니다. 이 가드는 K7.2 gate boundary matrix가 못 잡는 `/ask` 배선 층을 고정한다
(plan §6 — gate가 아니라 route→answer 경계의 caller 바인딩). 라우트가 `answer(user_id, ...)`로
넘기는 인자를 캡처해 PG-plane에서만 검증한다.

**neo4j 불필요(§6 G3 핀):** `answer`를 통째로 monkeypatch하므로 neo4j/wiki 시드 없이 도는
표준 통합 lane에서 그대로 실행된다 — leak/owner-binding 방어가 neo4j 부재로 skip되어 no-op로
증발하지 않는다. 토큰→user_id가 single-owner로 resolve 안 되면(철회/무효) loudly fail(401,
`answer` 미호출).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from orthus.api.knowledge_token import reset_token_ask_rate_limit
from orthus.api.main import app
from orthus.collector.auth import hash_collector_token
from orthus.db import session
from orthus.schemas.canonical import RoutedAnswer
from orthus.settings import get_settings
from orthus.tables import collector_tokens, users

client = TestClient(app)
COMPANY_NODE = "company"

# 토큰일 때 intake가 건너뛰므로 명령형이 아니어도 됨 — 순수 지식 질문.
_QUESTION = "A 프로젝트와 B 프로젝트는 어떻게 연결돼 있어?"


@pytest.fixture
def _company_owner_scope(monkeypatch):
    """company node + owner-scope ON(K7.4 실배포 형태) + 토큰 rate-limit 초기화."""
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    reset_token_ask_rate_limit()


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def _make_token(user_id: uuid.UUID) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=COMPANY_NODE,
                name="k7-4-caller-binding",
                token_hash=hash_collector_token(token),
                scopes=["knowledge"],
            )
        )
        s.commit()
    return token


def _revoke(token: str) -> None:
    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_hash == hash_collector_token(token))
            .values(revoked_at=datetime.now(UTC))
        )
        s.commit()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _capture_answer(monkeypatch) -> dict:
    """route가 answer(...)에 넘기는 user_id($caller)를 캡처. 실제 grounding/graph는 우회."""
    captured: dict = {}

    def _fake_answer(
        user_id,
        question,
        *,
        scope=None,
        project=None,
        context_wiki_slug=None,
        history=None,
        **_kwargs,
    ):
        captured["user_id"] = user_id
        captured["scope"] = scope
        return RoutedAnswer(question=question, mode="wiki")

    monkeypatch.setattr("orthus.api.routes.ask.answer", _fake_answer)
    return captured


def test_collector_token_ask_binds_caller_to_token_owner(_company_owner_scope, monkeypatch):
    owner = _make_user()
    token = _make_token(owner)
    captured = _capture_answer(monkeypatch)

    res = client.post("/ask", json={"question": _QUESTION}, headers=_headers(token))

    assert res.status_code == 200, res.text
    # $caller == 토큰 소유자(요청/hit 유도 아님). 이 user_id가 곧 graph/missing_link owner-variant의
    # caller가 되어 "그 owner의 노트만" 노출하는 경계를 만든다.
    assert captured["user_id"] == owner


def test_collector_token_ask_caller_isolated_per_owner(_company_owner_scope, monkeypatch):
    owner_a = _make_user()
    owner_b = _make_user()
    token_a = _make_token(owner_a)
    token_b = _make_token(owner_b)
    captured = _capture_answer(monkeypatch)

    client.post("/ask", json={"question": _QUESTION}, headers=_headers(token_a))
    assert captured["user_id"] == owner_a

    # B 토큰은 B로만 resolve — A의 caller로 교차 바인딩되지 않는다(owner 격리).
    client.post("/ask", json={"question": _QUESTION}, headers=_headers(token_b))
    assert captured["user_id"] == owner_b
    assert captured["user_id"] != owner_a


def test_revoked_collector_token_ask_loud_fails_401_no_answer(_company_owner_scope, monkeypatch):
    owner = _make_user()
    token = _make_token(owner)
    _revoke(token)
    captured = _capture_answer(monkeypatch)

    res = client.post("/ask", json={"question": _QUESTION}, headers=_headers(token))

    # single-owner로 resolve 안 되면 loudly fail — answer는 호출되지 않아 어떤 caller도 바인딩 안 됨.
    assert res.status_code == 401, res.text
    assert "user_id" not in captured
