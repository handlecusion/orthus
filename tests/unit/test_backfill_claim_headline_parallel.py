from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from uuid import UUID

from orthus.wiki import backfill_claim_headline as mod


@dataclass
class _FakeClaim:
    claim: str
    display_title: str | None = None

    def model_copy(self, *, update):
        return _FakeClaim(claim=self.claim, display_title=update["display_title"])


class _SlowChat:
    model_id = "slow-test"

    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        del system, json_only
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.05)
            return f"headline for {user}"
        finally:
            with self._lock:
                self._active -= 1


def test_backfill_claim_headlines_parallelizes_llm_but_writes_in_order(monkeypatch):
    claims = {
        "a": _FakeClaim("claim-a"),
        "b": _FakeClaim("claim-b"),
        "c": _FakeClaim("claim-c"),
        "d": _FakeClaim("claim-d"),
    }
    chat = _SlowChat()
    written: list[str] = []

    monkeypatch.setattr(mod.store, "list_slugs", lambda kind, scope, owner_id: list(claims))
    monkeypatch.setattr(
        mod.store,
        "load_claim",
        lambda slug, scope, owner_id: claims[slug],
    )

    def _write_claim(claim, *, user_id, scope, owner_id):
        del user_id, scope, owner_id
        written.append(claim.claim)

    monkeypatch.setattr(mod.store, "write_claim", _write_claim)
    # 헤드라인은 배정표를 거쳐 모델을 받는다(docs/model-orchestration.md §12).
    monkeypatch.setattr(mod, "get_chat_model_for", lambda _task: chat)

    report = mod.backfill_claim_headlines(
        user_id=UUID("00000000-0000-4000-8000-000000000001"),
        concurrency=2,
    )

    assert report.scanned == 4
    assert report.updated == 4
    assert report.errors == 0
    assert chat.max_active == 2
    assert written == ["claim-a", "claim-b", "claim-c", "claim-d"]


def test_backfill_claim_headlines_no_llm_does_not_request_chat_model(monkeypatch):
    claims = {"a": _FakeClaim("Q: sufficiently long claim-a. More text.")}
    written: list[_FakeClaim] = []

    monkeypatch.setattr(mod.store, "list_slugs", lambda kind, scope, owner_id: list(claims))
    monkeypatch.setattr(mod.store, "load_claim", lambda slug, scope, owner_id: claims[slug])
    monkeypatch.setattr(
        mod.store,
        "write_claim",
        lambda claim, *, user_id, scope, owner_id: written.append(claim),
    )
    monkeypatch.setattr(
        mod,
        "get_chat_model_for",
        lambda _task: (_ for _ in ()).throw(AssertionError("chat model should not be loaded")),
    )

    report = mod.backfill_claim_headlines(
        user_id=UUID("00000000-0000-4000-8000-000000000001"),
        use_llm=False,
        concurrency=8,
    )

    assert report.updated == 1
    assert report.fallback == 1
    assert written[0].display_title == "sufficiently long claim-a."
