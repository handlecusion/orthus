"""진단: strict와 lenient가 갈리는 이유 — arm별 top-1의 kind(page/claim) 분포.

채점에서 strict(정확일치) 0.49 vs lenient(페이지∪claim) 0.79로 A.X-ft가 크게 갈렸다.
가설: **A.X-ft는 올바른 지식을 찾지만 page 대신 claim을 위에 올린다.** 학습 positive가
claim 7,276 / page 2,521 = 74% claim이었으니 claim 문체를 정답으로 배웠을 수 있다.

이게 사실이면 결론이 "A.X가 검색을 못한다"가 아니라 "**학습셋 구성이 eval 표적과 어긋났다**"가
된다 — 전자는 모델 한계고 후자는 고칠 수 있는 설정 실수다. 그래서 따로 잰다.

pool·질문은 score_ax.py와 동일. Solar arm만 API를 다시 부른다(질문 166건).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from orthus.models.registry import get_embedding_model
from orthus.tables import embeddings, wiki_chunks, wiki_pages

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mirror_retrieve import HERE, build_pool, cosine_sims, load_cases  # noqa: E402
from score_ax import _PoolArgs, load_accept_map  # noqa: E402


def main() -> None:
    cases_by_gen = {g: c for g, c in load_cases().items() if g.endswith("-real")}
    engine = create_engine(
        os.environ["ORTHUS_PG_DSN_READONLY"],
        connect_args={"options": "-c statement_timeout=180000"},
    )
    with Session(engine) as s:
        base_where = build_pool(s, cases_by_gen, _PoolArgs())
        pool_rows = s.execute(
            select(wiki_chunks.c.content, wiki_pages.c.page_id, wiki_pages.c.slug,
                   wiki_pages.c.title, wiki_pages.c.kind, wiki_pages.c.scope, embeddings.c.project)
            .select_from(
                wiki_chunks.join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
                .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
            ).where(*base_where)
        ).all()
        eval_slugs = sorted({c["expected_slug"] for rows in cases_by_gen.values() for c in rows})
        accept = load_accept_map(s, eval_slugs)

    ax = {v: {"pool": np.load(f"{HERE}/ax_pool_{v}.npy")} for v in ("ft", "zs")}
    for v in ax:
        for g in cases_by_gen:
            ax[v][g] = np.load(f"{HERE}/ax_q_{v}_{g}.npy")

    emb = get_embedding_model()
    stats: dict[str, Counter] = {n: Counter() for n in ("solar_prod", "ax_ft", "ax_zs")}
    # "정답 지식을 맞혔지만 granularity만 틀린" 케이스: top-1이 정답 페이지의 claim인 경우
    right_knowledge_wrong_kind: dict[str, int] = dict.fromkeys(stats, 0)

    with Session(engine) as s:
        for gen, cases in cases_by_gen.items():
            for ci, c in enumerate(cases):
                exp, acc = c["expected_slug"], accept[c["expected_slug"]]

                qv = emb.embed([c["question"]])[0]
                dist = embeddings.c.vec.cosine_distance(qv).label("distance")
                top = s.execute(
                    select(wiki_pages.c.slug, wiki_pages.c.kind)
                    .select_from(
                        wiki_chunks
                        .join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
                        .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
                    )
                    .where(*base_where).order_by(dist, embeddings.c.embedding_id).limit(1)
                ).first()
                tops = {"solar_prod": (top.slug, top.kind)}

                for v in ("ft", "zs"):
                    j = int(np.argmax(cosine_sims(ax[v][gen][ci], ax[v]["pool"])))
                    tops[f"ax_{v}"] = (pool_rows[j].slug, pool_rows[j].kind)

                for name, (slug, kind) in tops.items():
                    stats[name][kind] += 1
                    if slug != exp and slug in acc:
                        right_knowledge_wrong_kind[name] += 1

    n = sum(len(c) for c in cases_by_gen.values())
    print(f"\n{'=' * 72}\ntop-1 결과의 kind 분포 (질문 {n}건)")
    print(f"  pool 자체 구성: page {sum(1 for r in pool_rows if r.kind == 'page')} "
          f"({sum(1 for r in pool_rows if r.kind == 'page') / len(pool_rows):.0%}) / "
          f"claim {sum(1 for r in pool_rows if r.kind == 'claim')}")
    print(f"  A.X 학습 positive 구성: page 2,521 (26%) / claim 7,276 (74%)  ← 가설의 원인\n")
    print(f"  {'arm':12} {'top1=page':>12} {'top1=claim':>12}   {'정답페이지의 claim을 1위로':>24}")
    for name in ("solar_prod", "ax_ft", "ax_zs"):
        p, c_ = stats[name]["page"], stats[name]["claim"]
        print(f"  {name:12} {p:>6} ({p / n:>4.0%}) {c_:>6} ({c_ / n:>4.0%})   "
              f"{right_knowledge_wrong_kind[name]:>10}건 ({right_knowledge_wrong_kind[name] / n:.0%})")
    print("\n읽는 법: 'ax_ft가 claim에 쏠리고 + 정답페이지의 claim을 1위로 올린 비율이 높다'면")
    print("  → strict 패배는 **학습셋이 74% claim이라 생긴 granularity 편향**이지 검색 무능이 아니다.")
    print("  → 그 경우 page-only positive로 재학습하면 결과가 달라질 수 있다(후속 과제).")


if __name__ == "__main__":
    main()
