"""Track A 잔여 표면 — rewrite(후속 재작성) · synthesize(합성) · decompose(분해 게이트).

방침: 먼저 baseline을 결정론+판정자로 재고, 실제 약점이 보이는 표면에만 변형을
설계한다 (앞 라운드 교훈 — 약점 없는 표면의 문구 개입은 대체로 무효).

서브커맨드:
  rw-gen / rw-run --variant / rw-eval --a --b
  syn-gen / syn-run --variant / syn-eval --a [--b]
  dec-run [--golden t7|t7_holdout]

환경: PYTHONPATH=$PWD:$PWD/experiments/fugu-ko:$PWD/experiments/prompt-lab
  FUGU_KEYS=... JUDGE_OPENAI_KEY=...
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
RAW_DIR = LAB_DIR / "analysis" / "raw"
DATA_DIR = LAB_DIR / "data"
FUGU_GOLDEN = LAB_DIR.parent / "fugu-ko" / "golden"

CIT_RE = re.compile(r"문서\s*\[|(?<![\w가-힣])\[\d+\]")


def _pool_chat(slug: str):
    import pool

    if slug.startswith("gpt"):
        key = os.environ.get("JUDGE_OPENAI_KEY", "").strip()
        spec = pool.WorkerSpec("gptx", provider_match="", base_url="https://api.openai.com/v1", model=slug, timeout=90)
        return pool.WorkerChat(spec, key, slug)
    return pool.build_pool([slug])[slug]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _append(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── rewrite ──────────────────────────────────────────────────────────────────

_FOLLOWUP_TEMPLATES = [
    "그거 담당자가 누구야?",
    "방금 그 내용 좀 더 자세히 알려줘",
    "아까 그거 일정이 어떻게 돼?",
    "그건 왜 그런 거야?",
    "그거 관련해서 다른 프로젝트에도 적용돼?",
    "위에서 말한 거 요약해줘",
    "그때 결정된 내용이 뭐였지?",
    "그거 지금 상태가 어때?",
]


def cmd_rw_gen(args: argparse.Namespace) -> None:
    golden = _load_jsonl(DATA_DIR / "qa_golden.jsonl")
    answers = {r["id"]: r for r in _load_jsonl(RAW_DIR / "qa_baseline_solar.jsonl") if "answer" in r}
    out = DATA_DIR / "rw_golden.jsonl"
    done = {r["id"] for r in _load_jsonl(out)}
    n = 0
    for i, g in enumerate(golden):
        if g["id"] not in answers or len(answers[g["id"]]["answer"]) < 80:
            continue
        rid = f"rw-{g['id']}"
        if rid in done or n >= 80:
            continue
        _append(
            out,
            {
                "id": rid,
                "history": [
                    {"role": "user", "text": g["q"]},
                    {"role": "assistant", "text": answers[g["id"]]["answer"][:600]},
                ],
                "followup": _FOLLOWUP_TEMPLATES[i % len(_FOLLOWUP_TEMPLATES)],
                "topic": g["title"],
            },
        )
        n += 1
    print(f"rw golden: +{n} (total {len(_load_jsonl(out))})")


def cmd_rw_run(args: argparse.Namespace) -> None:
    from orthus.router import rewrite as rw_mod
    from orthus.schemas.canonical import ChatTurn

    from variants_misc import REWRITE_VARIANTS

    v = REWRITE_VARIANTS[args.variant]
    if v.get("system"):
        rw_mod._SYSTEM = v["system"]
    if v.get("system_append"):
        rw_mod._SYSTEM = rw_mod._SYSTEM + v["system_append"]

    chat = _pool_chat(args.model)
    out = RAW_DIR / f"rw_{args.variant}_{args.model}.jsonl"
    done = {r["id"] for r in _load_jsonl(out)}
    n = 0
    for g in _load_jsonl(DATA_DIR / "rw_golden.jsonl"):
        if g["id"] in done:
            continue
        history = [ChatTurn(role=t["role"], text=t["text"]) for t in g["history"]]
        got = rw_mod.resolve_followup(g["followup"], history, chat_model=chat)
        _append(out, {"id": g["id"], "followup": g["followup"], "rewritten": got, "topic": g["topic"]})
        n += 1
    print(f"rw {args.variant}: +{n}")


_RW_JUDGE = (
    "너는 질문 재작성 심사자다. 이전 대화(주제), 후속 질문, 재작성 결과를 받는다. "
    "재작성이 (a) 이전 대화 없이도 이해되는 self-contained 질문이고 (b) 후속 질문의 "
    "의도(요구 종류)를 왜곡 없이 보존하며 (c) 이전 대화의 올바른 대상을 가리키면 pass다. "
    'JSON만 출력: {"pass": true|false, "why": "한 줄"}'
)

_REF_TOKENS = ("그거", "그것", "그건", "해당", "이거", "이것", "저거", "아까", "방금", "위에서", "앞서", "거기", "그때")


def cmd_rw_eval(args: argparse.Namespace) -> None:
    import significance

    golden = {g["id"]: g for g in _load_jsonl(DATA_DIR / "rw_golden.jsonl")}
    chat = _pool_chat(args.judge)

    def det(rows):
        out = {}
        for r in rows:
            rw = r["rewritten"]
            compact = rw.replace(" ", "")
            out[r["id"]] = {
                "unchanged": rw.strip() == r["followup"].strip(),
                "ref_left": any(t in compact for t in _REF_TOKENS),
            }
        return out

    def judged(run):
        cache = RAW_DIR / f"rwjudge_{run}_{args.judge}.jsonl"
        have = {r["id"]: r for r in _load_jsonl(cache)}
        rows = _load_jsonl(RAW_DIR / f"rw_{run}_{args.model}.jsonl")
        for r in rows:
            if r["id"] in have:
                continue
            g = golden[r["id"]]
            convo = "\n".join(f"{t['role']}: {t['text'][:300]}" for t in g["history"])
            user = f"이전 대화:\n{convo}\n\n후속 질문: {r['followup']}\n\n재작성: {r['rewritten']}"
            try:
                res = json.loads(chat.complete(_RW_JUDGE, user, json_only=True))
                rec = {"id": r["id"], "pass": bool(res.get("pass"))}
            except Exception:
                rec = {"id": r["id"], "pass": False}
            _append(cache, rec)
            have[r["id"]] = rec
        return {k: v["pass"] for k, v in have.items()}

    runs = [args.a] + ([args.b] if args.b else [])
    results = {}
    for run in runs:
        rows = _load_jsonl(RAW_DIR / f"rw_{run}_{args.model}.jsonl")
        d = det(rows)
        j = judged(run)
        ids = sorted(d)
        results[run] = (d, j)
        print(
            f"{run}: n={len(ids)} 무변경={sum(d[i]['unchanged'] for i in ids)} "
            f"지시어잔존={sum(d[i]['ref_left'] for i in ids)} "
            f"judge-pass={sum(j.get(i, False) for i in ids)}/{len(ids)}"
        )
    if args.b:
        da, ja = results[args.a]
        db, jb = results[args.b]
        common = sorted(set(ja) & set(jb))
        ab = sum(1 for i in common if ja[i] and not jb[i])
        ba = sum(1 for i in common if jb[i] and not ja[i])
        print(f"judge-pass paired McNemar: {args.a}-only={ab} {args.b}-only={ba} p={significance.exact_mcnemar(ab, ba):.4f}")


# ── synthesize ───────────────────────────────────────────────────────────────


def cmd_syn_gen(args: argparse.Namespace) -> None:
    golden = _load_jsonl(DATA_DIR / "qa_golden.jsonl")
    answers = {r["id"]: r for r in _load_jsonl(RAW_DIR / "qa_baseline_solar.jsonl") if "answer" in r}
    usable = [g for g in golden if g["id"] in answers and len(answers[g["id"]]["answer"]) > 80]
    out = DATA_DIR / "syn_golden.jsonl"
    done = {r["id"] for r in _load_jsonl(out)}
    n = 0
    for i in range(0, min(len(usable) - 1, 60), 2):
        g1, g2 = usable[i], usable[i + 1]
        with_action = (i // 2) % 3 == 2  # 1/3은 action 절 포함 (rule 5 검사)
        q = f"{g1['q']} 그리고 {g2['q']}"
        if with_action:
            q += " 정리해서 김서연한테 메일로 보내줘"
        rid = f"syn-{i//2:02d}"
        if rid in done:
            continue
        _append(
            out,
            {
                "id": rid,
                "q": q,
                "with_action": with_action,
                "subs": [
                    {"q": g1["q"], "a": answers[g1["id"]]["answer"][:800]},
                    {"q": g2["q"], "a": answers[g2["id"]]["answer"][:800]},
                ],
            },
        )
        n += 1
    print(f"syn golden: +{n} (total {len(_load_jsonl(out))})")


def cmd_syn_run(args: argparse.Namespace) -> None:
    from orthus.router import decompose as dec_mod

    from variants_misc import SYN_VARIANTS

    v = SYN_VARIANTS[args.variant]
    system = dec_mod._SYNTHESIZE_SYSTEM
    if v.get("system"):
        system = v["system"]
    if v.get("system_append"):
        system = system + v["system_append"]

    chat = _pool_chat(args.model)
    out = RAW_DIR / f"syn_{args.variant}_{args.model}.jsonl"
    done = {r["id"] for r in _load_jsonl(out)}
    n = 0
    for g in _load_jsonl(DATA_DIR / "syn_golden.jsonl"):
        if g["id"] in done:
            continue
        parts = [
            f"[{i + 1}] Sub-question: {s['q']}\nAnswer: {s['a']}" for i, s in enumerate(g["subs"])
        ]
        user = (
            f"Original compound question: {g['q']}\n\n"
            f"Sub-answers:\n" + "\n\n".join(parts) + "\n\nSynthesized answer:"
        )
        try:
            ans = chat.complete(system, user)
            _append(out, {"id": g["id"], "answer": ans})
        except Exception as exc:
            _append(out, {"id": g["id"], "error": str(exc)})
        n += 1
    print(f"syn {args.variant}: +{n}")


_ACTION_RE = re.compile(r"메일(을|로)?\s*(보내|발송|전송)|보내\s*드리|발송하겠|전송하겠|메일로 정리")

_SYN_JUDGE = (
    "너는 합성 답변 심사자다. 복합 질문, 두 개의 하위 답변, 그리고 합성 결과를 받는다. "
    "합성이 (a) 두 하위 답변의 핵심 사실을 모두 담고 (b) 하위 답변에 없는 새 사실을 만들지 "
    "않았으면 pass다. "
    'JSON만 출력: {"pass": true|false, "dropped": 0|1|2, "why": "한 줄"}'
)


def cmd_syn_eval(args: argparse.Namespace) -> None:
    import significance

    golden = {g["id"]: g for g in _load_jsonl(DATA_DIR / "syn_golden.jsonl")}
    chat = _pool_chat(args.judge)

    def evaluate(run):
        cache = RAW_DIR / f"synjudge_{run}_{args.judge}.jsonl"
        have = {r["id"]: r for r in _load_jsonl(cache)}
        rows = [r for r in _load_jsonl(RAW_DIR / f"syn_{run}_{args.model}.jsonl") if "answer" in r]
        det = {}
        for r in rows:
            g = golden[r["id"]]
            det[r["id"]] = {
                "cite": bool(CIT_RE.search(r["answer"])),
                "action_leak": bool(g["with_action"] and _ACTION_RE.search(r["answer"])),
            }
            if r["id"] not in have:
                subs = g["subs"]
                user = (
                    f"복합 질문: {g['q']}\n\n하위 답변 1 ({subs[0]['q']}):\n{subs[0]['a']}\n\n"
                    f"하위 답변 2 ({subs[1]['q']}):\n{subs[1]['a']}\n\n합성 결과:\n{r['answer']}"
                )
                try:
                    res = json.loads(chat.complete(_SYN_JUDGE, user, json_only=True))
                    rec = {"id": r["id"], "pass": bool(res.get("pass")), "dropped": int(res.get("dropped", 0))}
                except Exception:
                    rec = {"id": r["id"], "pass": False, "dropped": -1}
                _append(cache, rec)
                have[r["id"]] = rec
        return det, {k: v for k, v in have.items()}

    runs = [args.a] + ([args.b] if args.b else [])
    results = {}
    for run in runs:
        det, j = evaluate(run)
        ids = sorted(det)
        act_ids = [i for i in ids if golden[i]["with_action"]]
        results[run] = (det, j)
        print(
            f"{run}: n={len(ids)} 인용마커={sum(det[i]['cite'] for i in ids)} "
            f"action누설={sum(det[i]['action_leak'] for i in ids)}/{len(act_ids)} "
            f"judge-pass={sum(j[i]['pass'] for i in ids if i in j)}/{len(ids)} "
            f"sub누락건={sum(1 for i in ids if i in j and j[i]['dropped'] > 0)}"
        )
    if args.b:
        (da, ja), (db, jb) = results[args.a], results[args.b]
        common = sorted(set(da) & set(db))
        ab = sum(1 for i in common if da[i]["cite"] and not db[i]["cite"])
        ba = sum(1 for i in common if db[i]["cite"] and not da[i]["cite"])
        print(f"인용마커 paired McNemar: {args.a}-only={ab} {args.b}-only={ba} p={significance.exact_mcnemar(ab, ba):.4f}")


# ── decompose gate ───────────────────────────────────────────────────────────


def cmd_dec_run(args: argparse.Namespace) -> None:
    from orthus.router.decompose import should_decompose

    chat = _pool_chat(args.model)
    data = json.load(open(FUGU_GOLDEN / f"{args.golden}.json", encoding="utf-8"))
    ok = 0
    fails = []
    for it in data["items"]:
        got = should_decompose(it["q"], chat_model=chat, ext_tier=args.ext_tier)
        exp = bool(it.get("compound"))
        if got == exp:
            ok += 1
        else:
            fails.append((it["id"], it["q"][:40], exp, got))
    print(f"decompose gate ({args.golden}): {ok}/{len(data['items'])}")
    for f in fails:
        print("  FAIL", f)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, extra in [
        ("rw-gen", cmd_rw_gen, []),
        ("rw-run", cmd_rw_run, ["variant"]),
        ("rw-eval", cmd_rw_eval, ["a", "b?"]),
        ("syn-gen", cmd_syn_gen, []),
        ("syn-run", cmd_syn_run, ["variant"]),
        ("syn-eval", cmd_syn_eval, ["a", "b?"]),
        ("dec-run", cmd_dec_run, ["golden?"]),
    ]:
        p = sub.add_parser(name)
        if "variant" in extra:
            p.add_argument("--variant", required=True)
        if "a" in extra:
            p.add_argument("--a", required=True)
        if "b?" in extra:
            p.add_argument("--b", default=None)
        if "golden?" in extra:
            p.add_argument("--golden", default="t7_holdout")
            p.add_argument("--ext-tier", type=int, default=None)
        p.add_argument("--model", default="solar")
        p.add_argument("--judge", default="gpt-4o")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
