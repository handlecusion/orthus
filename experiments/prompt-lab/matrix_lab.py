"""모델×태스크 매트릭스 — 국내 3모델(solar·ax·exaone)을 프롬프트 고정(현행)하고
7표면에서 재는 하네스. "특정 모델이 잘하는 태스크가 있나"를 홀드아웃+유의성으로 답한다.

⚠️ model-orchestration.md 교훈: 낙관편향(규칙 캔 골든에서 채점)·다중비교(태스크×모델쌍
많으면 우연 승자) 함정. 신규 홀드아웃 + paired McNemar + Holm 보정으로 방어한다.

채점 = 하이브리드:
  결정론  — rewrite(지시어잔존)·synthesize(마커/action)·decompose(게이트정확도)·
            delegation(오탐)·신뢰성(JSON실패·지연)
  판정자  — distill(오염/커버리지, 원문 동봉)·wiki_qa(라운드로빈 쌍대, 근거 동봉)

서브커맨드:
  qa-gen              신규 페이지에서 한국어 질문 + frozen hits (사용 페이지 제외)
  qa-answer --model   frozen hits 위에서 답변 → mtx_qa_{model}.jsonl
  qa-rrjudge          3모델 라운드로빈 쌍대 판정 (익명 스왑, 근거 동봉)
  rw-gen / rw --model      후속 재작성 (신규 qa 답변 파생)
  syn-gen / syn --model    합성 (신규 qa 질문 페어)
  dec --model              분해 게이트 (t7+t7_holdout 라벨)
  deleg --model            위임 추출 (t10+t10_holdout2 함정)
  report                   전 표면 매트릭스 + 유의성표
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from itertools import combinations
from pathlib import Path
from uuid import UUID

LAB = Path(__file__).resolve().parent
RAW = LAB / "analysis" / "raw"
DATA = LAB / "data"
FUGU_GOLD = LAB.parent / "fugu-ko" / "golden"
SEED_USER = UUID("00000000-0000-4000-8000-000000000001")
MODELS = ["solar", "ax", "exaone"]
CIT = re.compile(r"문서\s*\[|(?<![\w가-힣])\[\d+\]")
ACT = re.compile(r"메일(을|로)?\s*(보내|발송|전송)|보내\s*드리|발송하겠|전송하겠|메일로 정리")
REF = ("그거", "그것", "그건", "해당", "이거", "이것", "저거", "아까", "방금", "위에서", "앞서", "거기", "그때")


def _chat(slug: str):
    import pool

    if slug == "codex":
        from codex_judge import CodexJudge

        return CodexJudge()
    if slug.startswith("gpt"):
        key = os.environ.get("JUDGE_OPENAI_KEY", "").strip()
        return pool.WorkerChat(pool.WorkerSpec("j", "", "https://api.openai.com/v1", slug, timeout=90), key, slug)
    return pool.build_pool([slug])[slug]


def _load(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def _app(p: Path, r: dict) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── wiki_qa (fresh holdout) ──────────────────────────────────────────────────

_GENQ = (
    "너는 사내 지식베이스 평가 문항 작성자다. 위키 페이지 하나로 답할 수 있는 실제 "
    "직원이 물을 법한 한국어 질문 1개를 작성하라. 제목 단어를 그대로 베끼지 말고 "
    "15~30자로. 페이지에 실제 답이 있는 질문만. "
    'JSON만 출력: {"q": "..."}'
)


def _page_rows(limit: int, exclude: set[str]) -> list[dict]:
    from sqlalchemy import text

    from orthus.db import session

    sql = text(
        "SELECT p.slug, p.title, p.project, string_agg(c.content, E'\n' ORDER BY c.ordinal) AS content "
        "FROM wiki_pages p JOIN wiki_chunks c ON c.page_id=p.page_id WHERE p.kind='page' "
        "GROUP BY p.page_id, p.slug, p.title, p.project "
        "HAVING sum(length(c.content))>=200 ORDER BY md5(p.slug||'mtx') LIMIT :lim"
    )
    with session() as s:
        rows = [dict(r) for r in s.execute(sql, {"lim": limit * 3}).mappings().all()]
    return [r for r in rows if r["slug"] not in exclude][:limit]


def cmd_qa_gen(a):
    from orthus.wiki.retrieve import retrieve

    exclude = set(Path("/tmp/used_qa_slugs.txt").read_text().split()) if Path("/tmp/used_qa_slugs.txt").exists() else set()
    pages = _page_rows(a.n, exclude)
    # 생성기 편향 방지: 3모델을 문항마다 로테이션 (fugu-ko 다생성기 관례, 벤더 독립).
    gens = {m: _chat(m) for m in MODELS}
    gpath, hpath = DATA / "mtx_qa_golden.jsonl", DATA / "mtx_qa_hits.jsonl"
    gdone = {r["page_slug"] for r in _load(gpath)}
    n = 0
    for i, pg in enumerate(pages):
        if pg["slug"] in gdone:
            continue
        gm = MODELS[i % len(MODELS)]
        try:
            q = json.loads(gens[gm].complete(_GENQ, f"제목: {pg['title']}\n\n내용:\n{pg['content'][:4000]}", json_only=True)).get("q", "").strip()
        except Exception:
            continue
        if not q:
            continue
        _app(gpath, {"id": pg["slug"][:50], "q": q, "page_slug": pg["slug"], "title": pg["title"], "generator": gm})
        refs = retrieve(SEED_USER, q, k=5, scope="company")
        _app(hpath, {"id": pg["slug"][:50], "q": q, "hits": [json.loads(r.model_dump_json()) for r in refs]})
        n += 1
    print(f"qa-gen: +{n} (total {len(_load(gpath))})")


def cmd_qa_answer(a):
    from orthus.schemas.canonical import WikiSourceRef
    from orthus.wiki import qa as qa_mod

    chat = _chat(a.model)
    out = RAW / f"mtx_qa_{a.model}.jsonl"
    done = {r["id"] for r in _load(out)}
    n = 0
    for row in _load(DATA / "mtx_qa_hits.jsonl"):
        if row["id"] in done:
            continue
        refs = [WikiSourceRef.model_validate(h) for h in row["hits"]]
        t0 = time.monotonic()
        try:
            ans = qa_mod.answer_from_hits(SEED_USER, row["q"], refs, chat_model=chat, learn=False, record_gaps=False)
            rec = {"id": row["id"], "q": row["q"], "answer": ans.answer, "ms": int((time.monotonic() - t0) * 1000)}
        except Exception as exc:
            rec = {"id": row["id"], "q": row["q"], "error": f"{type(exc).__name__}", "ms": int((time.monotonic() - t0) * 1000)}
        _app(out, rec)
        n += 1
    print(f"qa-answer {a.model}: +{n}")


_QAJUDGE = (
    "너는 사내 위키 어시스턴트 답변 품질 심사자다. 질문, 위키 근거, 익명의 두 답변 A/B를 "
    "받는다. 근거에 실제로 있는 내용만 좋은 답으로 인정하라 — 근거 없는 단정 감점, 근거에 "
    "있는데 회피 감점. 유용성(구체성·정확성·간결성)을 비교하라. "
    'JSON만 출력: {"winner": "A"|"B"|"tie"}'
)


def cmd_qa_rrjudge(a):
    # 판정자 = 비교쌍에 없는 제3의 국내 모델(judge ∉ pair, domestic) 또는 중립 고정 판정자
    # (codex/gpt-4o). codex는 콜당 ~8s라 스레드풀로 동시 판정(독립 subprocess, append는 lock).
    import threading
    from concurrent.futures import ThreadPoolExecutor

    hits = {r["id"]: r for r in _load(DATA / "mtx_qa_hits.jsonl")}
    ans = {m: {r["id"]: r for r in _load(RAW / f"mtx_qa_{m}.jsonl") if "answer" in r} for m in MODELS}
    domestic = a.judge == "domestic"
    tag = a.judge
    out = RAW / f"mtx_qa_rrjudge_{tag}.jsonl"
    done = {(r["pair"], r["id"]) for r in _load(out)}
    lock = threading.Lock()
    # 판정자 인스턴스는 스레드마다 별도(codex=subprocess라 무상태, 국내는 pool 재사용 가능)
    tls = threading.local()

    def judge_for(mA, mB):
        jid = a.judge if not domestic else next(m for m in MODELS if m not in (mA, mB))
        if not hasattr(tls, "cache"):
            tls.cache = {}
        if jid not in tls.cache:
            tls.cache[jid] = _chat(jid)
        return tls.cache[jid], jid

    work = []
    for mA, mB in combinations(MODELS, 2):
        pair = f"{mA}|{mB}"
        for qid in sorted(set(ans[mA]) & set(ans[mB]) & set(hits)):
            if (pair, qid) not in done:
                work.append((mA, mB, pair, qid))

    def do(item):
        mA, mB, pair, qid = item
        judge, jid = judge_for(mA, mB)
        ctx = "\n".join(f"[{i+1}] ({h.get('page_slug','?')}) {h.get('excerpt','')[:450]}" for i, h in enumerate(hits[qid]["hits"]))
        votes = []
        for first, second, la, lb in ((ans[mA][qid], ans[mB][qid], mA, mB), (ans[mB][qid], ans[mA][qid], mB, mA)):
            u = f"질문: {hits[qid]['q']}\n\n위키 근거:\n{ctx}\n\n답변 A:\n{first['answer']}\n\n답변 B:\n{second['answer']}"
            try:
                w = json.loads(judge.complete(_QAJUDGE, u, json_only=True)).get("winner", "tie")
            except Exception:
                w = "tie"
            votes.append({"A": la, "B": lb, "tie": "tie"}.get(w, "tie"))
        rec = {"pair": pair, "id": qid, "judge": jid, "winner": votes[0] if votes[0] == votes[1] else "tie"}
        with lock:
            _app(out, rec)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, _ in enumerate(ex.map(do, work), 1):
            if i % 30 == 0:
                print(f"  rrjudge {i}/{len(work)}")
    print(f"qa-rrjudge ({tag}) done -> {out}")


# ── rewrite / synthesize (fresh, deterministic) ──────────────────────────────

_FUP = ["그거 담당자가 누구야?", "방금 그 내용 더 자세히 알려줘", "아까 그거 일정이 어떻게 돼?",
        "그건 왜 그런 거야?", "그거 다른 프로젝트에도 적용돼?", "위에서 말한 거 요약해줘",
        "그때 결정된 내용이 뭐였지?", "그거 지금 상태가 어때?"]


def cmd_rw_gen(a):
    base = {r["id"]: r for r in _load(RAW / "mtx_qa_solar.jsonl") if r.get("answer") and len(r["answer"]) > 60}
    out = DATA / "mtx_rw_golden.jsonl"
    done = {r["id"] for r in _load(out)}
    n = 0
    for i, (qid, r) in enumerate(base.items()):
        if qid in done:
            continue
        _app(out, {"id": qid, "history": [{"role": "user", "text": r["q"]}, {"role": "assistant", "text": r["answer"][:600]}], "followup": _FUP[i % len(_FUP)]})
        n += 1
    print(f"rw-gen: +{n}")


def cmd_syn_gen(a):
    base = [r for r in _load(RAW / "mtx_qa_solar.jsonl") if r.get("answer") and len(r["answer"]) > 60]
    out = DATA / "mtx_syn_golden.jsonl"
    done = {r["id"] for r in _load(out)}
    n = 0
    for i in range(0, len(base) - 1, 2):
        rid = f"msyn-{i//2:02d}"
        if rid in done:
            continue
        g1, g2 = base[i], base[i + 1]
        act = (i // 2) % 3 == 2
        q = f"{g1['q']} 그리고 {g2['q']}" + (" 정리해서 김서연한테 메일로 보내줘" if act else "")
        _app(out, {"id": rid, "q": q, "with_action": act, "subs": [{"q": g1["q"], "a": g1["answer"][:800]}, {"q": g2["q"], "a": g2["answer"][:800]}]})
        n += 1
    print(f"syn-gen: +{n}")


def cmd_rw(a):
    from orthus.router import rewrite as rw
    from orthus.schemas.canonical import ChatTurn

    chat = _chat(a.model)
    out = RAW / f"mtx_rw_{a.model}.jsonl"
    done = {r["id"] for r in _load(out)}
    for g in _load(DATA / "mtx_rw_golden.jsonl"):
        if g["id"] in done:
            continue
        hist = [ChatTurn(role=t["role"], text=t["text"]) for t in g["history"]]
        got = rw.resolve_followup(g["followup"], hist, chat_model=chat)
        _app(out, {"id": g["id"], "followup": g["followup"], "rewritten": got})
    print(f"rw {a.model} done")


def cmd_syn(a):
    from orthus.router import decompose as dec

    chat = _chat(a.model)
    out = RAW / f"mtx_syn_{a.model}.jsonl"
    done = {r["id"] for r in _load(out)}
    for g in _load(DATA / "mtx_syn_golden.jsonl"):
        if g["id"] in done:
            continue
        parts = [f"[{i+1}] Sub-question: {s['q']}\nAnswer: {s['a']}" for i, s in enumerate(g["subs"])]
        user = f"Original compound question: {g['q']}\n\nSub-answers:\n" + "\n\n".join(parts) + "\n\nSynthesized answer:"
        try:
            ans = chat.complete(dec._SYNTHESIZE_SYSTEM, user)
            _app(out, {"id": g["id"], "answer": ans, "with_action": g["with_action"]})
        except Exception as exc:
            _app(out, {"id": g["id"], "error": str(exc), "with_action": g["with_action"]})
    print(f"syn {a.model} done")


# ── decompose / delegation (fixed labeled golden) ────────────────────────────


def cmd_dec(a):
    from orthus.router.decompose import should_decompose

    chat = _chat(a.model)
    out = RAW / f"mtx_dec_{a.model}.jsonl"
    done = {r["id"] for r in _load(out)}
    for gf in ("t7", "t7_holdout"):
        for it in json.load(open(FUGU_GOLD / f"{gf}.json", encoding="utf-8"))["items"]:
            if it["id"] in done:
                continue
            try:
                got = should_decompose(it["q"], chat_model=chat, ext_tier=a.ext_tier)
            except Exception:
                got = None
            _app(out, {"id": it["id"], "expected": bool(it.get("compound")), "got": got})
    print(f"dec {a.model} done")


def cmd_deleg(a):
    from orthus.agentwork.delegation import extract_delegation_intent
    from orthus.settings import get_settings

    st = get_settings()
    chat = _chat(a.model)
    out = RAW / f"mtx_deleg_{a.model}.jsonl"
    done = {r["id"] for r in _load(out)}
    for gf in ("t10_delegation", "t10_holdout2"):
        for it in json.load(open(FUGU_GOLD / f"{gf}.json", encoding="utf-8"))["items"]:
            if it["id"] in done:
                continue
            try:
                res = extract_delegation_intent(it["text"], st, chat_model=chat)
                got = res is not None
            except Exception:
                got = False
            _app(out, {"id": it["id"], "expected": bool(it.get("is_delegation")), "got": got})
    print(f"deleg {a.model} done")


# ── report ───────────────────────────────────────────────────────────────────


def cmd_report(a):
    import significance as sig

    def det_rows(task, fn):
        # fn(row)->bool "is this a defect/miss" (lower=better)
        return {m: {r["id"]: fn(r) for r in _load(RAW / f"mtx_{task}_{m}.jsonl")} for m in MODELS}

    lines = ["# 모델×태스크 매트릭스 결과\n"]
    holm = []  # (task, pair, metric, p, better)

    def paired(task, label, defect_by_model, lower_better=True):
        lines.append(f"\n## {task} — {label} (낮을수록 좋음={lower_better})")
        for m in MODELS:
            d = defect_by_model[m]
            lines.append(f"  {m}: {sum(d.values())}/{len(d)} ({sum(d.values())/max(len(d),1):.1%})")
        for mA, mB in combinations(MODELS, 2):
            dA, dB = defect_by_model[mA], defect_by_model[mB]
            common = set(dA) & set(dB)
            ab = sum(1 for i in common if dA[i] and not dB[i])  # A결함 B정상
            ba = sum(1 for i in common if dB[i] and not dA[i])
            p = sig.exact_mcnemar(ab, ba)
            better = mB if ab > ba else (mA if ba > ab else "=")
            lines.append(f"    {mA} vs {mB}: {ab}:{ba} p={p:.4f} → {better} 우세")
            holm.append((task, f"{mA}v{mB}", label, p, better))

    # rewrite: 지시어 잔존
    paired("rewrite", "지시어잔존", det_rows("rw", lambda r: any(t in r["rewritten"].replace(" ", "") for t in REF)))
    # synthesize: 인용마커 / action누설
    syn = {m: _load(RAW / f"mtx_syn_{m}.jsonl") for m in MODELS}
    paired("synthesize", "인용마커", {m: {r["id"]: bool(CIT.search(r.get("answer", ""))) for r in syn[m] if "answer" in r} for m in MODELS})
    paired("synthesize", "action누설", {m: {r["id"]: bool(r.get("with_action") and ACT.search(r.get("answer", ""))) for r in syn[m] if "answer" in r} for m in MODELS})
    # decompose: 게이트 오답
    paired("decompose", "게이트오답", det_rows("dec", lambda r: r["got"] != r["expected"]))
    # delegation: 오탐(함정에 fire) + 전체 오답
    paired("delegation", "오탐(함정fire)", {m: {r["id"]: (r["got"] and not r["expected"]) for r in _load(RAW / f"mtx_deleg_{m}.jsonl")} for m in MODELS})
    # distill: 오염(문서단위) — score 파일에서
    def distill_defect(metric):
        import sys as _s
        _s.path.insert(0, str(LAB))
        from score_distill import _doc_metrics, _load_jsonl
        out = {}
        for m in MODELS:
            f = RAW / f"score_distill_baseline_{m}_mtx_{a.judge}.jsonl"
            rows = _load_jsonl(f) if f.exists() else []
            d = {}
            for r in rows:
                mm = _doc_metrics(r)
                if mm and not mm["error"]:
                    d[r["doc_id"]] = (mm["fab"] > 0) if metric == "contam" else (mm["meta"] > 0)
            out[m] = d
        return out
    paired("distill", "오염(문서단위)", distill_defect("contam"))

    # wiki_qa: 라운드로빈 승패 (Copeland)
    lines.append("\n## wiki_qa — 라운드로빈 쌍대 (판정자, 높을수록 좋음=승)")
    rr = _load(RAW / f"mtx_qa_rrjudge_{a.judge}.jsonl")
    from collections import defaultdict
    wins = defaultdict(int); losses = defaultdict(int)
    pair_tally = defaultdict(lambda: [0, 0, 0])  # w_left,w_right,tie
    for r in rr:
        mA, mB = r["pair"].split("|")
        t = pair_tally[r["pair"]]
        if r["winner"] == mA:
            wins[mA] += 1; losses[mB] += 1; t[0] += 1
        elif r["winner"] == mB:
            wins[mB] += 1; losses[mA] += 1; t[1] += 1
        else:
            t[2] += 1
    for m in MODELS:
        lines.append(f"  {m}: 승 {wins[m]} 패 {losses[m]} (Copeland {wins[m]-losses[m]:+d})")
    for pair, t in pair_tally.items():
        mA, mB = pair.split("|")
        p = sig.exact_mcnemar(t[0], t[1])
        better = mA if t[0] > t[1] else (mB if t[1] > t[0] else "=")
        lines.append(f"    {mA} vs {mB}: {t[0]}:{t[1]} tie={t[2]} p={p:.4f} → {better}")
        holm.append(("wiki_qa", pair, "쌍대", p, better))

    # 신뢰성: JSON실패(=answer/error), 지연 (qa ms)
    lines.append("\n## 신뢰성 (결정론)")
    for m in MODELS:
        qa = _load(RAW / f"mtx_qa_{m}.jsonl")
        errs = sum(1 for r in qa if "error" in r)
        ms = sorted(r["ms"] for r in qa if "ms" in r)
        p50 = ms[len(ms)//2] if ms else 0
        p95 = ms[int(len(ms)*0.95)] if ms else 0
        lines.append(f"  {m}: qa오류 {errs}/{len(qa)} · 지연 p50={p50}ms p95={p95}ms")

    # Holm 다중비교 보정
    lines.append("\n## 다중비교 보정 (Holm, α=0.05)")
    holm_sorted = sorted(holm, key=lambda x: x[3])
    K = len(holm_sorted)
    any_sig = False
    for i, (task, pair, metric, p, better) in enumerate(holm_sorted):
        thresh = 0.05 / (K - i)
        sig_mark = "★유의" if p < thresh else ""
        if p < thresh:
            any_sig = True
            lines.append(f"  {task}/{metric} {pair}: p={p:.4f} < {thresh:.4f} {sig_mark} → {better}")
    if not any_sig:
        lines.append(f"  보정 후 유의한 쌍 없음 (총 {K}개 비교, 최소 p={holm_sorted[0][3]:.4f}). "
                     "→ model-orchestration.md 결론(모델간 태스크별 유의차 없음) 재확인.")
    txt = "\n".join(lines)
    (LAB / "analysis" / "matrix-results.md").write_text(txt, encoding="utf-8")
    print(txt)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("qa-gen"); g.add_argument("--n", type=int, default=60); g.set_defaults(fn=cmd_qa_gen)
    for name, fn in [("qa-answer", cmd_qa_answer), ("rw", cmd_rw), ("syn", cmd_syn), ("dec", cmd_dec), ("deleg", cmd_deleg)]:
        p = sub.add_parser(name); p.add_argument("--model", required=True)
        if name == "dec":
            p.add_argument("--ext-tier", type=int, default=None)
        p.set_defaults(fn=fn)
    jp = sub.add_parser("qa-rrjudge"); jp.add_argument("--judge", default="codex"); jp.add_argument("--workers", type=int, default=4); jp.set_defaults(fn=cmd_qa_rrjudge)
    rp = sub.add_parser("report"); rp.add_argument("--judge", default="codex"); rp.set_defaults(fn=cmd_report)
    for name, fn in [("rw-gen", cmd_rw_gen), ("syn-gen", cmd_syn_gen)]:
        sub.add_parser(name).set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
