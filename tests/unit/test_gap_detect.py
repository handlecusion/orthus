"""Deterministic answer-gap detection (orthus/wiki/gap.py). No DB, no LLM.

Pins the three reasons and the negative case so the /ask gap banner only fires when
an answer is genuinely poorly grounded."""

from __future__ import annotations

import pytest

from orthus.schemas.canonical import WikiGap, WikiSourceRef
from orthus.wiki.gap import (
    WEAK_SCORE_THRESHOLD,
    _build_message,
    _top_company_page_slugs,
    detect_gap,
    maybe_missing_link,
    normalize_question,
)


def _hit(score: float, slug: str = "p", scope: str = "company") -> WikiSourceRef:
    return WikiSourceRef(
        page_slug=slug, title="P", kind="page", excerpt="…", score=score, source_scope=scope
    )


def _insufficient_gap() -> WikiGap:
    return WikiGap(
        detected=True,
        reason="insufficient_grounding",
        missing_topic="A와 B의 관계",
        top_score=1.0,
        message=_build_message("A와 B의 관계", "insufficient_grounding"),
    )


def test_normalize_question_dedup_key():
    assert normalize_question("  아크메  회사 소개?? ") == "아크메 회사 소개"
    assert normalize_question("Nova PROJECT") == "nova project"
    assert normalize_question("   ") == ""


def test_no_hits_is_no_data():
    gap = detect_gap("회사 소개", [], "관련 위키 내용을 찾지 못했습니다.")
    assert gap is not None
    assert gap.reason == "no_data"
    assert gap.detected is True
    assert gap.missing_topic == "회사 소개"
    assert gap.message  # deterministic guidance present


def test_insufficient_grounding_beats_high_score():
    # Reproduces the live case: retrieval scored well (1.0) but the model declared
    # the context lacks the answer -> still a gap.
    gap = detect_gap(
        "아크메 회사 소개",
        [_hit(1.0)],
        "제공된 컨텍스트에 아크메 회사 소개 정보 없음.",
    )
    assert gap is not None
    assert gap.reason == "insufficient_grounding"
    assert gap.top_score == 1.0


def test_weak_retrieval_on_low_score():
    low = WEAK_SCORE_THRESHOLD - 0.1
    gap = detect_gap("뭔가", [_hit(low)], "여기 충분한 답이 있습니다 상세 설명.")
    assert gap is not None
    assert gap.reason == "weak_retrieval"


def test_good_answer_no_gap():
    gap = detect_gap(
        "노바티 소개",
        [_hit(0.9)],
        "노바티는 아틀라스 매칭 서비스입니다. 주요 기능은 A, B, C 입니다.",
    )
    assert gap is None


def test_english_absence_marker():
    gap = detect_gap("what is X", [_hit(0.9)], "The context does not contain that.")
    assert gap is not None
    assert gap.reason == "insufficient_grounding"


# --- Korean absence phrasings the flat marker table missed (F2) -------------------
#
# The English "not provided in" was registered but its Korean mirrors were not, so a model
# that refuses with "…정보는 제공되지 않았습니다" / "…정보가 명시되어 있지 않습니다" slipped
# through as a grounded answer. Refusal phrasing varies by model → a one-dialect table is a
# model-bias device. The additions are noun-bound (see gap.py) so that a substantive answer's
# caveat tail is NOT flagged; both directions are pinned here.


@pytest.mark.parametrize(
    "answer",
    [
        # measured refusals (fugu-ko E2 anchor probe raw)
        "Nova 개발 로드맵의 주요 마일스톤에 대한 정보는 제공되지 않았습니다.",
        "촬영 관련 보고된 오류에 대한 정보는 현재 제공되지 않았습니다.",
        "제공된 컨텍스트에는 영입 후보 컨택 경로에 대한 정보가 명시되어 있지 않습니다.",
        "두 페이지 모두 AI 관련 툴에 대한 정보를 제공하지 않습니다.",
        "회사가 협업하는 파트너사에 대한 내용은 기재되어 있지 않습니다.",
        # explicit refusal verb
        "따라서 해당 질문에 대한 답변을 제공할 수 없습니다.",
        "해당 근거로는 답변할 수 없습니다.",
    ],
)
def test_korean_absence_phrasings_are_insufficient(answer):
    gap = detect_gap("q", [_hit(0.9)], answer)
    assert gap is not None
    assert gap.reason == "insufficient_grounding"


@pytest.mark.parametrize(
    "answer",
    [
        # A product FACT, not a refusal — "제공하지 않" bound to a domain noun (기능).
        "SubtitlePanel은 자막 트랙 표시와 인라인 큐 편집을 제공합니다. "
        "단, Kdenlive 같은 별도 자막 관리자 창은 제공하지 않습니다.",
        # Substantive answer + caveat about a domain noun (이름/액션/유형) — still grounded.
        "회사가 참고하는 AI 툴은 Kling입니다. 다만 순위 문서의 구체적인 툴 이름은 명시되지 않았습니다.",
        "파트너사는 초록뱀미디어, Alibaba입니다. 첫 미팅 문서에는 다음 액션이 명시되어 있지 않습니다.",
        "자주 등장하는 버그는 스크롤 튕김, 배역 수정 오류입니다. "
        "그 외 유형은 컨텍스트에 명시되어 있지 않습니다.",
    ],
)
def test_substantive_answer_with_caveat_tail_is_not_a_gap(answer):
    # Regression guard on over-broadening: a false gap drops the answer from the cache AND
    # from decompose synthesis, so these must stay grounded.
    assert detect_gap("q", [_hit(0.9)], answer) is None


# --- missing_link upgrade (K6 PR3) — deterministic, fail-open ---------------------


def test_missing_link_message_present():
    # GapReason now carries missing_link and _build_message has its 4th key (no KeyError).
    msg = _build_message("A와 B의 관계", "missing_link")
    assert msg
    assert "연결" in msg


def test_maybe_missing_link_passes_through_other_reasons():
    no_data = WikiGap(detected=True, reason="no_data", missing_topic="x", message="m")
    assert maybe_missing_link(no_data, []) is no_data


def test_maybe_missing_link_skipped_when_kg_unavailable():
    # Default settings keep KG off, so kg_available() is False — the upgrade is a
    # fail-open no-op and the gap keeps its insufficient_grounding reason (가드 ⓒ).
    gap = _insufficient_gap()
    out = maybe_missing_link(gap, [_hit(1.0, "page-a"), _hit(0.9, "page-b")])
    assert out.reason == "insufficient_grounding"


def test_maybe_missing_link_skipped_with_fewer_than_two_pages():
    # 가드 ⓑ — a single resolvable company page can't be "disconnected" from another.
    gap = _insufficient_gap()
    out = maybe_missing_link(gap, [_hit(1.0, "only-page"), _hit(0.9, "only-page")])
    assert out.reason == "insufficient_grounding"


def test_top_company_page_slugs_excludes_personal_hits():
    # #7 — central node에서 retrieve가 섞어 주는 personal hit은 owner_scope OFF(default)에서
    # 회사 그래프 입력에서 빠진다(today 동작 보존). company 1 + personal 1이면 회사 slug 1개만
    # 남아 <2로 skip된다. K7.4로 반환 타입은 scope-tag된 tuple이다.
    hits = [_hit(1.0, "co-a", scope="company"), _hit(0.9, "pers-x", scope="personal")]
    assert _top_company_page_slugs(hits, n=2) == [("co-a", "company")]


def test_top_company_page_slugs_owner_inclusive_admits_personal():
    # K7.4 — owner_scope ON이면 caller 본인 personal hit도 scope-tag된 anchor로 받는다
    # (foreign-personal은 retrieve가 애초에 hits에서 배제). company 1 + personal 1 = cross-scope.
    hits = [_hit(1.0, "co-a", scope="company"), _hit(0.9, "pers-x", scope="personal")]
    assert _top_company_page_slugs(hits, n=2, owner_scope=True) == [
        ("co-a", "company"),
        ("pers-x", "personal"),
    ]


def test_top_company_page_slugs_owner_scope_off_byte_identical_to_company_only():
    # K7.4 byte-identity — owner_scope OFF에서 personal hit은 제외되어 company-only candidate
    # set과 동일(노트 보유 여부가 candidate shape를 바꾸지 않음, fingerprint 방지).
    hits = [
        _hit(1.0, "co-a", scope="company"),
        _hit(0.95, "pers-x", scope="personal"),
        _hit(0.9, "co-b", scope="company"),
    ]
    assert _top_company_page_slugs(hits, n=2, owner_scope=False) == [
        ("co-a", "company"),
        ("co-b", "company"),
    ]
