"""author_from_qa가 claim.display_title를 결정론 폴백으로 채우고 LLM은 부르지 않음.

DB 불필요 — consolidate를 monkeypatch로 가로채 넘어온 WikiClaim만 검사한다.
author 계층은 Fully deterministic(LLM 미호출)이라 chat_model이 있어도 호출되면 안 된다.
"""

from __future__ import annotations

from uuid import uuid4

from orthus.wiki import author as author_mod


class _ExplodingChat:
    model_id = "must-not-be-called"

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:  # noqa: D401
        raise AssertionError("author_from_qa must not call the LLM")


def test_author_from_qa_sets_deterministic_headline(monkeypatch):
    captured = {}

    def _fake_consolidate(source, claims, **kwargs):
        captured["claims"] = claims
        return ([], [])

    monkeypatch.setattr(author_mod, "consolidate", _fake_consolidate)

    author_mod.author_from_qa(
        uuid4(),
        "nova 주간회고와 충돌하는 주장 보여줘",
        "주간 회고 문서에서 계획 및 회고 항목이 모두 미입력 상태다.",
        [],
        chat_model=_ExplodingChat(),  # 호출되면 AssertionError
    )

    claim = captured["claims"][0]
    assert claim.display_title
    # Q:/A: 접두 없이 사람이 읽는 한 줄.
    assert not claim.display_title.lower().startswith("q:")
    assert "\n" not in claim.display_title
    assert len(claim.display_title) <= 120
