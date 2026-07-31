"""K6 PR3 — missing_link gap 감지 (구현 명세 §9.3, 결정론·LLM 0회).

insufficient_grounding 답변 직후, 상위 2개 materialized company wiki page 사이에
path_between(max_hops=4)을 K4 게이트로 질의해 경로가 없으면 gap reason을
missing_link로 승격한다. company scope 한정 + fail-open(KG off/미가용/예외는 원래
reason 유지). neo4j-test(:7688) + orthus_test PG에서 실행된다(`make kg-test-up && make test`).
"""

from __future__ import annotations

import pytest

from orthus.kg.client import close_kg_driver
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import WikiGap, WikiPage, WikiSourceRef
from orthus.settings import get_settings
from orthus.wiki import store as wiki_store
from orthus.wiki.gap import maybe_missing_link


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    """test_kg_gate.kg_env와 동일 계약 — clean이 KG-off 기본값을 깔고 kg_clean이
    test 컨테이너 설정을 override하며 wiki-store frontmatter는 per-test tmp root로 격리한다.
    kg_clean이 setup/teardown에서 close_kg_driver를 호출해 client 음성 가용성 캐시도 리셋된다."""
    settings = get_settings()
    prev_root = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev_root


def _write_page(user_id, slug, *, project, backlinks=()):
    return wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=f"페이지 {slug}",
            definition=f"{slug} 정의",
            overview=f"{slug} 개요",
            backlinks=list(backlinks),
        ),
        user_id=user_id,
        scope="company",
        owner_id=None,
        project=project,
    )


def _hit(slug: str, score: float = 1.0) -> WikiSourceRef:
    return WikiSourceRef(page_slug=slug, title=slug, kind="page", excerpt="…", score=score)


def _insufficient_gap() -> WikiGap:
    return WikiGap(
        detected=True,
        reason="insufficient_grounding",
        missing_topic="A와 B의 관계",
        top_score=1.0,
        message="단편 정보만 있습니다.",
    )


def test_missing_link_detected_on_disconnected_pages(kg_env, user_id):
    # **같은 project**의 비연결 page 쌍 — path_between이 IN_PROJECT 허브를 제외하므로
    # a→:Project→b 2-hop으로 "연결"되지 않는다. 허브를 타던 이전 구현이라면 이 케이스가
    # missing_link로 안 잡혀 실패한다(코드 리뷰 #1 회귀).
    _write_page(user_id, "iso-a", project="atlas")
    _write_page(user_id, "iso-b", project="atlas")
    run_rebuild()

    out = maybe_missing_link(_insufficient_gap(), [_hit("iso-a"), _hit("iso-b")])

    assert out.reason == "missing_link"
    assert "연결" in out.message


def test_missing_link_not_upgraded_when_path_exists(kg_env, user_id):
    # link-a가 link-b를 backlink → 지식 rel(BACKLINK) 경로 존재 → 승격하지 않는다.
    _write_page(user_id, "link-a", project="atlas", backlinks=["link-b"])
    _write_page(user_id, "link-b", project="atlas")
    run_rebuild()

    out = maybe_missing_link(_insufficient_gap(), [_hit("link-a"), _hit("link-b")])

    assert out.reason == "insufficient_grounding"


def test_missing_link_skipped_when_kg_down(clean, user_id):
    """KG enabled + 죽은 URI → 게이트가 kg_unavailable로 reject → fail-open skip.

    company page를 실제로 만들지 않아도 된다 — 게이트 가용성 단계에서 멈춘다.
    connect-timeout 반복은 client.kg_available의 음성 캐시가 흡수한다."""
    settings = get_settings()
    close_kg_driver()
    settings.kg_enabled = True
    settings.kg_uri = "bolt://127.0.0.1:1"
    settings.kg_password = "x"
    try:
        out = maybe_missing_link(_insufficient_gap(), [_hit("a"), _hit("b")])
    finally:
        close_kg_driver()
        settings.kg_enabled = False
        settings.kg_uri = "bolt://127.0.0.1:7687"
        settings.kg_password = ""

    assert out.reason == "insufficient_grounding"


def test_missing_link_personal_node_skips(kg_env, user_id):
    """personal node는 v1 그래프 대상이 아니다(company-only) → 무조건 skip."""
    _write_page(user_id, "iso-a", project="atlas")
    _write_page(user_id, "iso-b", project="nova")
    run_rebuild()
    settings = get_settings()
    prev_kind = settings.node_kind
    settings.node_kind = "personal"
    try:
        out = maybe_missing_link(_insufficient_gap(), [_hit("iso-a"), _hit("iso-b")])
    finally:
        settings.node_kind = prev_kind

    assert out.reason == "insufficient_grounding"
