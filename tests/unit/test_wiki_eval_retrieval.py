from __future__ import annotations

from types import SimpleNamespace

from orthus.wiki.eval_retrieval import _rank
from orthus.wiki.retrieve import _hybrid_lexical_score, _lexical_terms, _normalize_term


def test_rank_matches_slug_substring() -> None:
    assert _rank(["other", "target-slug-123"], "target-slug") == 2
    assert _rank(["other"], "missing") is None


def test_employee_list_terms_prefer_team_page_over_generic_list_page() -> None:
    terms = _lexical_terms("아크메 직원 목록")
    assert "목록" not in terms
    assert {"아크메", "직원", "팀원", "멤버"}.issubset(set(terms))

    team_page = SimpleNamespace(
        slug="팀",
        title="팀",
        content="현재 아크메 팀 멤버입니다.",
    )
    generic_list_page = SimpleNamespace(
        slug="5-10-출연자-목록",
        title="5 10 출연자 목록",
        content="출연자 목록 화면 수정 작업.",
    )

    assert _hybrid_lexical_score(team_page, terms) > _hybrid_lexical_score(generic_list_page, terms)


def test_normalize_term_strips_connective_particles_but_not_roots() -> None:
    # Connective particles are stripped (these let "KPI랑"/"역할이랑" match an English
    # or bare slug). The length guard keeps short roots that merely end in the particle.
    assert _normalize_term("kpi랑") == "kpi"
    assert _normalize_term("역할이랑") == "역할"
    assert _normalize_term("성과보다") == "성과"
    assert _normalize_term("친구하고") == "친구"
    # Roots that ARE the particle (or too short once stripped) stay intact.
    assert _normalize_term("사랑") == "사랑"
    assert _normalize_term("보다") == "보다"
    assert _normalize_term("나보다") == "나보다"


def test_hybrid_lexical_score_stays_within_unit_range() -> None:
    # Phrase + entity-anchor bonuses must never push the lexical score past 1.0 — the
    # merge step's vector/lexical blend rescales lexical as (l - 0.45) / 0.55.
    terms = _lexical_terms("이개발 역할 담당")
    person_page = SimpleNamespace(slug="이개발", title="이개발", content="이개발 담당 역할")
    score = _hybrid_lexical_score(person_page, terms)
    assert 0.0 <= score <= 1.0
