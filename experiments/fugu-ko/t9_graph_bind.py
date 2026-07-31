"""graph_bind 측정 — KG 질문에서 intent(enum) + 명사구(subjects) 추출.

프로덕션의 LLM 단계만 격리해서 잰다. `bind_graph_params()`는 LLM 추출 뒤에 slug resolve /
template 선택을 하는데 그건 **결정론 코드**라 모델과 무관하고, KG 가용성에 의존해 None을
반환하면 intent 정보까지 잃는다. 그래서 프로덕션 프롬프트 `_BIND_SYSTEM`을 그대로 쓰되
`chat.complete(...)` 한 번만 호출하고 그 JSON을 채점한다 — 모델이 실제로 하는 일이 그것뿐이다.

채점:
- **intent**: `_INTENTS`(relation|conflict|provenance|entity) 완전일치. 1차 지표(완전 객관).
- **subjects**: 기대 명사구와의 집합 일치. 부분 점수(재현율) — 표기 흔들림이 있어 2차 지표.
- **parse**: JSON 파싱 실패 / intent 미허용값이면 프로덕션은 None → wiki 강등이다. 그 비율도 센다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
MODELS = ["solar", "ax", "exaone", "baseline"]


def items() -> list[dict]:
    return json.loads((HERE / "golden" / "t9_graph_bind.json").read_text(encoding="utf-8"))["items"]


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def run(models: list[str]) -> None:
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent.parent))
    import time

    from pool import build_pool

    from orthus.router.graph import _BIND_SYSTEM, _INTENTS

    pool = build_pool(models)
    RAW.mkdir(parents=True, exist_ok=True)
    data = items()
    for slug in models:
        chat = pool[slug]
        with (RAW / f"t9_{slug}.jsonl").open("w", encoding="utf-8") as fh:
            for n, it in enumerate(data, 1):
                t0 = time.monotonic()
                rec: dict = {"id": it["id"]}
                try:
                    raw = chat.complete(_BIND_SYSTEM, f"Question: {it['q']}", json_only=True)
                    d = json.loads(raw)
                    intent = d.get("intent")
                    subs = d.get("subjects")
                    rec["intent"] = intent if intent in _INTENTS else None
                    rec["subjects"] = [s for s in subs if isinstance(s, str)] if isinstance(subs, list) else []
                    rec["parse_ok"] = rec["intent"] is not None
                except Exception as e:  # noqa: BLE001
                    rec |= {"intent": None, "subjects": [], "parse_ok": False, "error": str(e)[:100]}
                rec["ms"] = int((time.monotonic() - t0) * 1000)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  {slug:9} {n:2}/{len(data)} {it['id']}", flush=True)


def mcnemar(a: dict, b: dict) -> tuple[int, int, float]:
    ids = set(a) & set(b)
    w = sum(1 for i in ids if a[i] and not b[i])
    lo = sum(1 for i in ids if not a[i] and b[i])
    n = w + lo
    if n == 0:
        return w, lo, 1.0
    k = min(w, lo)
    return w, lo, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if not a.score_only:
        run(models)

    data = {i["id"]: i for i in items()}
    print(f"\n  ══ graph_bind ({len(data)}문항: relation/conflict/provenance/entity 각 8) ══")
    ok: dict[str, dict[str, bool]] = {}
    for m in models:
        p = RAW / f"t9_{m}.jsonl"
        if not p.exists():
            continue
        d, conf = {}, Counter()
        subj_hit = subj_tot = 0
        bad_parse = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            g = data[r["id"]]
            d[r["id"]] = r.get("intent") == g["intent"]
            if not r.get("parse_ok"):
                bad_parse += 1
            if r.get("intent") != g["intent"]:
                conf[f"{g['intent']}→{r.get('intent')}"] += 1
            want = {_norm(s) for s in g["subjects"]}
            got = {_norm(s) for s in r.get("subjects", [])}
            subj_hit += len(want & got)
            subj_tot += len(want)
        ok[m] = d
        k = sum(d.values())
        print(
            f"  {m:9} intent {k:2}/{len(d)} ({k / len(d) * 100:5.1f}%)  "
            f"| subjects 재현율 {subj_hit}/{subj_tot} ({subj_hit / subj_tot * 100:.0f}%)  "
            f"| 파싱실패(→wiki강등) {bad_parse}"
        )
        for kk, v in conf.most_common(3):
            print(f"      오분류 ×{v}: {kk}")

    dom = [m for m in ("solar", "ax", "exaone") if m in ok]
    if len(dom) > 1:
        best = max(dom, key=lambda m: sum(ok[m].values()))
        print("\n  ── 국내 모델 쌍대 (intent, McNemar) ──")
        for m in dom:
            if m == best:
                continue
            w, lo, p = mcnemar(ok[best], ok[m])
            print(
                f"    {best} vs {m:7}: {best}만맞음 {w:2}, {m}만맞음 {lo:2} → p={p:.4f}"
                f"  {'★ 유의미' if p < 0.05 else '차이 불충분'}"
            )


if __name__ == "__main__":
    main()
