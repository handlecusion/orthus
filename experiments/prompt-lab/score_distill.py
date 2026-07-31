"""Track C — distill 결과 판정자 채점기.

원칙(임베딩 실험 §12.1 교훈): **판정자에게 반드시 원문을 동봉**한다 — 안 주면
자신감 있는 환각에 가점한다. 채점 축:

1. claim별 판정: supported / fabricated / contradicts (원문 대비) — 오염률.
2. 메타-claim: "문서를 설명하는 claim"(C6 회귀) — 결정론 regex + 판정자 플래그 OR.
3. 커버리지: 판정자가 원문에서 핵심 사실 목록을 **문서당 1회** 추출(캐시,
   변형 간 공유)한 뒤, 각 사실이 claims에 커버되는지 판정.

실행 (채점):
    PYTHONPATH=$PWD:$PWD/experiments/fugu-ko:$PWD/experiments/prompt-lab \
    uv run python experiments/prompt-lab/score_distill.py score \
        --run distill_baseline_solar --judge gpt-4o [--limit N]

실행 (쌍대 비교):
    ... score_distill.py compare --a distill_baseline_solar --b distill_kr-v1_solar

출력: analysis/raw/score_{run}_{judge}.jsonl / keyfacts_{judge}.jsonl (캐시).
비교는 per-doc paired: 오염 유무 McNemar(exact), 커버리지/메타 비율 부호검정.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
RAW_DIR = LAB_DIR / "analysis" / "raw"

# C6 실측 fallback claim 23건에서 뽑은 메타-claim 결정론 패턴 (한/영)
_META_PATTERNS = [
    r"문서(입니다|이다|다\.?$)",
    r"(정리|기록|작성|설명)(한|된)\s*(문서|내용|자료)",
    r"이\s*문서(는|에는)",
    r"에\s*대한\s*(내용|정보)를?\s*(다루|담|포함)",
    r"\b(this|the)\s+document\b",
    r"\boutlines?\b",
    r"에\s*대해\s*(다루고|설명하고)\s*있",
]
_META_RE = re.compile("|".join(_META_PATTERNS), re.IGNORECASE)

_JUDGE_CLAIMS_SYSTEM = (
    "너는 지식 추출 품질 감사자다. 원문 문서와, 그 문서에서 추출된 claim 목록을 받는다.\n"
    "각 claim에 대해 원문만 근거로 판정하라:\n"
    '- "supported": 원문이 그 claim을 직접 뒷받침한다\n'
    '- "fabricated": 원문에 없는 내용이 들어갔다 (외부 지식·추측 포함)\n'
    '- "contradicts": 원문과 모순된다\n'
    "추가로 각 claim에 meta 플래그를 판정하라: 그 claim이 문서의 **내용**(사실)이 아니라\n"
    "문서 **자체를 설명**('~를 정리한 문서다' 류)하면 true.\n"
    'JSON만 출력: {"claims": [{"i": 0, "verdict": "supported", "meta": false}, ...]}'
)

_JUDGE_KEYFACTS_SYSTEM = (
    "너는 지식 감사자다. 문서를 읽고, 회사 지식베이스에 보존할 가치가 있는 **검증 가능한\n"
    "핵심 사실**을 최대 12개 나열하라. 문서에 실제로 적힌 사실만. 인사말·서식·빈 값은 제외.\n"
    "사실이 없으면 빈 배열.\n"
    'JSON만 출력: {"facts": ["...", ...]}'
)

_JUDGE_COVERAGE_SYSTEM = (
    "너는 지식 감사자다. 문서의 핵심 사실 목록과, 추출된 claim 목록을 받는다.\n"
    "각 사실이 claim 목록 중 어느 하나에라도 **의미상 담겨 있는지** 판정하라.\n"
    'JSON만 출력: {"facts": [{"i": 0, "covered": true}, ...]}'
)


def _judge_chat(judge: str):
    import pool

    # codex(ChatGPT OAuth) 판정자 — 중립 GPT급, API 빌링 무관.
    if judge == "codex":
        from codex_judge import CodexJudge

        return CodexJudge()
    # 국내 모델 판정자(solar/ax/exaone) — 벤더 독립. keys.json 경유.
    if judge in ("solar", "ax", "exaone"):
        return pool.build_pool([judge])[judge]

    spec = pool.WorkerSpec("judge", provider_match="", base_url="https://api.openai.com/v1", model=judge, timeout=90)
    import os

    key = os.environ.get("JUDGE_OPENAI_KEY", "").strip()
    if not key:
        raise SystemExit("JUDGE_OPENAI_KEY env 필요 (판정자 OpenAI 키)")
    return pool.WorkerChat(spec, key, judge)


def _jload(chat, system: str, user: str) -> dict:
    raw = chat.complete(system, user, json_only=True)
    return json.loads(raw)


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _doc_index(sample_path: Path) -> dict[str, dict]:
    docs = [
        json.loads(l)
        for l in sample_path.read_text(encoding="utf-8").splitlines()
        if l.strip() and l.strip() != "t"
    ]
    return {d["doc_id"]: d for d in docs if isinstance(d, dict)}


def _keyfacts(chat, judge: str, doc: dict, cache: dict[str, list[str]], cache_path: Path) -> list[str]:
    if doc["doc_id"] in cache:
        return cache[doc["doc_id"]]
    data = _jload(
        chat,
        _JUDGE_KEYFACTS_SYSTEM,
        f"문서 제목: {doc['title']}\n\n문서 본문:\n{doc['markdown']}",
    )
    facts = [str(f) for f in data.get("facts", []) if str(f).strip()][:12]
    cache[doc["doc_id"]] = facts
    with cache_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"doc_id": doc["doc_id"], "facts": facts}, ensure_ascii=False) + "\n")
    return facts


def cmd_score(args: argparse.Namespace) -> None:
    run_path = RAW_DIR / f"{args.run}.jsonl"
    rows = [r for r in _load_jsonl(run_path) if r.get("rep", 0) == args.rep]
    docs = _doc_index(Path(args.sample))
    chat = _judge_chat(args.judge)

    cache_path = RAW_DIR / f"keyfacts_{args.judge}.jsonl"
    cache: dict[str, list[str]] = {}
    if cache_path.exists():
        for r in _load_jsonl(cache_path):
            cache[r["doc_id"]] = r["facts"]

    out_path = RAW_DIR / f"score_{args.run}_{args.judge}.jsonl"
    done = {r["doc_id"] for r in _load_jsonl(out_path)} if out_path.exists() else set()

    n = 0
    with out_path.open("a", encoding="utf-8") as out:
        for row in rows:
            if row["doc_id"] in done:
                continue
            if args.limit and n >= args.limit:
                break
            doc = docs[row["doc_id"]]
            rec: dict = {"doc_id": row["doc_id"], "bucket": row["bucket"], "run": args.run}
            if "error" in row:
                rec["run_error"] = row["error"]
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                continue
            claims = [c["claim"] for c in row["parsed"]["claims"]]
            rec["n_claims"] = len(claims)
            rec["meta_regex"] = [bool(_META_RE.search(c)) for c in claims]
            if claims:
                claim_list = "\n".join(f"[{i}] {c}" for i, c in enumerate(claims))
                data = _jload(
                    chat,
                    _JUDGE_CLAIMS_SYSTEM,
                    f"문서 제목: {doc['title']}\n\n문서 본문:\n{doc['markdown']}\n\nclaim 목록:\n{claim_list}",
                )
                verdicts = {int(c["i"]): c for c in data.get("claims", []) if "i" in c}
                rec["verdicts"] = [
                    str(verdicts.get(i, {}).get("verdict", "missing")) for i in range(len(claims))
                ]
                rec["meta_judge"] = [bool(verdicts.get(i, {}).get("meta", False)) for i in range(len(claims))]
            else:
                rec["verdicts"] = []
                rec["meta_judge"] = []
            facts = _keyfacts(chat, args.judge, doc, cache, cache_path)
            rec["n_facts"] = len(facts)
            if facts and claims:
                fact_list = "\n".join(f"[{i}] {f}" for i, f in enumerate(facts))
                claim_list = "\n".join(f"- {c}" for c in claims)
                data = _jload(
                    chat,
                    _JUDGE_COVERAGE_SYSTEM,
                    f"핵심 사실 목록:\n{fact_list}\n\nclaim 목록:\n{claim_list}",
                )
                cov = {int(f["i"]): bool(f.get("covered")) for f in data.get("facts", []) if "i" in f}
                rec["covered"] = [cov.get(i, False) for i in range(len(facts))]
            else:
                rec["covered"] = [False] * len(facts)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            print(f"[{n}] {row['doc_id'][:8]} claims={rec['n_claims']} facts={rec['n_facts']}")
    print(f"scored {n} -> {out_path}")


def _doc_metrics(rec: dict) -> dict | None:
    if "run_error" in rec:
        return {"error": True}
    v = rec["verdicts"]
    meta = [a or b for a, b in zip(rec["meta_regex"], rec["meta_judge"])]
    n = len(v)
    return {
        "error": False,
        "n_claims": n,
        "fab": sum(1 for x in v if x in ("fabricated", "contradicts")),
        "meta": sum(meta),
        "cov_num": sum(rec["covered"]),
        "cov_den": rec["n_facts"],
    }


def _summary(scores: list[dict]) -> dict:
    ms = [_doc_metrics(r) for r in scores]
    ok = [m for m in ms if m and not m["error"]]
    total_claims = sum(m["n_claims"] for m in ok)
    return {
        "docs": len(ms),
        "run_errors": sum(1 for m in ms if m and m["error"]),
        "claims_per_doc": round(total_claims / max(len(ok), 1), 2),
        "contamination_rate": round(sum(m["fab"] for m in ok) / max(total_claims, 1), 4),
        "docs_with_contamination": sum(1 for m in ok if m["fab"] > 0),
        "meta_claim_rate": round(sum(m["meta"] for m in ok) / max(total_claims, 1), 4),
        "coverage": round(
            sum(m["cov_num"] for m in ok) / max(sum(m["cov_den"] for m in ok), 1), 4
        ),
    }


def cmd_compare(args: argparse.Namespace) -> None:
    import significance  # experiments/fugu-ko/embedding/significance.py

    a = {r["doc_id"]: r for r in _load_jsonl(RAW_DIR / f"score_{args.a}_{args.judge}.jsonl")}
    b = {r["doc_id"]: r for r in _load_jsonl(RAW_DIR / f"score_{args.b}_{args.judge}.jsonl")}
    common = sorted(set(a) & set(b))
    print(f"A={args.a}\nB={args.b}\npaired docs={len(common)}\n")
    print("A summary:", json.dumps(_summary([a[d] for d in common]), ensure_ascii=False))
    print("B summary:", json.dumps(_summary([b[d] for d in common]), ensure_ascii=False))

    # per-doc 오염 유무 paired McNemar
    disc_ab = disc_ba = 0
    for d in common:
        ma, mb = _doc_metrics(a[d]), _doc_metrics(b[d])
        if ma["error"] or mb["error"]:
            continue
        ca, cb = ma["fab"] > 0, mb["fab"] > 0
        if ca and not cb:
            disc_ab += 1  # A만 오염
        elif cb and not ca:
            disc_ba += 1  # B만 오염
    p = significance.exact_mcnemar(disc_ab, disc_ba)
    print(f"\ncontamination (doc-level): A-only={disc_ab} B-only={disc_ba} McNemar p={p:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--run", required=True)
    s.add_argument("--judge", default="gpt-4o")
    s.add_argument("--rep", type=int, default=0)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--sample", default=str(LAB_DIR / "data" / "distill_sample.jsonl"))
    s.set_defaults(fn=cmd_score)
    c = sub.add_parser("compare")
    c.add_argument("--a", required=True)
    c.add_argument("--b", required=True)
    c.add_argument("--judge", default="gpt-4o")
    c.set_defaults(fn=cmd_compare)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
