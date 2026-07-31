"""Purge T2 qa_session-learned artifacts from the LLM wiki.

WHY
---
`answer_from_qa` 학습(T2)이 채팅/askQ&A 답변을 그대로 `qa-*` source + `qa-*-claim`
claim으로 위키에 재흡수해 왔다. 프로덕션 증거(2026-07-16, company node): 학습된
qa 페이지 179건에 인사말("안녕?")까지 포함됐고, 그중 23개 청크는 죽은 번호 인용
("... (문서 [1] 및 [2] 참조)")을 품은 과거 답변 원문이었다. 이 claim들이 다음 검색의
grounding으로 다시 걸려 **같은 저품질 답이 글자 그대로 반복되는 자기오염 루프**가
됐다. 코드 게이트(`wiki/qa.py::_should_learn`)는 새 오염을 막지만 consolidate는
additive라 기존 오염은 명시적으로 지워야 한다 — 그게 이 스크립트다.

WHAT IT DOES (personal scope per owner + company scope; qa_session 산출물만)
---------------------------------------------------------------------------
1. `WikiSource.source_type == "qa_session"`인 source(slug `qa-*`)를 전부 찾는다.
2. 그 source들의 `key_claims`(`qa-*-claim`)를 모은다.
3. backlinks가 그 claim들로만 구성된 순수 qa 페이지는 삭제하고, 다른 지식과 섞인
   페이지는 보고 후 SKIP(수동 정리)한다 — purge_personal_board_wiki.py와 동일 규칙.
4. `--contaminated-only`면 claim 본문에 번호 인용 패턴(`[N]`/`(문서 [`)이 있는
   qa source/claim만 지운다(기본은 qa_session 전량 — T2 잔재는 Q&A 에코라
   지식 가치가 없고, 근거 원문은 원 소스 위키에 그대로 남아 있다).

SAFETY
------
- Dry-run 기본. `--apply`를 줘야 삭제한다.
- `--owner <uuid>`로 한 owner만 제한 가능. company scope는 `--company`로 opt-in.
- `store.delete_item`은 KG 비인지다. K7.1 이후 personal owner-scope row도 KG에
  투영되므로, apply 후 `make node-kg-rebuild NODE=<node>`(full rebuild + prune,
  삭제 수렴 권위)를 한 번 돌려야 KG 고아 노드가 정리된다.
- 실행 대상은 로드된 node env가 가리키는 DB + wiki-store다.

Usage:
    uv run python -m scripts.wiki.purge_qa_learned_wiki                    # dry-run
    uv run python -m scripts.wiki.purge_qa_learned_wiki --owner <uuid>     # dry-run, 한 owner
    uv run python -m scripts.wiki.purge_qa_learned_wiki --apply            # personal 전 owner 적용
    uv run python -m scripts.wiki.purge_qa_learned_wiki --company --apply  # company scope 포함
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select

from orthus.db import session
from orthus.tables import wiki_pages
from orthus.wiki import store
from orthus.wiki.qa import _ANSWER_CITATION as _CITATION  # 게이트와 같은 판정을 쓴다


@dataclass
class ScopePlan:
    scope: str
    owner_id: UUID | None
    source_slugs: list[str] = field(default_factory=list)
    claim_slugs: set[str] = field(default_factory=set)
    pure_page_slugs: list[str] = field(default_factory=list)
    mixed_page_slugs: list[str] = field(default_factory=list)


def _owners_with_personal_pages() -> list[UUID]:
    with session() as s:
        rows = s.execute(
            select(wiki_pages.c.owner_id)
            .where(wiki_pages.c.scope == "personal", wiki_pages.c.owner_id.is_not(None))
            .distinct()
        ).all()
    return [r.owner_id for r in rows]


def build_plan(scope: str, owner_id: UUID | None, *, contaminated_only: bool) -> ScopePlan:
    plan = ScopePlan(scope=scope, owner_id=owner_id)
    for src_slug in store.list_slugs("source", scope=scope, owner_id=owner_id):
        src = store.load_source(src_slug, scope=scope, owner_id=owner_id)
        if src is None or src.source_type != "qa_session":
            continue
        if contaminated_only:
            contaminated = _CITATION.search(src.summary or "")
            if not contaminated:
                for claim_slug in src.key_claims:
                    claim = store.load_claim(claim_slug, scope=scope, owner_id=owner_id)
                    if claim is not None and _CITATION.search(claim.claim or ""):
                        contaminated = True
                        break
            if not contaminated:
                continue
        plan.source_slugs.append(src_slug)
        plan.claim_slugs.update(src.key_claims)

    # 페이지: backlinks(폴드된 claim slug들)가 전부 qa claim이면 순수 qa 페이지 → 삭제.
    # 다른 claim과 섞였으면 보고만 하고 남긴다 (board purge와 동일한 보수 규칙).
    for pg_slug in store.list_slugs("page", scope=scope, owner_id=owner_id):
        pg = store.load_page(pg_slug, scope=scope, owner_id=owner_id)
        if pg is None:
            continue
        page_claims = set(pg.backlinks)
        if not page_claims or not (page_claims & plan.claim_slugs):
            continue
        if page_claims <= plan.claim_slugs:
            plan.pure_page_slugs.append(pg_slug)
        else:
            plan.mixed_page_slugs.append(pg_slug)
    return plan


def print_plan(plan: ScopePlan) -> None:
    who = f"owner {plan.owner_id}" if plan.owner_id else plan.scope
    print(f"\n=== {plan.scope} / {who} ===")
    print(f"  qa sources to delete : {len(plan.source_slugs)}")
    print(f"  qa claims to delete  : {len(plan.claim_slugs)}")
    print(f"  pure qa pages        : {len(plan.pure_page_slugs)}")
    if plan.mixed_page_slugs:
        print(f"  MIXED pages (SKIPPED, review by hand): {len(plan.mixed_page_slugs)}")
        for slug in plan.mixed_page_slugs:
            print(f"    - {slug}")
    for slug in plan.source_slugs[:10]:
        print(f"    source: {slug}")
    if len(plan.source_slugs) > 10:
        print(f"    … +{len(plan.source_slugs) - 10} more sources")


def apply_plan(plan: ScopePlan) -> int:
    deleted = 0
    for slug in plan.claim_slugs:
        if store.delete_item("claim", slug, scope=plan.scope, owner_id=plan.owner_id):
            deleted += 1
    for slug in plan.pure_page_slugs:
        if store.delete_item("page", slug, scope=plan.scope, owner_id=plan.owner_id):
            deleted += 1
    for slug in plan.source_slugs:
        if store.delete_item("source", slug, scope=plan.scope, owner_id=plan.owner_id):
            deleted += 1
    return deleted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="실제 삭제 (기본 dry-run)")
    ap.add_argument("--owner", type=UUID, default=None, help="한 owner의 personal scope만")
    ap.add_argument("--company", action="store_true", help="company scope도 포함")
    ap.add_argument(
        "--contaminated-only",
        action="store_true",
        help="번호 인용이 박힌 qa 산출물만 (기본은 qa_session 전량)",
    )
    args = ap.parse_args()

    plans: list[ScopePlan] = []
    owners = [args.owner] if args.owner else _owners_with_personal_pages()
    for owner in owners:
        plans.append(build_plan("personal", owner, contaminated_only=args.contaminated_only))
    if args.company:
        plans.append(build_plan("company", None, contaminated_only=args.contaminated_only))

    total = 0
    for plan in plans:
        if not (plan.source_slugs or plan.claim_slugs):
            continue
        print_plan(plan)
        total += len(plan.source_slugs) + len(plan.claim_slugs) + len(plan.pure_page_slugs)
        if args.apply:
            deleted = apply_plan(plan)
            print(f"  -> deleted {deleted} items")
    if total == 0:
        print("qa_session 잔재 없음 — 지울 것이 없습니다.")
        return
    if not args.apply:
        print(
            f"\nDRY-RUN: 총 {total}개 대상. 삭제하려면 --apply. 적용 후 make node-kg-rebuild 권장."
        )


if __name__ == "__main__":
    main()
