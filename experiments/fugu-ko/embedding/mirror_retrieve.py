"""OpenAI(prod) vs Solar 임베딩을 같은 hybrid retrieve() 파이프라인에서 비교.

공정성 설계:
- lexical arm(임베딩 무관)은 실제 orthus.wiki.retrieve 함수를 **그대로 재사용**한다.
- vector arm만 OpenAI(DB 저장 벡터, SQL cosine)와 Solar(인메모리, 새 임베딩)로 분기.
- 청크 pool은 생성기와 무관하게 **공유**한다(임베딩 1회). 질문 벡터만 생성기별로 다르다.

생성기 편향 교차검증(왜 여러 생성기인가):
  질문을 gpt로만 만들면 GPT 표현이 GPT 임베딩에 유리할 수 있다. 같은 83페이지에 대해
  gpt/claude/solar가 각각 질문을 쓰고 **생성기별로 따로 채점**하면, 각 임베딩이 자기
  벤더 질문에서만 유독 잘하는지 보인다. `cases_{gen}.json`이 있는 생성기만 자동 로드한다.

지표 4종:
  {openai,solar} × {hybrid, veconly}
  - hybrid  = 실제 프로덕션 랭킹(vector+lexical 블렌드) → 사용자 체감
  - veconly = 임베딩 신호만 격리 → 교체의 순수 영향

DB는 orthus_ro read-only role로만 붙고 쓰기는 없다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import time

import httpx
import numpy as np
from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import Session

from orthus.models.registry import get_embedding_model
from orthus.tables import embeddings, wiki_chunks, wiki_pages
from orthus.wiki.retrieve import (
    _blocked_company_provenance_slugs,
    _candidate_from_row,
    _lexical_candidates,
    _merge_candidates,
    _not_blocked_company_page,
)

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_PATH = os.environ.get(
    "FUGU_KEYS", "<로컬 키 저장소>/keys.json"
)
POOL_CACHE = f"{HERE}/solar_pool_vectors.npy"
POOL_KEY_CACHE = f"{HERE}/solar_pool_keys.json"
OUT_PATH = f"{HERE}/mirror_results.json"
K = 5
SOLAR_URL = "https://api.upstage.ai/v1/embeddings"


def load_solar_key() -> str:
    raw = json.load(open(KEYS_PATH, encoding="utf-8"))
    entry = next(e for e in raw if "upstage" in str(e.get("provider", "")).lower())
    return entry["key"]


def load_cases() -> dict[str, list[dict]]:
    """cases_{gen}.json 을 전부 로드. 생성기별 dict."""
    out: dict[str, list[dict]] = {}
    for path in sorted(glob.glob(f"{HERE}/cases_*.json")):
        gen = os.path.basename(path)[len("cases_") : -len(".json")]
        out[gen] = json.load(open(path, encoding="utf-8"))
    if not out:
        raise SystemExit("cases_*.json 없음 — 먼저 gen_questions.py 를 돌려라.")
    return out


def solar_embed(texts: list[str], model: str, key: str, *, batch: int = 90, dims: int = 1024):
    vecs: list = [None] * len(texts)
    for i in range(0, len(texts), batch):
        body = {"model": model, "input": texts[i : i + batch], "dimensions": dims}
        r = httpx.post(SOLAR_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=60)
        r.raise_for_status()
        for d in r.json()["data"]:
            vecs[i + d["index"]] = d["embedding"]
        time.sleep(0.15)
        if (i // batch) % 10 == 0 and i:
            print(f"    solar embed {i}/{len(texts)}")
    return np.array(vecs, dtype=np.float32)


def cosine_sims(qvec, mat):
    qn = qvec / (np.linalg.norm(qvec) + 1e-9)
    mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    return mn @ qn


def vector_limit(k: int) -> int:
    return max(k * 8, 40)


def lexical_limit(k: int) -> int:
    return max(k * 40, 200)


def rank_of(slugs: list[str], expected: str) -> int | None:
    for i, slug in enumerate(slugs, start=1):
        if expected in slug:
            return i
    return None


_RRF_K = 60  # 표준값(Cormack et al. 2009)


def rrf_merge(vec_rows: list[dict], lex_rows: list[dict], k: int) -> list[dict]:
    """Reciprocal Rank Fusion — 점수 **순위**만 쓰므로 스케일에 무관하다.

    왜 필요한가: 프로덕션 `_merge_candidates`는 `base = max(vec, lex)`로 벡터 점수를
    lexical 점수(0.45~1.0 밴드로 보정됨)와 직접 크기 비교한다. 두 임베딩의 코사인 분포가
    다르면(실측: Solar 중앙값 0.515 vs OpenAI 0.592) 낮게 깔리는 쪽은 자기 벡터 팔이
    lexical에 먹혀 hybrid가 나빠진다 — 순위가 더 좋아도 그렇다. RRF는 그 교란을 제거해
    "블렌드 상수를 재보정하면 어떻게 되나"를 근사한다.
    """
    scores: dict = {}
    meta: dict = {}
    for rows in (vec_rows, lex_rows):
        for rank, r in enumerate(
            sorted(rows, key=lambda x: -x["score"]), start=1
        ):
            key = r["page_id"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            meta.setdefault(key, r)
    merged = [{**meta[key], "score": sc} for key, sc in scores.items()]
    return sorted(merged, key=lambda r: -r["score"])[:k]


def summarize(rows: list[dict]) -> dict:
    n = len(rows) or 1
    return {
        "n": len(rows),
        "hit@1": round(sum(1 for r in rows if r["rank"] == 1) / n, 3),
        f"hit@{K}": round(sum(1 for r in rows if r["rank"] and r["rank"] <= K) / n, 3),
        "mrr": round(sum(1.0 / r["rank"] for r in rows if r["rank"]) / n, 3),
    }


def build_pool(s, cases_by_gen: dict, args) -> list:
    blocked = _blocked_company_provenance_slugs(s)
    where = [embeddings.c.kind == "wiki_chunk", embeddings.c.scope == "company"]
    if blocked:
        where.append(_not_blocked_company_page(blocked))

    # Exposure control. The task is only meaningful if the right chunk must beat a
    # realistic field of wrong ones: prod ranks against ~10k chunks, so a pool of just
    # the answers makes both models look perfect and measures nothing.
    answer_slugs = {c["expected_slug"] for rows in cases_by_gen.values() for c in rows}
    if not args.full_corpus:
        answer_pred = wiki_pages.c.slug.in_(answer_slugs)
        if args.distractors > 0:
            # Sample in Python, not `ORDER BY random()` — the random sort over the full
            # join blows past orthus_ro's statement_timeout. Seeded so the distractor set
            # (and therefore the scores) reproduce across runs.
            all_ids = (
                s.execute(
                    select(wiki_chunks.c.chunk_id)
                    .select_from(
                        wiki_chunks.join(
                            embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id
                        ).join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
                    )
                    .where(*where, ~answer_pred)
                )
                .scalars()
                .all()
            )
            rng = random.Random(args.seed)
            ids = rng.sample(all_ids, min(args.distractors, len(all_ids)))
            print(f"distractor: {len(all_ids)}개 중 {len(ids)}개 표집 (seed={args.seed})")
            where.append(or_(answer_pred, wiki_chunks.c.chunk_id.in_(ids)))
        else:
            where.append(answer_pred)
    return where


def main() -> None:
    ap = argparse.ArgumentParser(description="OpenAI vs Solar 임베딩 검색 품질 비교")
    ap.add_argument("--dry-run", action="store_true", help="외부 전송 없이 env/DB/케이스/lexical 검증")
    ap.add_argument("--full-corpus", action="store_true", help="제한 해제 — 전 company 청크(노출 ~10k)")
    ap.add_argument(
        "--distractors",
        type=int,
        default=1500,
        help="정답 외 무작위 방해 청크 수(기본 1500). 0이면 랭킹 과제가 성립 안 해 무의미하다.",
    )
    ap.add_argument("--seed", type=int, default=20260716, help="distractor 표집 시드(재현용)")
    args = ap.parse_args()

    cases_by_gen = load_cases()
    # 역할 기본 statement_timeout은 10s인데, 이 박스는 dev server/agent가 붙으면 load가
    # 8코어를 넘겨 인덱스 쿼리도 굶어 죽는다(실측: load 13에서 4ms 쿼리가 10s 초과).
    # 역할 설정을 바꾸지 않고 이 연결에만 여유를 준다.
    engine = create_engine(
        os.environ["ORTHUS_PG_DSN_READONLY"],
        connect_args={"options": "-c statement_timeout=120000"},
    )
    solar_key = None if args.dry_run else load_solar_key()

    with Session(engine) as s:
        base_where = build_pool(s, cases_by_gen, args)
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

    gens = {g: len(rows) for g, rows in cases_by_gen.items()}
    print(f"pool: {len(pool_rows)} chunks | 생성기: {gens}")

    if args.dry_run:
        chars = sum(len(r.content) for r in pool_rows)
        nq = sum(gens.values())
        print("\n[DRY RUN] Solar 전송 없음. 실제 실행 시 외부로 나갈 분량:")
        print(f"  - 회사 위키 청크 {len(pool_rows)}건 (약 {chars:,}자) → embedding-passage")
        print(f"  - 질문 {nq}건 ({len(gens)}개 생성기) → embedding-query")
        print(f"  - 목적지: {SOLAR_URL}")
        print(f"\n[체크] keys.json: {os.path.exists(KEYS_PATH)}")
        print(f"[체크] prod 임베딩 슬롯: {get_embedding_model().model_version}")
        first = next(iter(cases_by_gen.values()))[0]
        with Session(engine) as s:
            probe = _lexical_candidates(s, first["question"], base_where, limit=lexical_limit(K))
        print(f"[체크] lexical arm: 첫 케이스 후보 {len(probe)}건")
        print("\n파이프라인 정상. 실제 실행은 --dry-run 을 빼라.")
        return

    # 청크 pool 임베딩(생성기 무관 → 1회). pool 구성이 바뀌면 캐시를 무효화한다.
    pool_keys = [str(r.page_id) + "|" + r.content[:40] for r in pool_rows]
    cache_ok = os.path.exists(POOL_CACHE) and os.path.exists(POOL_KEY_CACHE)
    if cache_ok and json.load(open(POOL_KEY_CACHE, encoding="utf-8")) == pool_keys:
        pool_vecs = np.load(POOL_CACHE)
        print(f"pool 벡터 캐시 재사용 {pool_vecs.shape}")
    else:
        if cache_ok:
            print("pool 구성이 바뀜 → 재임베딩")
        pool_vecs = solar_embed([r.content for r in pool_rows], "embedding-passage", solar_key)
        np.save(POOL_CACHE, pool_vecs)
        json.dump(pool_keys, open(POOL_KEY_CACHE, "w"), ensure_ascii=False)
        print(f"pool 임베딩 완료 {pool_vecs.shape}")

    openai_embedder = get_embedding_model()
    pool_project = np.array([r.project for r in pool_rows])
    results: dict[str, list[dict]] = {}

    for gen, cases in cases_by_gen.items():
        print(f"\n=== 생성기: {gen} ({len(cases)} 질문) ===")
        qcache = f"{HERE}/solar_qvecs_{gen}.npy"
        if os.path.exists(qcache):
            solar_qvecs = np.load(qcache)
        else:
            solar_qvecs = solar_embed([c["question"] for c in cases], "embedding-query", solar_key, batch=50)
            np.save(qcache, solar_qvecs)

        with Session(engine) as s:
            for ci, c in enumerate(cases):
                q, project = c["question"], c["project"]
                where = list(base_where)
                if project:
                    where.append(embeddings.c.project == project)

                # --- OpenAI vector arm: 실제 DB 벡터를 SQL cosine으로 ---
                qvec = openai_embedder.embed([q])[0]
                distance = embeddings.c.vec.cosine_distance(qvec).label("distance")
                openai_v = [
                    _candidate_from_row(r, 1.0 - float(r.distance), "vector")
                    for r in s.execute(
                        select(
                            wiki_pages.c.page_id,
                            wiki_pages.c.slug,
                            wiki_pages.c.title,
                            wiki_pages.c.kind,
                            wiki_pages.c.scope,
                            wiki_chunks.c.content,
                            distance,
                        )
                        .join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
                        .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
                        .where(*where)
                        .order_by(distance, embeddings.c.embedding_id)
                        .limit(vector_limit(K))
                    ).all()
                ]

                # --- Solar vector arm: 인메모리 cosine ---
                mask = np.ones(len(pool_rows), bool) if project is None else (pool_project == project)
                sims = cosine_sims(solar_qvecs[ci], pool_vecs[mask])
                masked = [r for r, m in zip(pool_rows, mask) if m]
                solar_v = [
                    _candidate_from_row(masked[j], float(sims[j]), "vector")
                    for j in np.argsort(-sims)[: vector_limit(K)]
                ]

                # --- lexical arm: 두 모델이 공유(임베딩 무관) ---
                lex = _lexical_candidates(s, q, where, limit=lexical_limit(K))

                for name, rows_ in [
                    ("openai_hybrid", _merge_candidates(openai_v + lex, K)),
                    ("solar_hybrid", _merge_candidates(solar_v + lex, K)),
                    ("openai_veconly", sorted(openai_v, key=lambda r: -r["score"])[:K]),
                    ("solar_veconly", sorted(solar_v, key=lambda r: -r["score"])[:K]),
                    ("openai_rrf", rrf_merge(openai_v, lex, K)),
                    ("solar_rrf", rrf_merge(solar_v, lex, K)),
                ]:
                    slugs = [r["slug"] for r in rows_]
                    results.setdefault(f"{gen}|{name}", []).append(
                        {
                            "case_id": c["case_id"],
                            "generator": gen,
                            "rank": rank_of(slugs, c["expected_slug"]),
                            "top": slugs[0] if slugs else None,
                            "expected": c["expected_slug"],
                        }
                    )
                if (ci + 1) % 20 == 0:
                    print(f"  {ci + 1}/{len(cases)}")

    summary = {k: summarize(v) for k, v in results.items()}
    json.dump(
        {"summary": summary, "detail": results}, open(OUT_PATH, "w", encoding="utf-8"),
        ensure_ascii=False, indent=2,
    )

    # --- 리포트: 생성기 × 모델 교차표 (편향이 여기서 보인다) ---
    labels = {
        "veconly": "임베딩 순수 영향 (스케일 무관 — 순위만)",
        "hybrid": "현행 프로덕션 블렌드 (max(vec,lex) — 스케일 민감!)",
        "rrf": "순위 융합 재보정 (스케일 무관 — 블렌드 재보정 근사)",
    }
    print("\n" + "=" * 72)
    for mode in ("veconly", "hybrid", "rrf"):
        print(f"\n[{mode}]  {labels[mode]}")
        print(f"  {'질문 작성':10} {'OpenAI hit@5':>14} {'Solar hit@5':>13} {'차이':>8}")
        diffs = []
        for gen in cases_by_gen:
            o = summary.get(f"{gen}|openai_{mode}", {}).get(f"hit@{K}")
            sv = summary.get(f"{gen}|solar_{mode}", {}).get(f"hit@{K}")
            if o is None or sv is None:
                continue
            diffs.append(sv - o)
            print(f"  {gen:10} {o:>14.3f} {sv:>13.3f} {sv - o:>+8.3f}")
        if diffs:
            sign = "Solar 우세" if all(d > 0 for d in diffs) else (
                "OpenAI 우세" if all(d < 0 for d in diffs) else "부호 불일치 → 결론 보류"
            )
            print(f"  {'':10} {'':14} {'전 생성기:':>13} {sign:>8}")

    print(f"\n상세: {OUT_PATH}")
    print("\n해석 순서:")
    print("  1. 부호가 생성기마다 뒤집히면 → 생성기 편향. 결론 내지 마라.")
    print("  2. veconly와 rrf가 일치하면 → 임베딩 자체의 진짜 신호.")
    print("  3. hybrid만 반대면 → **블렌드 상수 보정 문제**이지 임베딩 품질이 아니다")
    print("     (현행 상수는 text-embedding-3-small 분포에 맞춰져 있다. diagnose_scale.py 참조).")
    print("  4. 83문항이라 ±몇 %p는 노이즈다.")


if __name__ == "__main__":
    main()
