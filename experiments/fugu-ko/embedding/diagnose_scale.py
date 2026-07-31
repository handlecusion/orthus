"""hybrid 역전의 원인 진단: 두 임베딩의 점수 스케일이 다른가?

`_merge_candidates`는 `base = max(vec, lex)`로 **벡터 점수와 lexical 점수를 직접 크기
비교**한다. lexical은 0.45~1.0 밴드로 보정돼 있으므로, 벡터 점수가 그 밴드보다 낮게
깔리는 모델은 lexical에 가려지고, 높게 깔리는 모델은 lexical을 덮어버린다.
→ 블렌드 상수가 특정 모델의 점수 분포에 맞춰져 있으면 hybrid 비교는 불공정하다.

Solar 벡터는 캐시(.npy)에서 읽으므로 Upstage 재호출은 없다. OpenAI 질의 임베딩만
새로 호출한다(기존 prod 경로).
"""
from __future__ import annotations

import json
import os

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from orthus.models.registry import get_embedding_model
from orthus.tables import embeddings, wiki_chunks, wiki_pages
from orthus.wiki.retrieve import _lexical_candidates

from mirror_retrieve import (  # noqa: E402
    HERE,
    K,
    build_pool,
    cosine_sims,
    lexical_limit,
    load_cases,
    vector_limit,
)


class _Args:
    full_corpus = False
    distractors = 1500
    seed = 20260716


def pct(a, p):
    return float(np.percentile(a, p)) if len(a) else float("nan")


def main() -> None:
    cases_by_gen = load_cases()
    engine = create_engine(
        os.environ["ORTHUS_PG_DSN_READONLY"],
        connect_args={"options": "-c statement_timeout=120000"},
    )
    with Session(engine) as s:
        base_where = build_pool(s, cases_by_gen, _Args())
        pool_rows = s.execute(
            select(
                wiki_chunks.c.content,
                wiki_pages.c.page_id,
                wiki_pages.c.slug,
                wiki_pages.c.title,
                wiki_pages.c.kind,
                wiki_pages.c.scope,
                embeddings.c.project,
            )
            .select_from(
                wiki_chunks.join(
                    embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id
                ).join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
            )
            .where(*base_where)
        ).all()

    pool_vecs = np.load(f"{HERE}/solar_pool_vectors.npy")
    pool_project = np.array([r.project for r in pool_rows])
    openai_embedder = get_embedding_model()

    # gpt 슬라이스 30문항으로 분포만 본다(스케일 진단엔 충분).
    cases = cases_by_gen["gpt"][:30]
    solar_qvecs = np.load(f"{HERE}/solar_qvecs_gpt.npy")[:30]

    o_top, s_top, lex_top = [], [], []
    o_all, s_all = [], []

    with Session(engine) as s:
        for ci, c in enumerate(cases):
            where = list(base_where)
            if c["project"]:
                where.append(embeddings.c.project == c["project"])

            qvec = openai_embedder.embed([c["question"]])[0]
            distance = embeddings.c.vec.cosine_distance(qvec).label("distance")
            rows = s.execute(
                select(wiki_pages.c.slug, distance)
                .join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
                .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
                .where(*where)
                .order_by(distance, embeddings.c.embedding_id)
                .limit(vector_limit(K))
            ).all()
            osc = [1.0 - float(r.distance) for r in rows]
            o_top.append(osc[0])
            o_all.extend(osc)

            mask = pool_project == c["project"] if c["project"] else np.ones(len(pool_rows), bool)
            sims = cosine_sims(solar_qvecs[ci], pool_vecs[mask])
            ssc = sorted(sims, reverse=True)[: vector_limit(K)]
            s_top.append(float(ssc[0]))
            s_all.extend([float(x) for x in ssc])

            lex = _lexical_candidates(s, c["question"], where, limit=lexical_limit(K))
            if lex:
                lex_top.append(max(r["score"] for r in lex))

    print("=" * 68)
    print("벡터 점수 분포 (gpt 슬라이스 30문항)")
    print(f"  {'':22} {'p10':>7} {'중앙':>7} {'p90':>7} {'최대':>7}")
    for name, arr in [("OpenAI 후보 전체", o_all), ("Solar 후보 전체", s_all)]:
        a = np.array(arr)
        print(f"  {name:22} {pct(a,10):7.3f} {pct(a,50):7.3f} {pct(a,90):7.3f} {a.max():7.3f}")
    for name, arr in [("OpenAI 1위", o_top), ("Solar 1위", s_top), ("lexical 1위", lex_top)]:
        a = np.array(arr)
        print(f"  {name:22} {pct(a,10):7.3f} {pct(a,50):7.3f} {pct(a,90):7.3f} {a.max():7.3f}")

    o, s_, lx = np.array(o_top), np.array(s_top), np.array(lex_top)
    print("\n" + "=" * 68)
    print("블렌드 영향: base = max(vec, lex) 에서 어느 팔이 이기나")
    n = min(len(o), len(lx))
    print(f"  OpenAI 벡터가 lexical을 이긴 비율: {(o[:n] > lx[:n]).mean():.0%}")
    print(f"  Solar  벡터가 lexical을 이긴 비율: {(s_[:n] > lx[:n]).mean():.0%}")
    print(f"\n  중앙값 — OpenAI 벡터 {np.median(o):.3f} | Solar 벡터 {np.median(s_):.3f} "
          f"| lexical {np.median(lx):.3f}")
    gap = np.median(s_) - np.median(o)
    print(f"  → Solar 벡터 점수가 OpenAI보다 중앙값 {gap:+.3f} 만큼 {'높다' if gap>0 else '낮다'}")
    print("\n해석: lexical은 0.45~1.0 밴드로 보정돼 있다. 벡터 점수가 그 밴드와 다른 위치에")
    print("      깔리면 max(vec,lex) 비교가 한쪽으로 기운다 → hybrid 수치는 임베딩 품질이")
    print("      아니라 **블렌드 상수의 보정 대상**을 반영한다.")


if __name__ == "__main__":
    main()
