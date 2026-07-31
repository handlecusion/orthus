from __future__ import annotations

import uuid

import pytest

from orthus.wiki.distill import (
    _MAX_CLAIMS,
    _SYSTEM,
    _coerce_confidence,
    _coerce_text_list,
    _complete_distill_json,
)


class _ScriptedChat:
    model_id = "test"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self.outputs.pop(0)


def test_coerce_text_list_accepts_model_shape_drift() -> None:
    assert _coerce_text_list(
        [
            "claim-a",
            {"slug": "claim-b", "claim": "ignored"},
            {"claim": "claim-c"},
            {"title": "claim-d"},
            {"unknown": "ignored"},
            123,
        ]
    ) == ["claim-a", "claim-b", "claim-c", "claim-d"]


def test_coerce_confidence_defaults_unknown_values() -> None:
    assert _coerce_confidence("HIGH") == "high"
    assert _coerce_confidence("certain") == "medium"
    assert _coerce_confidence(None) == "medium"


def test_complete_distill_json_retries_invalid_json() -> None:
    chat = _ScriptedChat(["not json", '{"summary": "ok", "claims": []}'])

    data = _complete_distill_json(chat, "title", "body", uuid.uuid4())

    assert data["summary"] == "ok"


def test_complete_distill_json_raises_after_retries() -> None:
    doc_id = uuid.uuid4()
    chat = _ScriptedChat(["nope", "[]", "still nope"])

    with pytest.raises(ValueError, match=f"wiki_distill_invalid_json:{doc_id}"):
        _complete_distill_json(chat, "title", "body", doc_id)


def test_prompt_claim_cap_matches_code_truncation() -> None:
    """The prompt's stated cap and the code truncation (`raw_claims[:_MAX_CLAIMS]`) must
    be the same number.

    They are two separate ceilings on the same quantity. When they drifted apart — the
    prompt said "at most 8" with no lower bound while the code cut at 8 — distill quietly
    thinned the wiki, capping even the strongest model on more than half of company docs
    (T14, docs/model-orchestration.md §13). Lifting one without the other reintroduces
    exactly that failure, so this pins them together.
    """
    assert f"Return at most {_MAX_CLAIMS} claims" in _SYSTEM
    # The lifted prompt must also carry an explicit lower bound, or a terse model stops
    # early well under the cap (the actual cause of Solar's old 4.8 vs 7.1).
    assert "Extract EVERY verifiable claim" in _SYSTEM


def test_fallback_claim_helper_is_gone() -> None:
    """요약→claim fallback은 제거됐다 (헬퍼 부활 방지 트립와이어).

    요약은 **문서에 대한 서술**이지 지식이 아니다. claim으로 승격하면 "…에 대한 순위를
    정리한 문서입니다" 같은 메타 페이지가 생기고, 그게 다음 질문의 근거로 걸려 사용자에게
    그대로 되돌아온다(2026-07-17 prod). 실측: 살아있는 company 위키의 fallback claim
    23개가 **예외 없이 전부** 메타 서술(전체 claim의 0.3%). 동작 회귀는
    tests/integration/test_distill_entities.py::test_zero_claims_creates_no_fallback_claim.
    """
    import orthus.wiki.distill as d

    assert not hasattr(d, "_should_create_fallback_claim")
    assert not hasattr(d, "_NO_FACT_HINTS")
