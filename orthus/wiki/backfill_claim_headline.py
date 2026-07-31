"""`python -m orthus.wiki.backfill_claim_headline [옵션]` — 기존 claim에 display_title 백필.

목적: KG 그래프 패널에서 claim 노드가 내부 slug(`qa-…-claim`)로 뜨는 문제를 고치기
위해, `display_title`이 비어 있는 기존 claim들에 사람이 읽는 한 줄 헤드라인을 채운다.

계층 분리(불변식): 헤드라인 LLM 생성은 이 백필(=저작 계층)에서만 일어나고, 결과를
`WikiClaim.display_title`에 실어 `store.write_claim`로 재저작한다. write_claim이 다시
frontmatter + Postgres `wiki_pages.display_title`(coalesce) + KG outbox를 한 트랜잭션에
기록한다. content_hash는 본문(claim/evidence/notes) 기준이라 헤드라인만 채워도 불변 →
재임베딩 없음. KG 투영은 이후 `make node-kg-rebuild NODE=company`로 저장된 헤드라인을
slug처럼 복사만 한다(투영 LLM 0회 유지).

멱등: 이미 `display_title`이 있는 claim은 skip. `--dry-run`이면 대상만 세고 쓰지 않는다.
LLM 미가용/실패는 `_headline_fallback`(결정론 Q/A 제거 + 첫 문장 + 120자)로 폴백한다.

실제 대량 실행(수천 건 + LLM)은 operator 단계다 — 이 모듈은 그 도구만 제공한다.
소규모 확인은 `--limit N` + `--dry-run`을 쓴다. node env 래퍼(run_python.sh)로 실행해야
SoR가 dev DB로 섞이지 않는다(operations.md §2.1).
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from uuid import UUID

from orthus.audit import audit
from orthus.models.orchestration import TASK_CLAIM_HEADLINE, get_chat_model_for
from orthus.wiki import store
from orthus.wiki.distill import (
    HEADLINE_SYSTEM,
    _claim_headline,
    _coerce_text,
    _headline_fallback,
)


def _llm_headline(chat, claim_text: str) -> tuple[str, bool]:
    """단발 LLM 헤드라인 → (headline, used_fallback). 실패/빈 응답이면 결정론 폴백."""
    try:
        # 호출 단위 audit span(AGENTS.md 설계원칙 3) — 대량 실행에서 per-call telemetry 보존.
        with audit("wiki.headline") as span:
            raw = chat.complete(HEADLINE_SYSTEM, claim_text)
            span.add_meta(chars=len(raw) if isinstance(raw, str) else 0)
    except Exception:  # noqa: BLE001 — 백필은 fail-soft: 폴백으로 계속 진행
        return _headline_fallback(claim_text), True
    headline = _claim_headline(raw, claim_text)
    # LLM 응답이 비어 _claim_headline이 결정론 폴백으로 떨어졌는지 재계산 없이 판정
    # (헤드라인이 폴백과 우연히 같아도 오분류하지 않는다).
    return headline, not _coerce_text(raw)


@dataclass
class BackfillReport:
    scanned: int = 0
    skipped_existing: int = 0
    updated: int = 0
    fallback: int = 0
    errors: int = 0


@dataclass(frozen=True)
class _ClaimCandidate:
    slug: str
    claim: object
    claim_text: str


def _headline_candidates(
    *,
    scope: str,
    owner_id: UUID | None,
    limit: int | None,
    report: BackfillReport,
) -> list[_ClaimCandidate]:
    candidates: list[_ClaimCandidate] = []
    slugs = store.list_slugs("claim", scope=scope, owner_id=owner_id)
    for slug in slugs:
        if limit is not None and len(candidates) >= limit:
            break
        report.scanned += 1
        try:
            claim = store.load_claim(slug, scope=scope, owner_id=owner_id)
        except Exception:  # noqa: BLE001 — 손상 파일 1건이 전체를 막지 않게
            report.errors += 1
            continue
        if claim is None:
            report.errors += 1
            continue
        if claim.display_title:
            report.skipped_existing += 1
            continue
        candidates.append(_ClaimCandidate(slug=slug, claim=claim, claim_text=claim.claim))
    return candidates


def _generate_headlines(
    candidates: list[_ClaimCandidate],
    *,
    use_llm: bool,
    dry_run: bool,
    concurrency: int,
) -> tuple[list[tuple[_ClaimCandidate, str]], int, int]:
    if not use_llm or dry_run:
        return (
            [(candidate, _headline_fallback(candidate.claim_text)) for candidate in candidates],
            len(candidates),
            0,
        )

    # All four models tie on this (0 failures, 0 over-length, 0 invented across 20
    # claims) — so it goes to the primary, which is simply the fastest (514ms p50).
    chat = get_chat_model_for(TASK_CLAIM_HEADLINE)
    max_workers = max(1, concurrency)
    results: list[tuple[_ClaimCandidate, str] | None] = [None] * len(candidates)
    fallback = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="claim-headline") as pool:
        future_to_index = {
            pool.submit(_llm_headline, chat, candidate.claim_text): idx
            for idx, candidate in enumerate(candidates)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            candidate = candidates[idx]
            try:
                headline, used_fallback = future.result()
            except Exception:  # noqa: BLE001 — 방어: _llm_headline 밖 예외도 1건 폴백
                headline = _headline_fallback(candidate.claim_text)
                used_fallback = True
                errors += 1
            if used_fallback:
                fallback += 1
            results[idx] = (candidate, headline)

    return [item for item in results if item is not None], fallback, errors


def backfill_claim_headlines(
    *,
    user_id: UUID,
    scope: str = "company",
    owner_id: UUID | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    use_llm: bool = True,
    concurrency: int = 4,
) -> BackfillReport:
    """`display_title`이 없는 claim에 헤드라인을 채운다(멱등, dry-run 지원)."""
    report = BackfillReport()
    candidates = _headline_candidates(scope=scope, owner_id=owner_id, limit=limit, report=report)
    generated, fallback, errors = _generate_headlines(
        candidates,
        use_llm=use_llm,
        dry_run=dry_run,
        concurrency=concurrency,
    )
    report.fallback += fallback
    report.errors += errors

    for candidate, headline in generated:
        if dry_run:
            report.updated += 1
            continue

        updated = candidate.claim.model_copy(update={"display_title": headline})
        store.write_claim(updated, user_id=user_id, scope=scope, owner_id=owner_id)
        report.updated += 1

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m orthus.wiki.backfill_claim_headline",
        description="claim display_title(사람이 읽는 헤드라인) 백필",
    )
    parser.add_argument("--user-id", required=True, help="저작 user_id(embeddings/audit 소유) UUID")
    parser.add_argument(
        "--scope", default="company", choices=["company", "personal"], help="wiki scope"
    )
    parser.add_argument("--owner-id", default=None, help="personal scope owner UUID")
    parser.add_argument("--dry-run", action="store_true", help="대상만 세고 쓰지 않음(LLM 미호출)")
    parser.add_argument("--limit", type=int, default=None, help="최대 갱신 건수")
    parser.add_argument("--no-llm", action="store_true", help="LLM 없이 결정론 폴백만 사용")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="LLM headline 생성 동시성(쓰기 단계는 직렬 유지, 기본 4)",
    )
    args = parser.parse_args(argv)

    try:
        user_id = UUID(args.user_id)
    except ValueError as exc:
        print(f"backfill: invalid --user-id — {exc}", file=sys.stderr)
        return 2
    owner_id: UUID | None = None
    if args.scope == "personal":
        if not args.owner_id:
            print("backfill: personal scope requires --owner-id", file=sys.stderr)
            return 2
        try:
            owner_id = UUID(args.owner_id)
        except ValueError as exc:
            print(f"backfill: invalid --owner-id — {exc}", file=sys.stderr)
            return 2

    with audit("wiki.backfill_claim_headline") as span:
        report = backfill_claim_headlines(
            user_id=user_id,
            scope=args.scope,
            owner_id=owner_id,
            dry_run=args.dry_run,
            limit=args.limit,
            use_llm=not args.no_llm,
            concurrency=max(1, args.concurrency),
        )
        span.add_meta(
            scanned=report.scanned,
            updated=report.updated,
            skipped_existing=report.skipped_existing,
            fallback=report.fallback,
            errors=report.errors,
            dry_run=args.dry_run,
            concurrency=max(1, args.concurrency),
        )

    print(
        "backfill claim headline complete: "
        f"scanned={report.scanned} updated={report.updated} "
        f"skipped_existing={report.skipped_existing} fallback={report.fallback} "
        f"errors={report.errors} dry_run={args.dry_run} concurrency={max(1, args.concurrency)}"
    )
    if not args.dry_run and report.updated:
        print(
            "next: run `make node-kg-rebuild NODE=company` to reproject the "
            "headlines onto Neo4j claim nodes.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
