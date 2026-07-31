"""K7.1 owner-scope 데모 — PG에 데모 wiki_pages/links를 심거나(seed) 지운다(cleanup).

K7.1은 화면 기능이 아니라 **projection 결과물**이다. 이 스크립트로 회사 1 + 개인 2
(같은 slug, 다른 owner) 페이지와, 셋 다 같은 미materialized slug로 향하는 dangling
link를 심은 뒤 `make kg-rebuild`를 돌리면, Neo4j 브라우저(localhost:7474)에서
K7.1이 만든 것을 눈으로 볼 수 있다:

  * 노드/엣지에 scope/owner_id 속성이 찍힌다 (개인은 owner_id 보유, 회사는 NULL)
  * 같은 dst_slug 'k7-shared-topic'가 owner별로 **분리된 3개 placeholder**가 된다
    (ns_key: company:.. / <A>:.. / <B>:..) — v1이라면 1개로 합쳐졌을 것(B4)

사용법:
    python -m scripts.demo.k7_owner_scope_demo seed
    python -m scripts.demo.k7_owner_scope_demo cleanup

owner UUID는 출력되는 값을 Neo4j 브라우저 Cypher의 $callerA/$callerB에 쓴다.
"""

from __future__ import annotations

import sys
from uuid import UUID

from sqlalchemy import delete, insert

from orthus.db import session
from orthus.tables import wiki_links, wiki_pages

# 고정 데모 식별자 (재실행/cleanup이 정확히 같은 row를 겨냥하도록).
OWNER_A = UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
OWNER_B = UUID("bbbbbbbb-0000-0000-0000-0000000000b2")
PAGE_C = UUID("cccccccc-0000-0000-0000-0000000000cc")  # 회사
PAGE_A = UUID("aaaaaaaa-0000-0000-0000-0000000000aa")  # 개인 A
PAGE_B = UUID("bbbbbbbb-0000-0000-0000-0000000000bb")  # 개인 B
SHARED_SLUG = "k7-shared-topic"  # 미materialized → owner별 placeholder가 된다
DEMO_PAGE_IDS = [PAGE_C, PAGE_A, PAGE_B]


def _page(page_id: UUID, slug: str, title: str, scope: str, owner_id: UUID | None) -> dict:
    return {
        "page_id": page_id,
        "slug": slug,
        "kind": "page",
        "path": f"demo/{slug}.md",
        "title": title,
        "content_hash": f"demo-{page_id}",
        "schema_version": 1,
        "scope": scope,
        "project": "company",
        "owner_id": owner_id,
    }


def seed() -> None:
    cleanup(quiet=True)  # 멱등 — 이전 데모 잔재 제거 후 재삽입
    with session() as s:
        s.execute(
            insert(wiki_pages),
            [
                _page(PAGE_C, "k7-demo-company", "K7 데모 회사 페이지", "company", None),
                _page(PAGE_A, "k7-demo-note", "내 개인 노트 (A)", "personal", OWNER_A),
                _page(PAGE_B, "k7-demo-note", "내 개인 노트 (B)", "personal", OWNER_B),
            ],
        )
        s.execute(
            insert(wiki_links),
            [
                {"src_page_id": PAGE_C, "dst_slug": SHARED_SLUG, "rel": "backlink"},
                {"src_page_id": PAGE_A, "dst_slug": SHARED_SLUG, "rel": "backlink"},
                {"src_page_id": PAGE_B, "dst_slug": SHARED_SLUG, "rel": "backlink"},
            ],
        )
        s.commit()
    print("seed 완료. 데모 식별자:")
    print("  회사 페이지 slug = k7-demo-company (scope=company, owner=NULL)")
    print("  개인 페이지 slug = k7-demo-note   (A·B 동명, page_id로 분리)")
    print(f"  공유 dst_slug    = {SHARED_SLUG} (owner별 placeholder 3개)")
    print(f"  OWNER_A = {OWNER_A}")
    print(f"  OWNER_B = {OWNER_B}")
    print("\n다음: 플래그 ON으로 rebuild →")
    print("  ORTHUS_KG_ENABLED=true ORTHUS_KG_OWNER_SCOPE_ENABLED=true \\")
    print("  ORTHUS_OWNER_SCOPE_ENABLED=true uv run python -m orthus.kg.rebuild")


def cleanup(quiet: bool = False) -> None:
    with session() as s:
        s.execute(delete(wiki_links).where(wiki_links.c.src_page_id.in_(DEMO_PAGE_IDS)))
        s.execute(delete(wiki_pages).where(wiki_pages.c.page_id.in_(DEMO_PAGE_IDS)))
        s.commit()
    if not quiet:
        print("PG 데모 row 삭제 완료. Neo4j 잔재는 rebuild가 prune하거나, 아래로 직접:")
        print(
            f"  MATCH (n) WHERE n.slug IN ['k7-demo-company','k7-demo-note','{SHARED_SLUG}'] DETACH DELETE n"
        )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        seed()
    elif cmd == "cleanup":
        cleanup()
    else:
        print("usage: python -m scripts.demo.k7_owner_scope_demo {seed|cleanup}", file=sys.stderr)
        sys.exit(2)
