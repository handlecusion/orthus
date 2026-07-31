"""command-split read→act 후속 수정 회귀 (A/B).

- A: 이메일 초안 생성기가 decompose upstream 컨텍스트(payload.context.upstream)를
  프롬프트/폴백에 반영한다("이 내용을 기반으로 …메일" grounding).
- B: fan_out의 outer as_completed 타임아웃이 전체를 500내지 않고 미완료 leaf만
  timeout으로 degrade한다.

C(orchestrate guard-off 명령 보존 안전망)는 정책 게이트 route + DB 경로라 통합 검증 대상.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import orthus.agentwork.service as svc
import orthus.router.decompose as d
from orthus.schemas.canonical import SubAnswer, SubQuestion


# ── A: 이메일 초안이 upstream 컨텍스트를 사용 ──────────────────────────────────


class TestEmailDraftUsesUpstream:
    def _capture_prompt(self, upstream):
        captured = {}

        class _Chat:
            def complete(self, system, user, json_only=False):
                captured["user"] = user
                return '{"subject":"S","body":"B"}'

        # `get_chat_model_for(task)`, not `get_chat_model()` — the email draft now resolves
        # through the assignment table (docs/model-orchestration.md §12).
        with patch.object(svc, "get_chat_model_for", return_value=_Chat()):
            svc._generate_command_email("김과장", "이 내용을 기반으로 메일 써줘", upstream)
        return captured["user"]

    def test_upstream_injected_into_prompt(self):
        user = self._capture_prompt("휴가 정책은 위키에 없음(데이터 공백)")
        assert "[참고 자료 —" in user  # 컨텍스트 블록 헤더
        assert "휴가 정책은 위키에 없음(데이터 공백)" in user

    def test_no_upstream_no_context_block(self):
        user = self._capture_prompt(None)
        assert "[참고 자료 —" not in user  # 블록 없음(정적 지시문의 '참고 자료가 있으면'과 구분)

    def test_fallback_body_includes_upstream(self):
        body = svc._email_body_fallback("김과장", "메일 써줘", "연차 정책 요약")
        assert "참고 내용: 연차 정책 요약" in body

    def test_fallback_body_no_upstream(self):
        body = svc._email_body_fallback("김과장", "메일 써줘", None)
        assert "참고 내용" not in body


class TestEnsureEmailDraftReadsContext:
    def test_ensure_passes_payload_context_upstream(self):
        # ensure_email_draft_for_work_item가 payload.context.upstream을 _generate_command_email에
        # 전달하는지 — 생성기를 stub해 인자를 캡처하고 DB 쓰기는 우회.
        captured = {}

        def _fake_gen(recipient, instruction, upstream=None):
            captured["upstream"] = upstream
            return ("S", "B", True)

        item = SimpleNamespace(
            action_family="email_send",
            state="draft_for_review",
            title="t",
            work_id=uuid4(),
            payload={
                "recipient_hint": "김과장",
                "question": "이 내용을 기반으로 메일 써줘",
                "context": {"upstream": "휴가 정책 없음"},
            },
        )
        with (
            patch.object(svc, "_generate_command_email", side_effect=_fake_gen),
            patch.object(svc, "_normalise_recipient_hint", return_value="김과장"),
            patch.object(svc, "session", side_effect=AssertionError("DB write reached")),
        ):
            try:
                svc.ensure_email_draft_for_work_item(uuid4(), item)
            except AssertionError:
                pass  # DB write guard hit AFTER the generator call — that's fine, we captured
        assert captured.get("upstream") == "휴가 정책 없음"


# ── B: fan_out outer 타임아웃 → 부분결과 degrade ──────────────────────────────


class TestFanOutOuterTimeout:
    def test_slow_leaf_degrades_not_raises(self):
        release = threading.Event()
        fast = SubQuestion(id=uuid4(), text="fast", scope="company")
        slow = SubQuestion(id=uuid4(), text="slow", scope="company")

        def _fake_leaf(sq, **kw):
            if sq.text == "slow":
                release.wait(timeout=5)  # blocks past the outer budget
                return SubAnswer(sub_question_id=sq.id, grounded=True)
            return SubAnswer(sub_question_id=sq.id, grounded=True)

        try:
            with patch.object(d, "_run_leaf", side_effect=_fake_leaf):
                t0 = time.monotonic()
                # outer budget = leaf_timeout_sec * len(sub) = 1 * 2 = 2s
                out = d.fan_out(
                    [fast, slow],
                    user_id=uuid4(),
                    scope="company",
                    project=None,
                    chat_model=None,
                    context_wiki_slug=None,
                    actor_role="owner",
                    allow_agentic_in_leaf=False,
                    stream_key=None,
                    max_concurrency=2,
                    leaf_timeout_sec=1,
                )
                elapsed = time.monotonic() - t0
        finally:
            release.set()  # let the background thread exit

        assert len(out) == 2
        # returned promptly (did not wait for the 5s straggler)
        assert elapsed < 4
        by_id = {sa.sub_question_id: sa for sa in out}
        assert by_id[fast.id].error is None
        assert by_id[slow.id].error == "timeout"
