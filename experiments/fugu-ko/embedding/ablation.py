"""후속 실험 2·3 — 비대칭 절제(ablation) + 차원(4096 vs 1024).

두 질문에 답한다:
  #2 비대칭 배선을 할 값어치가 있나?
     본 실험 수치는 Solar를 query/passage **분리**해서 냈다. 그런데 그 배선은 아직 없다
     (EmbeddingModel.embed()에 mode 인자 없음 — README §2). "대칭으로 쓰면 얼마나 손해인가"를
     재면 배선 작업의 값어치가 나온다. 손해 0이면 건너뛰고, 크면 "배선 없이 교체하면
     README 수치를 못 낸다"가 정량화된다.
     → passage 모델로 질의까지 임베딩(대칭) vs query 모델(비대칭) 비교.

  #3 스키마 때문에 Solar를 불리하게 쓰고 있나?
     pgvector vector(1024) 고정 탓에 Solar native 4096-d를 1024로 깎아 쓴다. 4096이 확실히
     낫다면 "스키마 마이그레이션할 값어치가 있다"는 새 선택지가 생긴다.
     → 같은 질문/청크를 4096-d로 재임베딩해 비교(인메모리라 스키마 변경 불필요).

⚠️ 회사 위키 원문을 Upstage로 전송한다(mirror_retrieve.py와 동일 경로/분량).
   에이전트가 못 돌리는 이유가 그것이다 — RUNBOOK.md STEP 6 참조.

lexical arm은 프로덕션 코드를 그대로 재사용하고, OpenAI 기준선도 같이 찍어 대조한다.
"""
from __future__ import annotations

import argparse
import json
import os
from math import comb

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from orthus.models.registry import get_embedding_model
from orthus.tables import embeddings, wiki_chunks, wiki_pages
from orthus.wiki.retrieve import _candidate_from_row, _lexical_candidates, _merge_candidates

from mirror_retrieve import (  # noqa: E402
    HERE,
    K,
    build_pool,
    cosine_sims,
    lexical_limit,
    load_cases,
    load_solar_key,
    rank_of,
    solar_embed,
    summarize,
    vector_limit,
)


class _PoolArgs:
    def __init__(self, distractors: int, seed: int, full_corpus: bool = False):
        self.distractors = distractors
        self.seed = seed
        self.full_corpus = full_corpus


def cache(path: str, build):
    if os.path.exists(path):
        return np.load(path)
    v = build()
    np.save(path, v)
    return v


DEPTH = vector_limit(K)  # 순위 관측 깊이(=40). hit@5는 rank<=5로 유도한다.


def exact_mcnemar(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    lo = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(lo + 1)) / (2**n))


def paired(rows_a: list[dict], rows_b: list[dict]) -> tuple[int, int, float]:
    """(a만 맞힘, b만 맞힘, p). 불일치 쌍이 McNemar의 실질 표본이다 — README §5 참조."""
    ha = {r["case_id"]: (r["rank"] is not None and r["rank"] <= K) for r in rows_a}
    hb = {r["case_id"]: (r["rank"] is not None and r["rank"] <= K) for r in rows_b}
    ids = sorted(set(ha) & set(hb))
    b = sum(1 for i in ids if ha[i] and not hb[i])
    c = sum(1 for i in ids if hb[i] and not ha[i])
    return b, c, exact_mcnemar(b, c)


def rr(rank) -> float:
    """Reciprocal rank. 미발견(DEPTH 밖)은 0."""
    return 1.0 / rank if rank else 0.0


def paired_rank(rows_a: list[dict], rows_b: list[dict]) -> dict:
    """순위 기반 쌍체 비교 — hit@5(이진)보다 훨씬 민감하다.

    왜: hit@5는 1위→3위 같은 실제 열화를 '변화 없음'으로 뭉갠다. 그래서 McNemar의 실질
    표본(불일치 쌍)이 0~4로 붕괴해 어떤 결과도 검정 불가가 된다(README §8.2). RR(=1/rank)
    차이에 Wilcoxon signed-rank를 걸면 **순위가 바뀐 모든 문항**이 표본이 되어 검정력이 산다.
    """
    ra = {r["case_id"]: r["rank"] for r in rows_a}
    rb = {r["case_id"]: r["rank"] for r in rows_b}
    ids = sorted(set(ra) & set(rb))
    da = [rr(ra[i]) for i in ids]
    db = [rr(rb[i]) for i in ids]
    deltas = [x - y for x, y in zip(da, db)]
    nonzero = [d for d in deltas if d != 0.0]
    p = float("nan")
    if len(nonzero) >= 1:
        from scipy.stats import wilcoxon

        try:
            p = float(wilcoxon(da, db, zero_method="wilcox").pvalue)
        except ValueError:  # 전부 동일 → 검정 불가
            p = 1.0
    return {
        "mrr_a": sum(da) / len(ids), "mrr_b": sum(db) / len(ids),
        "delta": (sum(da) - sum(db)) / len(ids),
        "n_changed": len(nonzero), "n": len(ids), "p": p,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="비대칭 절제 + 차원 실험")
    ap.add_argument("--dims", type=int, nargs="+", default=[1024, 4096],
                    help="비교할 차원 (기본 1024 4096)")
    ap.add_argument("--distractors", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=20260716)
    ap.add_argument(
        "--generators",
        nargs="+",
        default=None,
        help="사용할 질문 슬라이스(기본: 있는 것 전부). gpt 슬라이스는 hit@5가 0.95로 "
        "포화돼 변별력이 거의 없다 — claude/solar를 함께 써야 헤드룸이 생긴다.",
    )
    args = ap.parse_args()

    all_cases = load_cases()
    gens = args.generators or sorted(all_cases)
    missing = [g for g in gens if g not in all_cases]
    if missing:
        raise SystemExit(f"cases_{missing[0]}.json 없음 — gen_questions.py 먼저.")
    # 슬라이스를 합쳐 하나의 케이스 목록으로 본다(불일치 쌍을 최대한 모으려고).
    # case_id가 슬라이스 간 겹치므로 생성기 prefix를 붙여 구분한다.
    cases = [{**c, "case_id": f"{g}:{c['case_id']}"} for g in gens for c in all_cases[g]]

    engine = create_engine(
        os.environ["ORTHUS_PG_DSN_READONLY"],
        connect_args={"options": "-c statement_timeout=120000"},
    )
    key = load_solar_key()

    with Session(engine) as s:
        base_where = build_pool(s, {"all": cases}, _PoolArgs(args.distractors, args.seed))
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
    print(f"pool: {len(pool_rows)} chunks | 질문: {len(cases)} (슬라이스 {'+'.join(gens)})")

    texts = [r.content for r in pool_rows]
    qs = [c["question"] for c in cases]
    pool_project = np.array([r.project for r in pool_rows])

    # --- 임베딩 (차원별 × arm별). 캐시되므로 재실행은 API 무호출.
    # 질의 캐시 키에 슬라이스 조합을 넣는다 — 조합이 바뀌면 질문 목록도 바뀌므로
    # 같은 파일을 재사용하면 벡터와 케이스가 어긋난다.
    gkey = "-".join(gens)
    vecs: dict = {}
    for d in args.dims:
        print(f"\n[{d}-d] 임베딩...")
        vecs[d] = {
            # pool은 슬라이스와 무관 → 공유 (mirror_retrieve와 별개 캐시)
            "pool": cache(f"{HERE}/abl_pool_{d}.npy",
                          lambda d=d: solar_embed(texts, "embedding-passage", key, dims=d)),
            # 비대칭: 질의를 query 모델로 (프로덕션에 배선하려면 Protocol 수정 필요)
            "q_asym": cache(f"{HERE}/abl_q_asym_{gkey}_{d}.npy",
                            lambda d=d: solar_embed(qs, "embedding-query", key, batch=50, dims=d)),
            # 대칭: 질의도 passage 모델로 (배선 없이 그냥 교체했을 때)
            "q_sym": cache(f"{HERE}/abl_q_sym_{gkey}_{d}.npy",
                           lambda d=d: solar_embed(qs, "embedding-passage", key, batch=50, dims=d)),
        }
        for k in ("pool", "q_asym", "q_sym"):
            assert vecs[d][k].shape[1] == d, f"{k} 차원 불일치: {vecs[d][k].shape[1]} != {d}"
        assert len(vecs[d]["q_asym"]) == len(cases), "질의 캐시와 케이스 수 불일치 — .npy를 지워라"
        print(f"  pool {vecs[d]['pool'].shape} / q_asym {vecs[d]['q_asym'].shape} / q_sym {vecs[d]['q_sym'].shape}")

    openai_embedder = get_embedding_model()
    results: dict = {}

    with Session(engine) as s:
        for ci, c in enumerate(cases):
            where = list(base_where)
            if c["project"]:
                where.append(embeddings.c.project == c["project"])
            mask = (pool_project == c["project"]) if c["project"] else np.ones(len(pool_rows), bool)
            masked = [r for r, m in zip(pool_rows, mask) if m]
            lex = _lexical_candidates(s, c["question"], where, limit=lexical_limit(K))

            arms: list[tuple[str, list]] = []

            # OpenAI 기준선 (대조군)
            qv = openai_embedder.embed([c["question"]])[0]
            dist = embeddings.c.vec.cosine_distance(qv).label("distance")
            o_v = [
                _candidate_from_row(r, 1.0 - float(r.distance), "vector")
                for r in s.execute(
                    select(wiki_pages.c.page_id, wiki_pages.c.slug, wiki_pages.c.title,
                           wiki_pages.c.kind, wiki_pages.c.scope, wiki_chunks.c.content, dist)
                    .join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
                    .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
                    .where(*where).order_by(dist, embeddings.c.embedding_id).limit(vector_limit(K))
                ).all()
            ]
            arms.append(("openai_1024", o_v))

            # Solar: 차원 × (비대칭|대칭)
            for d in args.dims:
                for arm, qkey in (("asym", "q_asym"), ("sym", "q_sym")):
                    sims = cosine_sims(vecs[d][qkey][ci], vecs[d]["pool"][mask])
                    rows_ = [
                        _candidate_from_row(masked[j], float(sims[j]), "vector")
                        for j in np.argsort(-sims)[: vector_limit(K)]
                    ]
                    arms.append((f"solar_{d}_{arm}", rows_))

            # 순위를 DEPTH(=40)까지 관측한다. top-5로 자르면 6위와 40위가 똑같이 None이 되어
            # 정보를 통째로 버린다 — 그래서 hit@5 이진 지표만 남고 불일치 쌍이 0~4로 붕괴했다.
            # 깊은 순위를 남기면 "1위→3위로 밀림" 같은 변화가 보이고 Wilcoxon으로 검정된다.
            # hit@5는 rank<=5로 그대로 유도되므로 기존 지표와 호환된다.
            for name, vrows in arms:
                for mode, rows_ in (
                    ("veconly", sorted(vrows, key=lambda r: -r["score"])[:DEPTH]),
                    ("hybrid", _merge_candidates(vrows + lex, DEPTH)),
                ):
                    results.setdefault(f"{name}|{mode}", []).append(
                        {"case_id": c["case_id"],
                         "rank": rank_of([r["slug"] for r in rows_], c["expected_slug"])}
                    )
            if (ci + 1) % 20 == 0:
                print(f"  {ci + 1}/{len(cases)}")

    summary = {k: summarize(v) for k, v in results.items()}
    json.dump({"summary": summary, "detail": results},
              open(f"{HERE}/ablation_results.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # --- 리포트 ---
    hk = f"hit@{K}"
    base_v = summary["openai_1024|veconly"][hk]
    base_h = summary["openai_1024|hybrid"][hk]
    print("\n" + "=" * 78)
    print(f"[{len(cases)}문항, 슬라이스 {'+'.join(gens)}]  기준선 OpenAI-1024: "
          f"veconly {base_v:.3f} / hybrid {base_h:.3f}\n")
    print(f"  {'arm':22} {'veconly':>9} {'vs기준':>8} {'hybrid':>9} {'vs기준':>8}")
    for d in args.dims:
        for arm in ("asym", "sym"):
            k = f"solar_{d}_{arm}"
            v, h = summary[f"{k}|veconly"][hk], summary[f"{k}|hybrid"][hk]
            label = f"Solar {d}-d {'비대칭' if arm == 'asym' else '대칭'}"
            print(f"  {label:22} {v:>9.3f} {v - base_v:>+8.3f} {h:>9.3f} {h - base_h:>+8.3f}")

    # --- 판정: 순위 기반(민감) → 이진(참고) 순으로 ---
    # hit@5만 보면 1위→3위 같은 열화가 안 보여 불일치 쌍이 0~4로 붕괴한다(README §8.2).
    # RR(1/rank) + Wilcoxon은 순위가 바뀐 모든 문항을 쓰므로 검정력이 산다.
    def rank_line(label: str, a: str, b: str, mode: str) -> str:
        r = paired_rank(results[f"{a}|{mode}"], results[f"{b}|{mode}"])
        if r["n_changed"] == 0:
            verdict = "완전 동일(순위까지)"
        elif r["p"] < 0.05:
            verdict = "유의"
        else:
            verdict = "검출못함"
        return (f"  {label:22} {r['mrr_a']:>7.3f} {r['mrr_b']:>7.3f} {r['delta']:>+8.3f} "
                f"{r['n_changed']:>7}/{r['n']:<4} {r['p']:>7.3f}  {verdict}")

    def bin_line(label: str, a: str, b: str, mode: str) -> str:
        x, y, p = paired(results[f"{a}|{mode}"], results[f"{b}|{mode}"])
        note = "" if x + y >= 5 else "  ← 불일치 부족, 판정 불가"
        return f"  {label:22} {x:>3} / {y:<3} {x + y:>8} {p:>8.3f}{note}"

    print("\n" + "=" * 78)
    print("#2 비대칭 배선의 값어치 — 비대칭 vs 대칭")
    print(f"\n  [순위 기반 — 민감]  RR=1/rank, {DEPTH}위까지 관측, Wilcoxon")
    print(f"  {'':22} {'MRR비대':>7} {'MRR대칭':>7} {'차이':>8} {'순위변화':>7}     {'p':>7}")
    for d in args.dims:
        for mode in ("veconly", "hybrid"):
            print(rank_line(f"{d}-d {mode}", f"solar_{d}_asym", f"solar_{d}_sym", mode))
    print(f"\n  [이진 hit@{K} — 참고용, 정보 손실 큼]")
    print(f"  {'':22} {'비대칭만/대칭만':>3} {'불일치쌍':>8} {'p':>8}")
    for d in args.dims:
        for mode in ("veconly", "hybrid"):
            print(bin_line(f"{d}-d {mode}", f"solar_{d}_asym", f"solar_{d}_sym", mode))
    print("\n  → 배선하려면 Protocol에 mode 추가 + 4~6개 호출지점 수정(README §2).")
    print("    '완전 동일(순위까지)'면 두 arm이 같은 순위를 낸다는 뜻 = 배선 무의미.")

    if len(args.dims) > 1:
        lo, hi = min(args.dims), max(args.dims)
        print("\n" + "=" * 78)
        print(f"#3 차원의 값어치 — {hi} vs {lo} (비대칭 기준)")
        print(f"\n  [순위 기반 — 민감]")
        print(f"  {'':22} {f'MRR{hi}':>7} {f'MRR{lo}':>7} {'차이':>8} {'순위변화':>7}     {'p':>7}")
        for mode in ("veconly", "hybrid"):
            print(rank_line(f"{mode}", f"solar_{hi}_asym", f"solar_{lo}_asym", mode))
        print(f"\n  → 마이그레이션하려면 pgvector vector({lo})→({hi}) + openai_compat.py 가드 +")
        print(f"    전량 재임베딩. 인덱스/저장 {hi // lo}배.")
        print(f"    (주의: Upstage `dimensions`는 matryoshka 절단이 아니다 — {hi}-d의 앞 {lo}차원과")
        print(f"     {lo}-d 벡터는 실측 cos≈0.04로 사실상 직교다. 큰 차원이 정보 상위집합이 아니다.)")

    # 천장효과 경고 — gpt 슬라이스는 0.95라 변별력이 거의 없다.
    if base_v >= 0.93:
        print("\n⚠️ **천장효과**: 기준선 veconly가 {:.3f}로 포화 상태다. 이 슬라이스 구성에서는".format(base_v))
        print("   틀릴 여지가 거의 없어 불일치 쌍이 안 모인다 → 위 판정이 무력하다.")
        print("   `--generators claude solar`로 어려운 슬라이스를 쓰면 헤드룸이 생긴다.")

    print(f"\n상세: {HERE}/ablation_results.json")


if __name__ == "__main__":
    main()
