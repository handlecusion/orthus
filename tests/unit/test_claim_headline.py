"""KG claim 헤드라인(display_title) 단위 테스트 — DB 불필요.

결정론 폴백 헬퍼, LLM headline coerce, codec round-trip(신·구 frontmatter),
redaction, KG 투영 title override를 순수 함수/모델 수준에서 고정한다.
"""

from __future__ import annotations

from datetime import date

from orthus.schemas.canonical import WikiClaim
from orthus.wiki import store
from orthus.wiki.distill import _HEADLINE_MAX, _claim_headline, _headline_fallback


# --- 결정론 폴백 -----------------------------------------------------------------


def test_headline_fallback_strips_qa_prefix_and_takes_first_sentence():
    text = (
        "Q: nova 주간회고와 충돌하는 주장 보여줘 A: 주간 회고와 충돌하는 주장은 A다. 그리고 B다."
    )
    out = _headline_fallback(text)
    assert not out.lower().startswith("q:")
    assert not out.lower().startswith("a:")
    # 첫 문장에서 끊긴다(둘째 문장 'B다'는 미포함).
    assert "B다" not in out
    # Q: 접두만 제거되고 나머지 텍스트는 유지된다.
    assert out.startswith("nova 주간회고와 충돌하는 주장 보여줘")


def test_headline_fallback_one_line_and_capped():
    text = "첫 줄\n둘째 줄\n셋째 줄 " + ("가" * 300)
    out = _headline_fallback(text)
    assert "\n" not in out
    assert len(out) <= _HEADLINE_MAX


def test_headline_fallback_empty_input_is_empty():
    assert _headline_fallback("") == ""
    assert _headline_fallback("   ") == ""


def test_headline_fallback_no_sentence_end_uses_whole_capped():
    text = "종결부호 없는 한 줄 주장 텍스트"
    assert _headline_fallback(text) == text


# --- LLM headline coerce ---------------------------------------------------------


def test_claim_headline_prefers_llm_value():
    out = _claim_headline("사람이 읽는 헤드라인", "Q: 질문 A: 답변")
    assert out == "사람이 읽는 헤드라인"


def test_claim_headline_falls_back_when_llm_blank():
    out = _claim_headline("   ", "Q: 질문 A: 이것이 답이다.")
    # LLM이 비면 claim_text에서 Q/A 제거 폴백.
    assert not out.lower().startswith("q:")
    assert "이것이 답이다" in out


def test_claim_headline_llm_one_line_and_capped():
    out = _claim_headline("여러\n줄\n헤드라인 " + ("나" * 300), "claim")
    assert "\n" not in out
    assert len(out) <= _HEADLINE_MAX


# --- codec round-trip ------------------------------------------------------------


def _base_claim(**over) -> WikiClaim:
    kw = dict(
        slug="qa-ae4e5c20-claim",
        claim="Q: 질문 A: 답변.",
        supporting=["src-x"],
        conflicting=[],
        confidence="low",
        last_reviewed=date(2026, 6, 8),
        related_pages=["x"],
        evidence="근거.",
        notes=None,
    )
    kw.update(over)
    return WikiClaim(**kw)


def test_claim_display_title_round_trip():
    m = _base_claim(display_title="주간 회고 문서에 계획·회고 항목이 모두 미입력이다")
    back = store.from_markdown["claim"](store.to_markdown["claim"](m))
    assert back == m
    assert back.display_title == m.display_title


def test_claim_display_title_none_round_trip():
    m = _base_claim(display_title=None)
    text = store.to_markdown["claim"](m)
    # None은 frontmatter에 `null`로 직렬화된다.
    assert "display_title: null" in text
    back = store.from_markdown["claim"](text)
    assert back == m
    assert back.display_title is None


def test_claim_old_frontmatter_without_display_title_loads_none():
    """display_title 키가 없는 옛 claim 파일도 None으로 안전하게 로드된다."""
    old_text = (
        "---\n"
        'slug: "qa-ae4e5c20-claim"\n'
        "supporting: []\n"
        "conflicting: []\n"
        'confidence: "low"\n'
        'last_reviewed: "2026-06-08"\n'
        "related_pages: []\n"
        "---\n\n"
        "# Claim: qa-ae4e5c20-claim\n\n"
        "## Claim\n\n주장 본문.\n\n"
        "## Evidence\n\n근거.\n\n"
        "## Notes\n\n(none)\n"
    )
    back = store.from_markdown["claim"](old_text)
    assert back.display_title is None
    assert back.slug == "qa-ae4e5c20-claim"


# --- redaction -------------------------------------------------------------------


def test_redact_claim_redacts_display_title():
    from orthus.wiki.store import _redact_claim

    m = _base_claim(display_title="연락처 test@example.com 관련 헤드라인")
    red = _redact_claim(m)
    assert "test@example.com" not in (red.display_title or "")


def test_redact_claim_display_title_none_stays_none():
    from orthus.wiki.store import _redact_claim

    red = _redact_claim(_base_claim(display_title=None))
    assert red.display_title is None
