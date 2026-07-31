"""미니위키 실험 — 저작(distill) 모델이 downstream 답변을 바꾸나?

같은 70문서로 Solar·A.X·EXAONE distill로 각각 위키를 짓고(임베딩은 전부 Solar —
국내 임베딩 API가 Solar뿐), 같은 질문 세트로 검색+답변 품질을 잰다. 답변 모델은
Solar로 고정해 **위키 저작만 변수**로 격리한다.

지표:
  결정론 — granularity(pages·claims/doc), 거부율(위키가 근거 못 찾음 = 검색 실패 proxy)
  판정자 — codex 라운드로빈 쌍대(어느 위키 답이 더 나은가, 근거 동봉)

전용 DB orthus_miniwiki (전량 회사 위키 orthus와 분리). rebuild_all(chat_model=X)로
저작 모델 주입.

서브커맨드:
  gen-q                    source 문서에서 build-독립 질문 생성 (도메스틱 로테이션)
  build --distill <m>      해당 모델로 위키 재구축(clean→rebuild) + granularity 기록
  answer --distill <m>     그 위키에서 검색+답변(Solar 고정) → mw_ans_{m}.jsonl
  rrjudge --judge codex    3위키 답변 라운드로빈 판정
  report
"""

from __future__ import annotations

import argparse
import json
import os
import time
from itertools import combinations
from pathlib import Path
from uuid import UUID

LAB = Path(__file__).resolve().parent
RAW = LAB / "analysis" / "raw"
DATA = LAB / "data"
SEED_USER = UUID("00000000-0000-4000-8000-000000000001")
MODELS = ["solar", "ax", "exaone"]
NO_HITS = "관련 위키 내용을 찾지 못했습니다"


def _chat(slug: str):
    import pool

    if slug == "codex":
        from codex_judge import CodexJudge

        return CodexJudge()
    return pool.build_pool([slug])[slug]


def _load(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def _app(p: Path, r):
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")


_GENQ = (
    "너는 사내 지식베이스 평가 문항 작성자다. 아래 문서로 답할 수 있는 실제 직원이 물을 "
    "법한 한국어 질문 1개를 작성하라. 제목 단어를 그대로 베끼지 말고 15~30자로. 문서에 "
    "실제 답이 있는 질문만. "
    'JSON만 출력: {"q": "..."}'
)


def cmd_gen_q(a):
    from sqlalchemy import text

    from orthus.db import session

    with session() as s:
        rows = [dict(r) for r in s.execute(text("SELECT doc_id, title, markdown FROM documents")).mappings().all()]
    gens = {m: _chat(m) for m in MODELS}
    out = DATA / "mw_golden.jsonl"
    done = {r["doc_id"] for r in _load(out)}
    n = 0
    for i, d in enumerate(rows):
        if str(d["doc_id"]) in done:
            continue
        gm = MODELS[i % len(MODELS)]
        try:
            q = json.loads(gens[gm].complete(_GENQ, f"제목: {d['title']}\n\n내용:\n{d['markdown'][:3500]}", json_only=True)).get("q", "").strip()
        except Exception:
            continue
        if q:
            _app(out, {"doc_id": str(d["doc_id"]), "q": q, "title": d["title"], "generator": gm})
            n += 1
    print(f"gen-q: +{n} (total {len(_load(out))})")


def _clean_wiki():
    from sqlalchemy import text

    from orthus.db import session

    with session() as s:
        for t in ("wiki_links", "wiki_chunks", "wiki_pages", "wiki_sources", "wiki_tasks", "embeddings"):
            try:
                s.execute(text(f"TRUNCATE {t} CASCADE"))
            except Exception:
                pass
        s.commit()


def cmd_build(a):
    from orthus.wiki.author import rebuild_all_parallel

    chat = _chat(a.distill)
    _clean_wiki()
    t0 = time.monotonic()
    totals = rebuild_all_parallel(chat_model=chat, concurrency=a.concurrency)
    totals["elapsed_s"] = round(time.monotonic() - t0, 1)
    totals["distill"] = a.distill
    _app(RAW / "mw_granularity.jsonl", totals)
    print(f"build {a.distill}: {totals}")


def cmd_answer(a):
    from orthus.wiki.qa import answer_from_hits
    from orthus.wiki.retrieve import retrieve

    ans_model = _chat("solar")  # 답변 모델 고정 — 위키 저작만 변수
    out = RAW / f"mw_ans_{a.distill}.jsonl"
    done = {r["doc_id"] for r in _load(out)}
    n = 0
    for g in _load(DATA / "mw_golden.jsonl"):
        if g["doc_id"] in done:
            continue
        hits = retrieve(SEED_USER, g["q"], k=5, scope="company")
        try:
            wa = answer_from_hits(SEED_USER, g["q"], hits, chat_model=ans_model, learn=False, record_gaps=False)
            ans = wa.answer
        except Exception as exc:
            ans = f"__ERR__ {type(exc).__name__}"
        _app(out, {"doc_id": g["doc_id"], "q": g["q"], "answer": ans,
                   "n_hits": len(hits), "refused": NO_HITS in ans})
        n += 1
    print(f"answer {a.distill}: +{n}")


_JUDGE = (
    "너는 사내 위키 어시스턴트 답변 품질 심사자다. 질문과 익명의 두 답변 A/B를 받는다. "
    "더 구체적이고 정확하며 질문에 실제로 답한 쪽이 낫다. 근거를 못 찾았다는 답은 최하다. "
    'JSON만 출력: {"winner":"A"|"B"|"tie"}'
)


def cmd_rrjudge(a):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    ans = {m: {r["doc_id"]: r for r in _load(RAW / f"mw_ans_{m}.jsonl")} for m in MODELS}
    out = RAW / f"mw_rrjudge_{a.judge}.jsonl"
    done = {(r["pair"], r["doc_id"]) for r in _load(out)}
    lock = threading.Lock()
    tls = threading.local()

    def judge():
        if not hasattr(tls, "c"):
            tls.c = _chat(a.judge)
        return tls.c

    work = []
    for mA, mB in combinations(MODELS, 2):
        pair = f"{mA}|{mB}"
        for did in sorted(set(ans[mA]) & set(ans[mB])):
            if (pair, did) not in done:
                work.append((mA, mB, pair, did))

    def do(it):
        mA, mB, pair, did = it
        a1, a2 = ans[mA][did], ans[mB][did]
        votes = []
        for first, second, la, lb in ((a1, a2, mA, mB), (a2, a1, mB, mA)):
            u = f"질문: {a1['q']}\n\n답변 A:\n{first['answer']}\n\n답변 B:\n{second['answer']}"
            try:
                w = json.loads(judge().complete(_JUDGE, u, json_only=True)).get("winner", "tie")
            except Exception:
                w = "tie"
            votes.append({"A": la, "B": lb, "tie": "tie"}.get(w, "tie"))
        with lock:
            _app(out, {"pair": pair, "doc_id": did, "winner": votes[0] if votes[0] == votes[1] else "tie"})

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(do, work))
    print(f"rrjudge ({a.judge}): {len(work)} done")


_JUDGE2 = (
    "너는 사내 위키 답변 채점자다. 질문, **원본 문서(정답 근거)**, 익명의 두 답변 A/B를 "
    "받는다. 원본 문서에 실제로 적힌 내용을 정확·충실하게 답한 쪽이 낫다. 채점 규칙: "
    "(1) 원본에 없는 내용을 단정하면 감점(환각) (2) 원본에 답이 있는데 못 답하거나 딴 얘기를 "
    "하면 감점 (3) 원본에 근거해 더 정확하고 완전한 쪽이 승. 매끄러움·자신감은 근거가 없으면 "
    "가점 사유가 아니다. "
    'JSON만 출력: {"winner":"A"|"B"|"tie"}'
)


def cmd_rrjudge2(a):
    """원본 문서를 정답 기준으로 놓는 재판정 — 근거 없는 자신감/저작 실패를 잡는다."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text

    from orthus.db import session

    with session() as s:
        src = {str(r["doc_id"]): r["markdown"] for r in
               s.execute(text("SELECT doc_id, markdown FROM documents")).mappings().all()}
    ans = {m: {r["doc_id"]: r for r in _load(RAW / f"mw_ans_{m}.jsonl")} for m in MODELS}
    out = RAW / f"mw_rrjudge2_{a.judge}.jsonl"
    done = {(r["pair"], r["doc_id"]) for r in _load(out)}
    lock = threading.Lock()
    tls = threading.local()

    def judge():
        if not hasattr(tls, "c"):
            tls.c = _chat(a.judge)
        return tls.c

    work = [(mA, mB, f"{mA}|{mB}", d) for mA, mB in combinations(MODELS, 2)
            for d in sorted(set(ans[mA]) & set(ans[mB]) & set(src))
            if (f"{mA}|{mB}", d) not in done]

    def do(it):
        mA, mB, pair, did = it
        a1, a2 = ans[mA][did], ans[mB][did]
        votes = []
        for first, second, la, lb in ((a1, a2, mA, mB), (a2, a1, mB, mA)):
            u = (f"질문: {a1['q']}\n\n원본 문서(정답 근거):\n{src[did][:3000]}\n\n"
                 f"답변 A:\n{first['answer']}\n\n답변 B:\n{second['answer']}")
            try:
                w = json.loads(judge().complete(_JUDGE2, u, json_only=True)).get("winner", "tie")
            except Exception:
                w = "tie"
            votes.append({"A": la, "B": lb, "tie": "tie"}.get(w, "tie"))
        with lock:
            _app(out, {"pair": pair, "doc_id": did, "winner": votes[0] if votes[0] == votes[1] else "tie"})

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(do, work))
    print(f"rrjudge2 ({a.judge}): {len(work)} done -> {out}")


def cmd_report(a):
    import significance as sig
    from collections import defaultdict

    lines = ["# 미니위키 — 저작 모델 × downstream 답변\n"]
    gran = {r["distill"]: r for r in _load(RAW / "mw_granularity.jsonl")}
    lines.append("## granularity + 거부율 (결정론)")
    for m in MODELS:
        g = gran.get(m, {})
        ans = _load(RAW / f"mw_ans_{m}.jsonl")
        refused = sum(1 for r in ans if r.get("refused"))
        errs = sum(1 for r in ans if "__ERR__" in r.get("answer", ""))
        cpd = g.get("claims", 0) / max(g.get("documents", 1), 1)
        lines.append(f"  {m}: pages={g.get('pages','?')} claims={g.get('claims','?')} "
                     f"claims/doc={cpd:.1f} 저작={g.get('elapsed_s','?')}s | 답변거부 {refused}/{len(ans)} 오류 {errs}")

    lines.append("\n## 거부율 paired (검색 실패 proxy, 낮을수록 좋음)")
    refmap = {m: {r["doc_id"]: bool(r.get("refused")) for r in _load(RAW / f"mw_ans_{m}.jsonl")} for m in MODELS}
    holm = []
    for mA, mB in combinations(MODELS, 2):
        common = set(refmap[mA]) & set(refmap[mB])
        ab = sum(1 for i in common if refmap[mA][i] and not refmap[mB][i])
        ba = sum(1 for i in common if refmap[mB][i] and not refmap[mA][i])
        p = sig.exact_mcnemar(ab, ba)
        lines.append(f"    {mA} vs {mB}: {ab}:{ba} p={p:.4f} → {mB if ab>ba else (mA if ba>ab else '=')}")
        # 거부율은 퇴화 검정(0:0)이 섞이므로 Holm 가족에서 분리 — K 부풀림 방지

    lines.append("\n## 답변 품질 라운드로빈 (codex, 높을수록 좋음)")
    rr = _load(RAW / f"mw_rrjudge_{a.judge}.jsonl")
    wins = defaultdict(int); losses = defaultdict(int); pt = defaultdict(lambda: [0, 0, 0])
    for r in rr:
        mA, mB = r["pair"].split("|"); t = pt[r["pair"]]
        if r["winner"] == mA: wins[mA] += 1; losses[mB] += 1; t[0] += 1
        elif r["winner"] == mB: wins[mB] += 1; losses[mA] += 1; t[1] += 1
        else: t[2] += 1
    for m in MODELS:
        lines.append(f"  {m}: 승 {wins[m]} 패 {losses[m]} (Copeland {wins[m]-losses[m]:+d})")
    for pair, t in pt.items():
        mA, mB = pair.split("|"); p = sig.exact_mcnemar(t[0], t[1])
        lines.append(f"    {mA} vs {mB}: {t[0]}:{t[1]} tie={t[2]} p={p:.4f} → {mA if t[0]>t[1] else (mB if t[1]>t[0] else '=')}")
        holm.append((f"품질 {pair}", p))

    lines.append("\n## Holm 보정 (α=0.05, 품질 비교만 — 거부율 퇴화검정 제외)")
    hs = sorted(holm, key=lambda x: x[1]); K = len(hs); any_sig = False
    for i, (name, p) in enumerate(hs):
        th = 0.05 / (K - i)
        if p < th:
            any_sig = True; lines.append(f"  {name}: p={p:.4f} < {th:.4f} ★유의")
    if not any_sig:
        lines.append(f"  보정 후 유의 없음 (총 {K} 비교, 최소 p={hs[0][1]:.4f}) → 저작 모델은 downstream을 유의하게 바꾸지 않음.")
    txt = "\n".join(lines)
    (LAB / "analysis" / "miniwiki-results.md").write_text(txt, encoding="utf-8")
    print(txt)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-q").set_defaults(fn=cmd_gen_q)
    b = sub.add_parser("build"); b.add_argument("--distill", required=True); b.add_argument("--concurrency", type=int, default=4); b.set_defaults(fn=cmd_build)
    an = sub.add_parser("answer"); an.add_argument("--distill", required=True); an.set_defaults(fn=cmd_answer)
    j = sub.add_parser("rrjudge"); j.add_argument("--judge", default="codex"); j.add_argument("--workers", type=int, default=5); j.set_defaults(fn=cmd_rrjudge)
    j2 = sub.add_parser("rrjudge2"); j2.add_argument("--judge", default="codex"); j2.add_argument("--workers", type=int, default=6); j2.set_defaults(fn=cmd_rrjudge2)
    r = sub.add_parser("report"); r.add_argument("--judge", default="codex"); r.set_defaults(fn=cmd_report)
    a = ap.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
