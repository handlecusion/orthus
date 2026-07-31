"""delegation_extract 측정 — 자유 텍스트에서 '팀원 위임 지시'를 뽑는 안전 분류기.

**1차 지표는 정확도가 아니라 오탐(FP)이다.** is_delegation=true 로 판정되면 팀원의 collector
daemon에서 headless agent가 **샌드박스 없이 full local file access**로 실행된다(AGENTS.md
carve-out). 위임이 아닌 메일을 위임으로 읽으면 남의 PC에서 에이전트가 도는 것이다. 그래서
FP 1건은 FN 1건보다 훨씬 비싸고, 표에서 FP를 따로 뽑아 보여준다.

프로덕션 프롬프트 `_EXTRACT_SYSTEM_PROMPT`를 그대로 쓰고 `_parse_extraction`으로 파싱해
프로덕션과 같은 관문을 통과시킨다(파싱 실패 = 위임 안 함).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
MODELS = ["solar", "ax", "exaone", "baseline"]


def items() -> list[dict]:
    return json.loads((HERE / "golden" / "t10_delegation.json").read_text(encoding="utf-8"))["items"]


def run(models: list[str]) -> None:
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent.parent))
    from pool import build_pool

    from orthus.agentwork.delegation import _EXTRACT_SYSTEM_PROMPT, _parse_extraction

    pool = build_pool(models)
    RAW.mkdir(parents=True, exist_ok=True)
    data = items()
    for slug in models:
        chat = pool[slug]
        with (RAW / f"t10_{slug}.jsonl").open("w", encoding="utf-8") as fh:
            for n, it in enumerate(data, 1):
                t0 = time.monotonic()
                rec: dict = {"id": it["id"]}
                try:
                    raw = (chat.complete(_EXTRACT_SYSTEM_PROMPT, it["text"], json_only=True) or "").strip()
                    p = _parse_extraction(raw)
                    # 프로덕션과 동일한 관문: 파싱 실패 / is_delegation!=True / instruction 공백 → 위임 안 함
                    is_del = bool(p) and p.get("is_delegation") is True
                    instr = str((p or {}).get("instruction") or "").strip()
                    rec["is_delegation"] = bool(is_del and instr)
                    rec["assignee"] = str((p or {}).get("assignee") or "").strip()
                    rec["mode"] = str((p or {}).get("mode") or "").strip().lower()
                    rec["parse_ok"] = bool(p)
                except Exception as e:  # noqa: BLE001
                    rec |= {"is_delegation": False, "assignee": "", "mode": "", "parse_ok": False,
                            "error": str(e)[:100]}
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

    gold = {i["id"]: i for i in items()}
    n_pos = sum(1 for g in gold.values() if g["is_delegation"])
    print(f"\n  ══ delegation_extract ({len(gold)}건 = 위임 {n_pos} + 함정 {len(gold) - n_pos}) ══")
    print("  ⚠ FP 1건 = 남의 PC에서 full-access 에이전트 실행. FP가 1차 지표다.\n")
    ok: dict[str, dict[str, bool]] = {}
    for m in models:
        p = RAW / f"t10_{m}.jsonl"
        if not p.exists():
            continue
        d = {}
        tp = fp = fn = 0
        mode_ok = mode_tot = 0
        name_ok = 0
        fps: list[str] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            g = gold[r["id"]]
            got, want = bool(r["is_delegation"]), bool(g["is_delegation"])
            d[r["id"]] = got == want
            if want and got:
                tp += 1
                mode_tot += 1
                mode_ok += r.get("mode") == g["mode"]
                name_ok += g["assignee"].split("@")[0] in r.get("assignee", "")
            elif not want and got:
                fp += 1
                fps.append(r["id"])
            elif want and not got:
                fn += 1
        ok[m] = d
        k = sum(d.values())
        print(
            f"  {m:9} 정확 {k:2}/{len(d)} ({k / len(d) * 100:5.1f}%)  "
            f"| ⚠FP {fp}  FN {fn}  | mode {mode_ok}/{mode_tot}  assignee {name_ok}/{tp}"
            + (f"  ← FP: {fps}" if fps else "")
        )

    dom = [m for m in ("solar", "ax", "exaone") if m in ok]
    if len(dom) > 1:
        best = max(dom, key=lambda m: sum(ok[m].values()))
        print("\n  ── 국내 모델 쌍대 (is_delegation, McNemar) ──")
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
