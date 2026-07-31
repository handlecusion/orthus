"""Track A — 질의측(wiki_qa) 프롬프트 A/B 파이프라인.

서브커맨드:
  gen     재구축 위키에서 골든 질문 역방향 생성 (제목 누출 검사 포함)
  hits    질문별 production retrieve() 1회 실행 → frozen hits jsonl
  answer  frozen hits 위에서 answer_from_hits를 프롬프트 변형으로 실행
  judge   두 변형 답변 쌍대 판정 (익명 A/B + 양방향 스왑 + 불일치=tie, hits 동봉)

환경: PYTHONPATH=$PWD:$PWD/experiments/fugu-ko:$PWD/experiments/prompt-lab
  FUGU_KEYS=experiments/fugu-ko/keys.json (solar)
  JUDGE_OPENAI_KEY=... (gen의 gpt 생성기 + judge)
  hits/answer는 로컬 재구축 DB + ORTHUS_EMBEDDING=solar 환경 필요.

원칙: hits는 1회 캡처 후 고정 — 모든 변형이 같은 hits 위에서 경쟁 (검색 변동과
프롬프트 효과 분리). answer는 learn=False, record_gaps=False (불변식 5 보존).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from uuid import UUID

LAB_DIR = Path(__file__).resolve().parent
RAW_DIR = LAB_DIR / "analysis" / "raw"
DATA_DIR = LAB_DIR / "data"
SEED_USER = UUID("00000000-0000-4000-8000-000000000001")

_GEN_SYSTEM = (
    "너는 사내 지식베이스 평가 문항 작성자다. 위키 페이지 하나를 받아, 그 페이지 "
    "내용으로 답할 수 있는 **실제 직원이 물을 법한 한국어 질문**을 {n}개 작성하라. "
    "규칙: (1) 페이지 제목에 있는 단어를 질문에 그대로 쓰지 마라(동의어/설명으로 "
    "바꿔라) (2) 질문은 짧고 자연스럽게 — 15~30자 내외, 절반은 의문형·절반은 "
    "키워드형 (3) 페이지 내용에 실제로 답이 있는 질문만. "
    'JSON만 출력: {{"questions": ["...", ...]}}'
)


def _pool_chat(slug: str):
    import pool

    if slug.startswith("gpt"):
        key = os.environ.get("JUDGE_OPENAI_KEY", "").strip()
        spec = pool.WorkerSpec("gptgen", provider_match="", base_url="https://api.openai.com/v1", model=slug, timeout=90)
        return pool.WorkerChat(spec, key, slug)
    return pool.build_pool([slug])[slug]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _page_rows(min_len: int, max_len: int, limit: int) -> list[dict]:
    from sqlalchemy import text

    from orthus.db import session

    sql = text(
        """
        SELECT p.slug, p.title, p.project, string_agg(c.content, E'\n' ORDER BY c.ordinal) AS content
        FROM wiki_pages p JOIN wiki_chunks c ON c.page_id = p.page_id
        WHERE p.kind = 'page'
        GROUP BY p.page_id, p.slug, p.title, p.project
        HAVING sum(length(c.content)) >= :lo AND sum(length(c.content)) < :hi
        ORDER BY md5(p.slug)
        LIMIT :lim
        """
    )
    with session() as s:
        rows = s.execute(sql, {"lo": min_len, "hi": max_len, "lim": limit}).mappings().all()
    return [dict(r) for r in rows]


def _title_leak(q: str, title: str) -> bool:
    words = [w for w in re.split(r"[\s/·,()\[\]]+", title) if len(w) >= 2]
    qn = q.lower()
    return any(w.lower() in qn for w in words)


def cmd_gen(args: argparse.Namespace) -> None:
    pages = _page_rows(300, 10**9, 50) + _page_rows(150, 300, 20)
    out_path = DATA_DIR / "qa_golden.jsonl"
    done = {r["page_slug"] for r in _load_jsonl(out_path)}
    gens = {g: _pool_chat(g) for g in args.generators.split(",")}
    n = 0
    with out_path.open("a", encoding="utf-8") as out:
        for pg in pages:
            if pg["slug"] in done:
                continue
            for gslug, chat in gens.items():
                try:
                    raw = chat.complete(
                        _GEN_SYSTEM.replace("{n}", "1").replace("{{", "{").replace("}}", "}"),
                        f"페이지 제목: {pg['title']}\n\n페이지 내용:\n{pg['content'][:4000]}",
                        json_only=True,
                    )
                    qs = json.loads(raw).get("questions", [])[:1]
                except Exception as exc:
                    print(f"  gen fail {pg['slug']} {gslug}: {exc}")
                    continue
                for q in qs:
                    q = str(q).strip()
                    if not q:
                        continue
                    rec = {
                        "id": f"{pg['slug'][:40]}::{gslug}",
                        "q": q,
                        "page_slug": pg["slug"],
                        "title": pg["title"],
                        "project": pg["project"],
                        "generator": gslug,
                        "title_leak": _title_leak(q, pg["title"]),
                    }
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n += 1
    rows = _load_jsonl(out_path)
    leaks = sum(1 for r in rows if r["title_leak"])
    print(f"generated {n} (total {len(rows)}, title-leak {leaks} — 누출 문항은 hits/판정엔 남기되 표시 유지)")


def cmd_hits(args: argparse.Namespace) -> None:
    from orthus.wiki.retrieve import retrieve

    golden = _load_jsonl(DATA_DIR / "qa_golden.jsonl")
    out_path = DATA_DIR / "qa_hits.jsonl"
    done = {r["id"] for r in _load_jsonl(out_path)}
    n = 0
    with out_path.open("a", encoding="utf-8") as out:
        for g in golden:
            if g["id"] in done:
                continue
            refs = retrieve(SEED_USER, g["q"], k=5, scope="company")
            out.write(
                json.dumps(
                    {"id": g["id"], "q": g["q"], "hits": [json.loads(r.model_dump_json()) for r in refs]},
                    ensure_ascii=False,
                )
                + "\n"
            )
            n += 1
            if n % 20 == 0:
                print(f"  hits {n}")
    print(f"captured hits for {n} questions -> {out_path}")


def cmd_answer(args: argparse.Namespace) -> None:
    from orthus.schemas.canonical import WikiSourceRef
    from orthus.wiki import qa as qa_mod

    from variants_qa import VARIANTS

    variant = VARIANTS[args.variant]
    if variant.get("system"):
        qa_mod._SYSTEM = variant["system"]
    if variant.get("system_append"):
        qa_mod._SYSTEM = qa_mod._SYSTEM + variant["system_append"]

    chat = _pool_chat(args.model)
    hits_rows = _load_jsonl(DATA_DIR / "qa_hits.jsonl")
    out_path = RAW_DIR / f"qa_{args.variant}_{args.model}.jsonl"
    done = {r["id"] for r in _load_jsonl(out_path)}
    n = 0
    with out_path.open("a", encoding="utf-8") as out:
        for row in hits_rows:
            if row["id"] in done:
                continue
            if args.limit and n >= args.limit:
                break
            refs = [WikiSourceRef.model_validate(h) for h in row["hits"]]
            try:
                ans = qa_mod.answer_from_hits(
                    SEED_USER, row["q"], refs, chat_model=chat, learn=False, record_gaps=False
                )
                rec = {"id": row["id"], "q": row["q"], "answer": ans.answer, "n_hits": len(refs)}
            except Exception as exc:
                rec = {"id": row["id"], "q": row["q"], "error": f"{type(exc).__name__}: {exc}"}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            if n % 20 == 0:
                print(f"  answered {n}")
    print(f"answered {n} -> {out_path}")


_JUDGE_SYSTEM = (
    "너는 사내 위키 어시스턴트 답변 품질 심사자다. 질문, 제공된 위키 근거, 그리고 "
    "익명의 두 답변 A/B를 받는다. **근거에 실제로 있는 내용만** 좋은 답으로 인정하라 "
    "— 근거에 없는 단정(환각)은 감점, 근거에 있는데 답하지 않은 회피도 감점. 그 "
    "다음 기준으로 유용성(구체성·정확성·간결성)을 비교하라. "
    'JSON만 출력: {"winner": "A" | "B" | "tie", "reason": "한 줄"}'
)


def cmd_judge(args: argparse.Namespace) -> None:
    hits_rows = {r["id"]: r for r in _load_jsonl(DATA_DIR / "qa_hits.jsonl")}
    a_rows = {r["id"]: r for r in _load_jsonl(RAW_DIR / f"qa_{args.a}_{args.model}.jsonl")}
    b_rows = {r["id"]: r for r in _load_jsonl(RAW_DIR / f"qa_{args.b}_{args.model}.jsonl")}
    common = sorted(set(a_rows) & set(b_rows) & set(hits_rows))
    chat = _pool_chat(args.judge)
    out_path = RAW_DIR / f"qajudge_{args.a}_vs_{args.b}_{args.judge}.jsonl"
    done = {r["id"] for r in _load_jsonl(out_path)}
    n = 0
    with out_path.open("a", encoding="utf-8") as out:
        for qid in common:
            if qid in done:
                continue
            ra, rb = a_rows[qid], b_rows[qid]
            if "error" in ra or "error" in rb:
                continue
            ctx = "\n".join(
                f"[{i+1}] ({h.get('page_slug','?')}) {h.get('excerpt','')[:500]}"
                for i, h in enumerate(hits_rows[qid]["hits"])
            )
            votes = []
            for first, second, la, lb in ((ra, rb, "A", "B"), (rb, ra, "B", "A")):
                user = (
                    f"질문: {ra['q']}\n\n위키 근거:\n{ctx}\n\n"
                    f"답변 A:\n{first['answer']}\n\n답변 B:\n{second['answer']}"
                )
                try:
                    v = json.loads(chat.complete(_JUDGE_SYSTEM, user, json_only=True)).get("winner", "tie")
                except Exception:
                    v = "tie"
                votes.append({"A": la, "B": lb, "tie": "tie"}.get(v, "tie"))
            final = votes[0] if votes[0] == votes[1] else "tie"
            out.write(json.dumps({"id": qid, "votes": votes, "winner": final}, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            if n % 20 == 0:
                print(f"  judged {n}")
    rows = _load_jsonl(out_path)
    from collections import Counter

    c = Counter(r["winner"] for r in rows)
    print(f"judged {n} (total {len(rows)}): A({args.a})={c['A']} B({args.b})={c['B']} tie={c['tie']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--generators", default="solar,gpt-4o")
    g.set_defaults(fn=cmd_gen)
    h = sub.add_parser("hits")
    h.set_defaults(fn=cmd_hits)
    a = sub.add_parser("answer")
    a.add_argument("--variant", required=True)
    a.add_argument("--model", default="solar")
    a.add_argument("--limit", type=int, default=0)
    a.set_defaults(fn=cmd_answer)
    j = sub.add_parser("judge")
    j.add_argument("--a", required=True)
    j.add_argument("--b", required=True)
    j.add_argument("--model", default="solar")
    j.add_argument("--judge", default="gpt-4o")
    j.set_defaults(fn=cmd_judge)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
