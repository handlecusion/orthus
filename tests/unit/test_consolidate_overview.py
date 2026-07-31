"""페이지 overview가 폴드된 claim 전부를 담는지 (`wiki/consolidate.py::_merge_overview`).

이전에는 seed 시점의 첫 claim 하나로 overview가 영구 고정됐다(fold는 evidence/sources/
backlinks만 병합). `wiki_chunks`가 페이지 본문을 인덱싱하고 페이지가 claim보다 상위
랭크(`retrieve._merge_candidates`)라, 20개 claim이 붙은 페이지도 첫 문장 하나로 답했다
— 2026-07-16 prod의 메타 답변 근원. 결정론(LLM 0회, AGENTS.md §7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from orthus.schemas.canonical import WikiClaim, WikiSource
from orthus.wiki.consolidate import (
    _OVERVIEW_MAX_CHARS,
    _OVERVIEW_MAX_CLAIMS,
    _merge_overview,
    _seed_page,
)


def _claim(slug: str, text: str, evidence: str = "") -> WikiClaim:
    return WikiClaim(
        slug=slug,
        claim=text,
        supporting=["src"],
        conflicting=[],
        confidence="medium",
        last_reviewed=date(2026, 7, 16),
        related_pages=["ai-video-tools"],
        evidence=evidence or text,
    )


def _source() -> WikiSource:
    return WikiSource(
        slug="src",
        title="AI 영상관련 툴",
        source_type="corpus_doc",
        source_ref="doc-1",
        ingested_at=datetime(2026, 7, 16, tzinfo=UTC),
        summary="AI 영상 툴 정리",
        key_concepts=[],
        key_claims=[],
        terminology=[],
        related_pages=["ai-video-tools"],
        open_questions=[],
    )


def test_merge_overview_accumulates_every_claim():
    out = _merge_overview(
        "",
        [
            _claim("c1", "Hugging Face는 AI 모델을 유통·실행·협업하게 해주는 플랫폼이다."),
            _claim("c2", "알리바바 qwen 오픈소스는 이미지와 동영상에서 강점을 보인다."),
            _claim("c3", "Wan2.2는 오픈소스 모델이다."),
        ],
    )
    assert out == (
        "- Hugging Face는 AI 모델을 유통·실행·협업하게 해주는 플랫폼이다.\n"
        "- 알리바바 qwen 오픈소스는 이미지와 동영상에서 강점을 보인다.\n"
        "- Wan2.2는 오픈소스 모델이다."
    )


def test_merge_overview_is_idempotent_on_refold():
    """같은 claim을 다시 폴드해도 중복되지 않는다 (consolidate 재실행 안전)."""
    first = _merge_overview("", [_claim("c1", "Wan2.2는 오픈소스 모델이다.")])
    again = _merge_overview(first, [_claim("c1", "Wan2.2는 오픈소스 모델이다.")])
    assert again == first == "- Wan2.2는 오픈소스 모델이다."


def test_merge_overview_appends_to_existing():
    existing = "- 기존 클레임이다."
    out = _merge_overview(existing, [_claim("c2", "새 클레임이다.")])
    assert out == "- 기존 클레임이다.\n- 새 클레임이다."


def test_merge_overview_keeps_legacy_prose_as_first_item():
    """이 변경 이전에 쓰인 단문 overview는 첫 항목으로 보존된다."""
    out = _merge_overview("옛 산문 요약이다.", [_claim("c1", "새 클레임이다.")])
    assert out == "- 옛 산문 요약이다.\n- 새 클레임이다."


def test_merge_overview_caps_claim_count():
    claims = [_claim(f"c{i}", f"클레임 번호 {i}이다.") for i in range(_OVERVIEW_MAX_CLAIMS + 5)]
    out = _merge_overview("", claims)
    assert len(out.splitlines()) == _OVERVIEW_MAX_CLAIMS


def test_merge_overview_caps_total_chars():
    """허브 페이지가 무한정 큰 청크로 자라지 않는다."""
    claims = [_claim(f"c{i}", "가" * 400) for i in range(10)]
    out = _merge_overview("", claims)
    assert len(out) <= _OVERVIEW_MAX_CHARS
    assert out.startswith("- ")


def test_seed_page_overview_is_bullet_form():
    page = _seed_page("ai-video-tools", _claim("c1", "Wan2.2는 오픈소스 모델이다."), _source())
    assert page.overview == "- Wan2.2는 오픈소스 모델이다."
    # definition은 여전히 seed claim의 단문 (Concise Definition 계약 유지).
    assert page.definition == "Wan2.2는 오픈소스 모델이다."


def test_seed_page_no_longer_duplicates_claim_and_evidence():
    """claim==evidence인 fallback claim도 정의/개요가 같은 문장을 두 번 쓰지 않는다.

    prod `ai-영상관련툴`이 정확히 이 형태였다(Concise Definition과 Conceptual Overview에
    동일 문장). 이제 overview는 불릿 목록이라 본문이 에코가 아니다.
    """
    text = "AI 영상관련툴에 대한 순위를 정리한 문서입니다."
    page = _seed_page("ai-video-tools", _claim("c1", text, evidence=text), _source())
    assert page.overview == f"- {text}"
    assert page.overview != page.definition
