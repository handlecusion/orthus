"""A.X-Encoder 파인튜닝용 (질문, 정답청크, 하드네거티브) 학습쌍 생성.

**이 스크립트의 존재 이유는 누수 차단이다.** 인계 문서 T3는 "평가 83페이지를 학습에서
빼라"고만 했는데, 그대로 따르면 실험이 무효가 된다:

    평가 페이지 `aws-operating-cost`      : "Orbit의 AWS 운영비는 대략 한달에 60달러이다."
    그 claim `aws-operating-cost-b906b823`: "Orbit의 AWS 운영비는 대략 한달에 60달러이다."

wiki pool(scope=company wiki_chunk 10,153)은 **page 2,618 + claim 7,535**로 이뤄지는데,
claim은 page를 컴파일한 원재료라 본문이 글자 단위로 겹친다. 페이지만 빼고 claim을 남기면
**정답 본문을 그대로 외운 모델**을 평가하게 된다. 그래서 `wiki_links.rel='supports'`로
평가 페이지를 떠받치는 claim 247개까지 전부 제외한다(실측: 83페이지 전부가 claim 보유).

하드 네거티브도 같은 이유로 **family 단위**로 거른다 — 정답 페이지의 형제 claim을
네거티브로 주면 "같은 지식을 서로 밀어내라"고 가르치는 꼴이라 학습이 망가진다.

네거티브는 학습 후보 안에서만 뽑는다. 평가 콘텐츠는 어떤 형태로도 학습에 노출하지 않는다.

출력: train_pairs.jsonl (회사 콘텐츠 포함 → gitignore)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from orthus.tables import embeddings, wiki_chunks, wiki_pages
from orthus.wiki.retrieve import _blocked_company_provenance_slugs, _not_blocked_company_page

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_questions import STYLES, build_chat, form_for  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_PATH = f"{HERE}/sample_pages.json"
OUT_PATH = f"{HERE}/train_pairs.jsonl"


def load_pool(s):
    """평가 pool과 **완전히 동일한 조건**의 청크 집합(scope=company wiki_chunk).

    mirror_retrieve.build_pool과 같은 where를 쓴다 — 학습 후보가 평가 pool과 다른
    모집단이면 도메인 적응이 아니라 다른 실험이 된다.
    """
    blocked = _blocked_company_provenance_slugs(s)
    where = [embeddings.c.kind == "wiki_chunk", embeddings.c.scope == "company"]
    if blocked:
        where.append(_not_blocked_company_page(blocked))
    return s.execute(
        select(
            wiki_pages.c.slug,
            wiki_pages.c.kind,
            wiki_pages.c.title,
            wiki_chunks.c.content,
            embeddings.c.vec,
        )
        .select_from(
            wiki_chunks.join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
            .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
        )
        .where(*where)
    ).all()


def build_family_map(s) -> tuple[dict, dict]:
    """page --supports--> claim / claim --backlink--> page 로 '같은 지식' 묶음을 만든다."""
    rows = s.execute(
        text(
            """
            select p.kind, p.slug, l.rel, l.dst_slug
            from wiki_links l join wiki_pages p on p.page_id = l.src_page_id
            where p.scope = 'company' and l.rel in ('supports', 'backlink')
            """
        )
    ).all()
    page_to_claims: dict[str, set[str]] = defaultdict(set)
    claim_to_page: dict[str, str] = {}
    for kind, slug, rel, dst in rows:
        if kind == "page" and rel == "supports":
            page_to_claims[slug].add(dst)
        elif kind == "claim" and rel == "backlink":
            claim_to_page[slug] = dst
    return page_to_claims, claim_to_page


def family_of(slug: str, kind: str, page_to_claims: dict, claim_to_page: dict) -> set[str]:
    page = slug if kind == "page" else claim_to_page.get(slug)
    if page is None:
        return {slug}
    return {page} | page_to_claims.get(page, set())


def main() -> None:
    ap = argparse.ArgumentParser(description="A.X 파인튜닝 학습쌍 생성")
    ap.add_argument("--limit", type=int, default=0, help="앞 N개만(스모크). 0=전체")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--negatives", type=int, default=4, help="쌍당 하드 네거티브 수")
    ap.add_argument("--min-chars", type=int, default=80, help="이보다 짧은 청크는 건너뛴다")
    ap.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 집합/누수만 검증")
    args = ap.parse_args()

    engine = create_engine(
        os.environ["ORTHUS_PG_DSN_READONLY"],
        connect_args={"options": "-c statement_timeout=120000"},
    )

    eval_pages = [p["expected_slug"] for p in json.load(open(BASE_PATH, encoding="utf-8"))]
    with Session(engine) as s:
        pool = load_pool(s)
        page_to_claims, claim_to_page = build_family_map(s)

    # ---- 제외 집합: 평가 페이지 + 그 페이지를 떠받치는 claim 전부 (T3 심화) ----
    excluded = set(eval_pages)
    for p in eval_pages:
        excluded |= page_to_claims.get(p, set())
    train_rows = [r for r in pool if r.slug not in excluded]
    n_short = sum(1 for r in train_rows if len(r.content) < args.min_chars)
    train_rows = [r for r in train_rows if len(r.content) >= args.min_chars]

    print(f"pool {len(pool)} = page {sum(1 for r in pool if r.kind == 'page')} "
          f"+ claim {sum(1 for r in pool if r.kind == 'claim')}")
    print(f"제외: 평가페이지 {len(eval_pages)} + 지지 claim {len(excluded) - len(eval_pages)} "
          f"= {len(excluded)}  |  짧은 청크(<{args.min_chars}자) {n_short} 제외")
    print(f"학습 후보: {len(train_rows)}")

    # ---- 누수 게이트 (T3): 학습 slug ∩ (평가 페이지 ∪ 지지 claim) == ∅ ----
    overlap = {r.slug for r in train_rows} & excluded
    assert not overlap, f"누수! 학습에 평가 관련 {len(overlap)}건: {sorted(overlap)[:5]}"
    print(f"[게이트] 학습 ∩ 평가관련 = ∅  ✅ (평가 {len(eval_pages)}페이지 + 지지 claim "
          f"{len(excluded) - len(eval_pages)}개 모두 배제)")

    if args.limit:
        train_rows = train_rows[: args.limit]

    # ---- 하드 네거티브: DB Solar 벡터로 in-memory top-k (LLM 호출 0) ----
    # family(정답)에 속한 청크는 네거티브에서 뺀다 — 같은 지식이라 밀어내면 안 된다.
    vecs = np.array([np.asarray(r.vec, dtype=np.float32) for r in train_rows])
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    fams = [family_of(r.slug, r.kind, page_to_claims, claim_to_page) for r in train_rows]
    slugs = [r.slug for r in train_rows]

    print(f"하드 네거티브 계산 중... ({len(train_rows)}x{len(train_rows)} 코사인)")
    negs: list[list[int]] = []
    B = 512
    for i in range(0, len(vecs), B):
        sims = vecs[i : i + B] @ vecs.T
        for j, row in enumerate(sims):
            gi = i + j
            row[gi] = -2.0
            cand = np.argsort(-row)[: args.negatives + 24]
            picked = [int(c) for c in cand if slugs[c] not in fams[gi]][: args.negatives]
            negs.append(picked)
    print(f"  네거티브 완료 (쌍당 {args.negatives}개, family 제외)")

    if args.dry_run:
        print("\n[DRY RUN] LLM 호출 없음. 샘플 3건의 네거티브:")
        for i in (0, 1, 2):
            print(f"  [{train_rows[i].kind}] {slugs[i]!r}")
            for n in negs[i][:2]:
                print(f"      neg: {slugs[n]!r} ({train_rows[n].content[:45]!r})")
        return

    # ---- 질문 생성 (Solar, 동시 N) ----
    # 평가 질문과 **같은 real 스타일/형태 배분**을 쓴다. 도메인+질의분포 적응이 파인튜닝의
    # 목적이고, 페이지가 disjoint하므로 공정하다.
    chat, model_label = build_chat("solar")
    print(f"질문 생성: {len(train_rows)}건 · {model_label} · 동시 {args.concurrency}")
    print("⚠️  회사 위키 원문을 Upstage 외부 API로 전송한다(프로덕션 임베딩과 동일 경로).")

    out: list[dict | None] = [None] * len(train_rows)
    done = threading.Lock()
    counter = {"n": 0, "fail": 0}

    def work(i: int) -> None:
        r = train_rows[i]
        form = form_for("real", i)
        user = f"{form}\n\n제목: {r.title}\n\n내용:\n{r.content[:1500]}"
        try:
            raw = chat.complete(STYLES["real"], user, json_only=True)
            q = json.loads(raw)["question"].strip()
            if q:
                out[i] = {
                    "q": q,
                    "pos": r.content,
                    "negs": [train_rows[n].content for n in negs[i]],
                    "slug": r.slug,
                    "kind": r.kind,
                }
        except Exception:  # noqa: BLE001
            with done:
                counter["fail"] += 1
        with done:
            counter["n"] += 1
            if counter["n"] % 500 == 0:
                print(f"  {counter['n']}/{len(train_rows)} (실패 {counter['fail']})", flush=True)

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(work, range(len(train_rows))))

    pairs = [p for p in out if p]
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # ---- 최종 누수 재검증 (기록용) ----
    assert not ({p["slug"] for p in pairs} & excluded), "누수! 최종 산출물에 평가 관련 slug"
    import statistics as _st

    L = [len(p["q"]) for p in pairs]
    print(f"\n학습쌍 {len(pairs)}건 (실패 {counter['fail']}) → {OUT_PATH}")
    print(f"  질문 길이 p50={_st.median(L):.0f}자 | page {sum(1 for p in pairs if p['kind'] == 'page')} "
          f"/ claim {sum(1 for p in pairs if p['kind'] == 'claim')}")
    print(f"  [게이트] 최종 산출물 ∩ 평가관련 = ∅  ✅")
    for p in pairs[:2]:
        print(f"    {p['slug']!r} → {p['q']!r}")


if __name__ == "__main__":
    main()
