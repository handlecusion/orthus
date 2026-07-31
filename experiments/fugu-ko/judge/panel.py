"""E4 — 이종 judge 패널(PoLL) 재판정. 계획: analysis/e4-judge-panel-plan.md

기존 T2 판정은 judge=gpt-4o 단독이고 기준선이 gpt-4o-mini(동일 계열)라 self-preference가
방법론 최대 약점(보고서 8.1). PoLL 처방대로 **이종 3-패널 다수결**로 재판정한다.

핵심 설계 — "judge ∉ 판정 쌍":
  solar  vs baseline → gpt-4o(기존 재사용) + exaone + ax
  exaone vs baseline → gpt-4o(기존 재사용) + solar  + ax
  ax     vs baseline → gpt-4o(기존 재사용) + solar  + exaone
자기 출력 판정은 self-preference를 역방향으로 재도입하므로 쌍 참여 모델은 그 쌍의 judge에서 뺀다.

변인 통제: 판정 프로토콜은 기존과 **byte-identical**(`pairwise._SYS`/`_prompt`/양방향 2회 +
방향 불일치→tie)이며 **judge 모델만 교체**한다. 워커 응답은 원자료(`t2_*.jsonl`) 고정 —
워커 재실행 0회. gpt-4o 신규 콜 0회(`t2_judge.jsonl` 재사용).

신규 콜: 30문항 × 3쌍 × 2방향 × 2 국내 judge = **360콜**.

출력: analysis/raw/t2_panel_judge.jsonl — {judge, worker, id, v_fwd, v_rev, result, err_*}
집계/진단은 신규 콜 0회인 `e4_analyze.py`가 별도로 수행한다.

실행:
  PYTHONPATH=<repo> python experiments/fugu-ko/judge/panel.py --smoke   # G-스모크(5문항)
  PYTHONPATH=<repo> python experiments/fugu-ko/judge/panel.py           # 본판정(360콜, resume 지원)
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT.parent.parent))  # orthus repo root
sys.path.insert(0, str(ROOT))  # experiments/fugu-ko (pool.py)

from pool import build_pool  # noqa: E402

from judge.pairwise import _SYS, BASELINE, WORKERS, _prompt, load  # noqa: E402

RAW = ROOT / "analysis" / "raw"
GOLDEN = ROOT / "golden"
OUT = RAW / "t2_panel_judge.jsonl"

# judge ∉ 판정 쌍 (§4.1). gpt-4o는 전 쌍 공통이나 기존 판정 재사용이라 여기서 호출하지 않는다.
DOMESTIC_JUDGES: dict[str, list[str]] = {
    "solar": ["exaone", "ax"],
    "exaone": ["solar", "ax"],
    "ax": ["solar", "exaone"],
}


def judge_once_detailed(judge, q: str, a: str, b: str) -> tuple[str, str | None]:
    """`pairwise.judge_once`와 동일 의미(실패→tie). 무효 조건(§5: json 실패율 >10%) 판정을
    위해 실패 사유만 추가로 돌려준다. 프롬프트/파싱 규칙은 동일."""
    try:
        out = judge.complete(_SYS, _prompt(q, a, b), json_only=True)
    except Exception as e:  # noqa: BLE001 — 호출 실패
        return "tie", f"call:{type(e).__name__}"
    try:
        w = str(json.loads(out).get("winner", "tie")).strip().lower()
    except Exception as e:  # noqa: BLE001 — json 파싱 실패
        return "tie", f"parse:{type(e).__name__}"
    v = {"a": "A", "b": "B", "tie": "tie"}.get(w)
    if v is None:
        return "tie", "parse:winner_enum"
    return v, None


def judge_pair(judge, q: str, a_worker: str, a_baseline: str) -> dict:
    """양방향 2회 + 방향 불일치→tie (기존 규칙 그대로)."""
    v1, e1 = judge_once_detailed(judge, q, a_worker, a_baseline)  # worker=A
    v2, e2 = judge_once_detailed(judge, q, a_baseline, a_worker)  # worker=B
    if v1 == "A" and v2 == "B":
        res = "win"
    elif v1 == "B" and v2 == "A":
        res = "loss"
    else:
        res = "tie"  # 위치에 흔들림 → 신뢰불가
    return {"v_fwd": v1, "v_rev": v2, "result": res, "err_fwd": e1, "err_rev": e2}


def _done_keys(path: Path) -> set[tuple[str, str, str]]:
    """resume 대상 = **실패 없이 완료된** 판정만.

    호출/파싱 실패는 tie로 흡수되므로(보수적) 그대로 두면 재실행으로 회수할 방법이 없다.
    err_fwd/err_rev가 있는 row는 done에서 빼서 다음 실행이 다시 시도하게 한다. 재시도분은
    같은 (judge, worker, id)로 append되고, 뒤 row가 앞 row를 덮어쓴다(`load_verdicts` 기준).
    """
    if not path.exists():
        return set()
    ok: dict[tuple[str, str, str], bool] = {}  # 같은 키가 여러 번이면 마지막 row가 이긴다
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        ok[(r["judge"], r["worker"], r["id"])] = not (r.get("err_fwd") or r.get("err_rev"))
    return {k for k, clean in ok.items() if clean}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="G-스모크: 첫 5문항만 (§8 게이트1)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--fresh", action="store_true", help="기존 raw 무시하고 처음부터")
    ap.add_argument(
        "--out",
        default=None,
        help="출력 jsonl 경로(기본 analysis/raw/t2_panel_judge.jsonl). "
        "재현성 측정용 독립 재실행은 별도 경로로 뽑아 기존 판정을 덮지 않는다(TS15 — judge도 비결정적).",
    )
    args = ap.parse_args()

    items = json.load(open(GOLDEN / "t2.json", encoding="utf-8"))["items"]
    if args.smoke:
        items = items[:5]
    qmap = {it["id"]: it["q"] for it in items}
    data = {m: load(m) for m in WORKERS + [BASELINE]}

    pool = build_pool(WORKERS)  # 국내 3종만 — gpt-4o는 기존 판정 재사용(신규 콜 0)

    if args.out:
        out_path = Path(args.out).resolve()  # 상대경로도 받는다(cwd 기준)
    else:
        out_path = RAW / ("t2_panel_judge_smoke.jsonl" if args.smoke else OUT.name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and out_path.exists():
        out_path.unlink()
    done = set() if args.smoke else _done_keys(out_path)
    if args.smoke and out_path.exists():
        out_path.unlink()  # 스모크는 항상 새로 (게이트 판정용)

    tasks = [
        (jslug, w, qid)
        for w in WORKERS
        for jslug in DOMESTIC_JUDGES[w]
        for qid in qmap
        if (jslug, w, qid) not in done
    ]
    if not tasks:
        print("모든 판정이 이미 존재한다(resume). --fresh로 재실행.")
        return

    # EXAONE 콜드스타트(~36s) 웜업 — 병렬 타임아웃 방어(§7).
    for slug in {j for w in WORKERS for j in DOMESTIC_JUDGES[w]}:
        t0 = time.monotonic()
        try:
            pool[slug].complete("한 단어만 답하라.", "준비됐나?", json_only=False)
            print(f"  warmup {slug:8} OK {int((time.monotonic() - t0) * 1000)}ms")
        except Exception as e:  # noqa: BLE001
            print(f"  warmup {slug:8} ERR {type(e).__name__}")

    print(f"\n== E4 패널 재판정 {'(스모크)' if args.smoke else ''} — {len(tasks)}개 판정 × 2방향 = {len(tasks) * 2}콜 ==")
    RAW.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    fh = open(out_path, "a", encoding="utf-8")
    n_err = 0
    t_start = time.monotonic()

    def run(task):
        jslug, w, qid = task
        aw = data[w].get(qid, {}).get("answer") or ""
        ab = data[BASELINE].get(qid, {}).get("answer") or ""
        row = {"judge": jslug, "worker": w, "id": qid, **judge_pair(pool[jslug], qmap[qid], aw, ab)}
        with lock:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
        return row

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(run, t): t for t in tasks}
        for i, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            n_err += sum(1 for k in ("err_fwd", "err_rev") if row[k])
            if i % 20 == 0 or i == len(tasks):
                el = time.monotonic() - t_start
                print(f"  {i:>3}/{len(tasks)}  {el:>5.0f}s  실패콜 {n_err}")
    fh.close()

    n_calls = len(tasks) * 2
    shown = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
    print(f"\n  완료 → {shown}  ({n_calls}콜, 실패 {n_err} = {n_err / n_calls * 100:.1f}%)")
    if args.smoke:
        rows = [json.loads(x) for x in out_path.read_text("utf-8").splitlines() if x.strip()]
        print("\n  [G-스모크 §8.1 — judge별 json 준수 / tie율]")
        for jslug in sorted({r["judge"] for r in rows}):
            rs = [r for r in rows if r["judge"] == jslug]
            errs = sum(1 for r in rs for k in ("err_fwd", "err_rev") if r[k])
            ties = sum(1 for r in rs if r["result"] == "tie")
            print(
                f"    {jslug:8} 판정 {len(rs):>2}  실패콜 {errs}/{len(rs) * 2} ({errs / (len(rs) * 2) * 100:>4.0f}%)"
                f"  tie {ties}/{len(rs)} ({ties / len(rs) * 100:>3.0f}%)"
            )
        print("\n    → 실패율 ≤10% & tie율 정상범위면 본판정 진입(--smoke 없이 실행)")


if __name__ == "__main__":
    main()
