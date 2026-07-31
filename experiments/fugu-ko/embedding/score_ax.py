"""A.X-ft vs A.X-zeroshot vs Solar(프로덕션) 검색 품질 채점 — 배선 없이 인메모리.

arm 3종. **pool·질문·관측깊이·lexical arm 전부 공유**한다:
  solar_prod  : DB에 저장된 프로덕션 Solar 벡터를 SQL cosine으로 (= 지금 돌아가는 그 배선)
  ax_ft       : 파인튜닝한 A.X (A100에서 임베딩해 .npy로 회수)
  ax_zeroshot : 파인튜닝 전 A.X (같은 ax_encoder.encode 경로 — 차이는 가중치뿐)

⚠️ arm 이름 주의: 선행 하네스의 `openai_1024` arm은 "DB 벡터를 SQL로 조회"인데,
   2026-07-17 로컬 노드가 Solar로 전환돼 **그 DB 벡터가 이제 Solar다**(인계문서 T1).
   그래서 여기선 그 arm을 `solar_prod`로 부른다 — 조용히 틀린 이름을 남기지 않는다.

★ rank_of를 고쳤다 (선행 하네스의 실측 버그):
   `mirror_retrieve.rank_of`는 `expected in slug` **부분 문자열** 매칭이다. 그런데 pool은
   page 2,618 + claim 7,535이고 claim slug는 `<page-slug>-<hash>` 꼴이라, 실측 결과
   **평가 83/83 전 케이스가 충돌**한다:
     - `aws-operating-cost` ⊂ claim `aws-operating-cost-b906b823`  (페이지를 못 찾아도 정답 처리)
     - `confirmation`       ⊂ page  `message-confirmation`          (**다른 페이지**를 정답 처리)
   전 arm이 같은 함수를 쓰니 공정성은 부분적으로 유지되지만, claim을 상위로 올리는 성향이
   모델마다 다르면 그대로 편향이 된다. 그래서 두 지표를 낸다:
     strict  = 기대 페이지 정확 일치           ← **대표 지표**
     lenient = 기대 페이지 ∪ 그 페이지의 supports claim (같은 지식이므로 인정)
   부호가 갈리면 결론을 보류한다.

사용:
    python score_ax.py --qvecs ax_q_{ft,zs}_{gen}.npy --pvecs ax_pool_{ft,zs}.npy
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from orthus.models.registry import get_embedding_model
from orthus.tables import embeddings, wiki_chunks, wiki_pages
from orthus.wiki.retrieve import _candidate_from_row, _lexical_candidates, _merge_candidates

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ablation import DEPTH, paired_rank  # noqa: E402  — 검정 로직 재사용(재구현 금지)
from mirror_retrieve import (  # noqa: E402
    HERE,
    K,
    build_pool,
    cosine_sims,
    lexical_limit,
    load_cases,
    vector_limit,
)

OUT_PATH = f"{HERE}/ax_results.json"


class _PoolArgs:
    """--full-corpus: 평가 pool을 company 전 청크(10,153)로 둔다.

    선행 실험이 distractor를 1,500으로 제한한 건 pool을 Solar API로 재임베딩하는 비용
    때문이었다. 이번엔 Solar arm이 DB 벡터(SQL)라 API 비용이 0이고 A.X는 GPU라 10k도
    몇 분이다. 프로덕션 노출과 동일한 난이도가 더 현실적이고, 선행 실험이 겪은
    천장효과(gpt 슬라이스 hit@5 0.95 포화)도 완화된다.
    """

    def __init__(self, distractors=0, seed=20260717, full_corpus=True):
        self.distractors = distractors
        self.seed = seed
        self.full_corpus = full_corpus


def rank_exact(slugs: list[str], expected: str) -> int | None:
    """기대 페이지 **정확 일치** 순위. 부분 문자열 금지."""
    for i, s in enumerate(slugs, start=1):
        if s == expected:
            return i
    return None


def rank_lenient(slugs: list[str], accept: set[str]) -> int | None:
    """기대 페이지 또는 그 페이지의 supports claim 중 가장 높은 순위."""
    for i, s in enumerate(slugs, start=1):
        if s in accept:
            return i
    return None


def load_accept_map(s, eval_slugs: list[str]) -> dict[str, set[str]]:
    """페이지 → {페이지} ∪ supports claim. lenient 지표용."""
    rows = s.execute(
        text(
            """
            select p.slug, l.dst_slug from wiki_links l
            join wiki_pages p on p.page_id = l.src_page_id
            where p.scope='company' and p.kind='page' and l.rel='supports' and p.slug = any(:ev)
            """
        ),
        {"ev": eval_slugs},
    ).all()
    acc: dict[str, set[str]] = {s_: {s_} for s_ in eval_slugs}
    for page, claim in rows:
        acc[page].add(claim)
    return acc


def summarize(rows: list[dict], key: str) -> dict:
    n = len(rows) or 1
    return {
        "n": len(rows),
        f"hit@{K}": round(sum(1 for r in rows if r[key] and r[key] <= K) / n, 3),
        "mrr": round(sum(1.0 / r[key] for r in rows if r[key]) / n, 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="A.X-ft vs Solar 채점")
    ap.add_argument("--vecdir", default=HERE, help=".npy 위치")
    ap.add_argument("--dry-run", action="store_true", help=".npy 없이 pool/케이스/lexical만 검증")
    args = ap.parse_args()

    cases_by_gen = load_cases()
    # cases_*.json 중 real 스타일만 (verbose 잔재 방지)
    cases_by_gen = {g: c for g, c in cases_by_gen.items() if g.endswith("-real")}
    engine = create_engine(
        os.environ["ORTHUS_PG_DSN_READONLY"],
        connect_args={"options": "-c statement_timeout=180000"},
    )

    with Session(engine) as s:
        base_where = build_pool(s, cases_by_gen, _PoolArgs())
        pool_rows = s.execute(
            select(
                wiki_chunks.c.content, wiki_pages.c.page_id, wiki_pages.c.slug,
                wiki_pages.c.title, wiki_pages.c.kind, wiki_pages.c.scope, embeddings.c.project,
            )
            .select_from(
                wiki_chunks.join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
                .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
            )
            .where(*base_where)
        ).all()
        eval_slugs = sorted({c["expected_slug"] for rows in cases_by_gen.values() for c in rows})
        accept = load_accept_map(s, eval_slugs)

    print(f"pool: {len(pool_rows)} chunks (page {sum(1 for r in pool_rows if r.kind == 'page')} "
          f"+ claim {sum(1 for r in pool_rows if r.kind == 'claim')}) | "
          f"생성기: { {g: len(c) for g, c in cases_by_gen.items()} }")
    print(f"lenient 허용집합: 페이지당 평균 {sum(len(v) for v in accept.values()) / len(accept):.1f}개")

    if args.dry_run:
        first = next(iter(cases_by_gen.values()))[0]
        with Session(engine) as s:
            probe = _lexical_candidates(s, first["question"], base_where, limit=lexical_limit(K))
        print(f"[체크] lexical arm 후보: {len(probe)}건")
        print(f"[체크] 프로덕션 임베딩 슬롯: {get_embedding_model().model_version}")
        print(f"[체크] pool 텍스트 {sum(len(r.content) for r in pool_rows):,}자 → A100에서 A.X 임베딩 필요")
        # A100으로 넘길 pool 텍스트를 순서 그대로 저장한다(순서가 곧 인덱스 계약).
        with open(f"{HERE}/pool_texts.json", "w", encoding="utf-8") as f:
            json.dump([r.content for r in pool_rows], f, ensure_ascii=False)
        with open(f"{HERE}/pool_keys_ax.json", "w", encoding="utf-8") as f:
            json.dump([r.slug for r in pool_rows], f, ensure_ascii=False)
        for g, cs in cases_by_gen.items():
            with open(f"{HERE}/q_texts_{g}.json", "w", encoding="utf-8") as f:
                json.dump([c["question"] for c in cs], f, ensure_ascii=False)
        print(f"[산출] pool_texts.json / q_texts_*.json — A100으로 scp 해라")
        return

    # --- A100에서 회수한 A.X 벡터 (순서 = pool_rows / cases 순서) ---
    pool_slugs_saved = json.load(open(f"{HERE}/pool_keys_ax.json", encoding="utf-8"))
    assert pool_slugs_saved == [r.slug for r in pool_rows], (
        "pool 구성이 임베딩 때와 다르다 — .npy를 다시 만들어라(위키가 바뀌었다)"
    )
    # ft = 전체 pool 학습(positive 74% claim) · ftp = page-only positive 학습 · zs = 원본 백본
    ax = {
        v: {"pool": np.load(f"{args.vecdir}/ax_pool_{v}.npy")}
        for v in ("ft", "ftp", "zs")
    }
    for v in ax:
        assert len(ax[v]["pool"]) == len(pool_rows), f"ax_pool_{v} 길이 불일치"
        for g in cases_by_gen:
            ax[v][g] = np.load(f"{args.vecdir}/ax_q_{v}_{g}.npy")
            assert len(ax[v][g]) == len(cases_by_gen[g]), f"ax_q_{v}_{g} 길이 불일치"

    solar_embedder = get_embedding_model()
    pool_project = np.array([r.project for r in pool_rows])
    results: dict[str, list[dict]] = {}

    with Session(engine) as s:
        for gen, cases in cases_by_gen.items():
            print(f"\n=== 생성기: {gen} ({len(cases)}) ===")
            for ci, c in enumerate(cases):
                q = c["question"]
                # ★ project 필터를 걸지 않는다 — 프로덕션 `retrieve()`의 기본이 project=None
                #   (전 프로젝트)이기 때문이다(retrieve.py §130-131). 선행 하네스는 케이스마다
                #   project로 좁혔는데, 그러면 orbit 질문이 279청크만 상대해 과제가 붕괴한다
                #   (선행 실험이 보고한 hit@5 0.95 천장효과의 유력한 원인). 하네스 자신도
                #   모순됐다 — build_pool 주석은 "prod ranks against ~10k chunks"라면서
                #   채점 루프는 좁혔다. 전 arm이 같은 10,153 pool을 상대하므로 공정하고,
                #   프로덕션 기본값과 일치하며, 변별력도 산다.
                where = list(base_where)
                masked = list(pool_rows)

                # --- solar_prod arm: 프로덕션 배선 그대로 (DB 벡터 SQL cosine) ---
                qv = solar_embedder.embed([q])[0]
                dist = embeddings.c.vec.cosine_distance(qv).label("distance")
                solar_v = [
                    _candidate_from_row(r, 1.0 - float(r.distance), "vector")
                    for r in s.execute(
                        select(wiki_pages.c.page_id, wiki_pages.c.slug, wiki_pages.c.title,
                               wiki_pages.c.kind, wiki_pages.c.scope, wiki_chunks.c.content, dist)
                        .join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
                        .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
                        .where(*where).order_by(dist, embeddings.c.embedding_id).limit(DEPTH)
                    ).all()
                ]
                arms = [("solar_prod", solar_v)]

                # --- ax arm 2종: 인메모리 코사인 (같은 encode 경로, 차이는 가중치뿐) ---
                for v in ("ft", "ftp", "zs"):
                    sims = cosine_sims(ax[v][gen][ci], ax[v]["pool"])
                    arms.append((
                        f"ax_{v}",
                        [_candidate_from_row(masked[j], float(sims[j]), "vector")
                         for j in np.argsort(-sims)[:DEPTH]],
                    ))

                # --- lexical arm: 전 arm 공유 (프로덕션 코드) ---
                lex = _lexical_candidates(s, q, where, limit=lexical_limit(K))
                acc = accept[c["expected_slug"]]

                for name, vrows in arms:
                    for mode, rows_ in (
                        ("veconly", sorted(vrows, key=lambda r: -r["score"])[:DEPTH]),
                        ("hybrid", _merge_candidates(vrows + lex, DEPTH)),
                    ):
                        slugs = [r["slug"] for r in rows_]
                        results.setdefault(f"{gen}|{name}|{mode}", []).append({
                            "case_id": c["case_id"],
                            "strict": rank_exact(slugs, c["expected_slug"]),
                            "lenient": rank_lenient(slugs, acc),
                        })
                if (ci + 1) % 20 == 0:
                    print(f"  {ci + 1}/{len(cases)}", flush=True)

    json.dump({"detail": results}, open(OUT_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # ---------------- 리포트 ----------------
    def pooled(name: str, mode: str) -> list[dict]:
        out: list[dict] = []
        for gen in cases_by_gen:
            out += [{**r, "case_id": f"{gen}:{r['case_id']}"} for r in results[f"{gen}|{name}|{mode}"]]
        return out

    print("\n" + "=" * 78)
    print("판정 = veconly/strict (대표). hybrid·lenient는 강건성 확인용.")
    for metric in ("strict", "lenient"):
        print(f"\n[{metric}]  " + ("기대 페이지 정확 일치" if metric == "strict"
                                   else "기대 페이지 ∪ 그 페이지의 claim"))
        for mode in ("veconly", "hybrid"):
            print(f"\n  {mode}:")
            print(f"    {'생성기':10} {'Solar MRR':>10} {'A.X-ft':>9} {'A.X-ftp':>10} {'A.X-zs':>9} {'ftp-Solar':>10}")
            for gen in cases_by_gen:
                r = {n: summarize(results[f"{gen}|{n}|{mode}"], metric)["mrr"]
                     for n in ("solar_prod", "ax_ft", "ax_ftp", "ax_zs")}
                print(f"    {gen:10} {r['solar_prod']:>10.4f} {r['ax_ft']:>9.4f} "
                      f"{r['ax_ftp']:>10.4f} {r['ax_zs']:>9.4f} "
                      f"{r['ax_ftp'] - r['solar_prod']:>+10.4f}")

    # 검정: 합산 슬라이스에서 Wilcoxon (ablation.paired_rank 재사용)
    print("\n" + "=" * 78)
    print("검정 — Wilcoxon signed-rank (RR=1/rank, 관측깊이 40, 전 생성기 합산)")
    for metric in ("strict", "lenient"):
        print(f"\n  [{metric}]")
        print(f"    {'비교':26} {'MRR_A':>7} {'MRR_B':>7} {'차이':>8} {'순위변화':>8} {'p':>7}  판정")
        for mode in ("veconly", "hybrid"):
            for a, b in (("ax_ftp", "solar_prod"), ("ax_ft", "solar_prod"),
                         ("ax_ftp", "ax_ft"), ("ax_ft", "ax_zs")):
                ra = [{**r, "rank": r[metric]} for r in pooled(a, mode)]
                rb = [{**r, "rank": r[metric]} for r in pooled(b, mode)]
                r = paired_rank(ra, rb)
                v = "완전동일" if r["n_changed"] == 0 else ("유의" if r["p"] < 0.05 else "검출못함")
                print(f"    {mode + ': ' + a + ' vs ' + b:26} {r['mrr_a']:>7.4f} {r['mrr_b']:>7.4f} "
                      f"{r['delta']:>+8.4f} {r['n_changed']:>8}/{r['n']:<4} {r['p']:>7.3f}  {v}")

    # 판정표 (인계문서 Step 5)
    ft = paired_rank([{**r, "rank": r["strict"]} for r in pooled("ax_ftp", "veconly")],
                     [{**r, "rank": r["strict"]} for r in pooled("solar_prod", "veconly")])
    ratio = ft["mrr_a"] / ft["mrr_b"] if ft["mrr_b"] else 0.0
    print("\n" + "=" * 78)
    print(f"★ 대표 판정 (veconly/strict): 최선 A.X(=ftp) MRR {ft['mrr_a']:.4f} vs Solar {ft['mrr_b']:.4f} "
          f"= Solar의 {ratio:.1%}  (p={ft['p']:.3f})")
    print("   > Solar        → 배선 진행(768→1024 projection 필요)" if ratio > 1.0 else
          "   ≥ Solar의 95%  → 경계. 대회 서사로는 충분, 추가 튜닝 검토" if ratio >= 0.95 else
          "   < Solar의 95%  → 중단. 결과를 T4 가설(한국어 특화 이점 소멸) 검증으로 기록")
    print(f"\n상세: {OUT_PATH}")


if __name__ == "__main__":
    main()
