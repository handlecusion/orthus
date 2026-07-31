"""Metric 05 — Assistant Arena: 쌍대 판정 + 집계.

prereg: analysis/arena-prereg.md (§8 개정 포함). 기존 검증 자산 verbatim 재사용:

- email : `judge/pairwise.py`의 `_SYS`/`_prompt` (D3 프로토콜) — [질문] = 렌더된 요청.
- t2    : `t2_holdout_judge.py`의 grounded `_SYS`/`_prompt` — 판정자가 [근거 — 위키 원문]을
          함께 받는다(무근거 판정이 확신에 찬 환각을 보상하는 결함의 수정판).
- 양방향(A↔B 스왑) 2회, 두 방향 일치 시만 판정자 verdict, 그 외 tie. 오류/파싱실패 = tie.

판정자(§8 개정 — 유일한 OpenAI 키 insufficient_quota로 gpt-4o 불가, 판정 시작 전 개정):
**2인 패널, judge ∉ 판정 쌍** — ① bedrock claude-haiku-4-5(프론티어 계열 → 국내에 보수적)
② 판정 쌍에 없는 국내 모델(exaone 배틀=solar, solar 배틀=exaone). 패널 collapse는 두
verdict가 같은 승자일 때만 win/loss(그 외 tie). B4-X1 검증은 gpt-4o에만 성립 — 이 패널에
승계되지 않음(보고서 caveat 필수).

t2 근거는 조립(exaone) arm이 기록한 retrieved sources의 excerpt 상위 3개(각 400자) —
retrieval은 chat_model 무관이며, 실행 시 두 arm의 source slug 집합 일치를 sanity-check한다.

    .venv/bin/python experiments/fugu-ko/arena_judge.py
    .venv/bin/python experiments/fugu-ko/arena_judge.py --score-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import b2_run  # noqa: E402  — load_dotenv + 프로덕션 어댑터 빌더 재사용
from judge.pairwise import _SYS as EMAIL_SYS, _prompt as email_prompt  # noqa: E402
from t2_holdout_judge import _SYS as T2_SYS, _prompt as t2_prompt  # noqa: E402

RAW = HERE / "raw"
OUT = RAW / "arena_judge.jsonl"

DOMESTIC = ["exaone", "solar"]  # exaone=조립 1차(주 배틀), solar=보조(report-only)
FRONTIER = ["claude-sonnet-4-6"]  # gpt-5.3은 quota로 취소(prereg §8); --frontier로 교체 가능
_OUT_SUFFIX = ""  # --out-suffix — 기존 verdict 파일을 덮지 않기 위한 파일명 접미사

# judge ∉ 판정 쌍: haiku(프론티어 계열) + 쌍에 없는 국내 모델.
PANEL = {"exaone": ["claude-haiku-4-5", "solar"], "solar": ["claude-haiku-4-5", "exaone"]}


def _load(task: str, slug: str) -> dict[str, dict]:
    p = RAW / f"arena_{task}_{slug}.jsonl"
    return {
        r["id"]: r
        for r in map(json.loads, p.read_text("utf-8").splitlines())
        if r and "error" not in r
    }


def _email_q(rec: dict) -> str:
    ctx = f"\n[참고 자료]\n{rec['ctx']}" if rec.get("ctx") else ""
    return f"받는 사람: {rec['to']}\n요청: {rec['inst']}{ctx}"


def _email_a(rec: dict) -> str:
    return f"제목: {rec.get('subject') or ''}\n\n{rec.get('body') or ''}"


def _t2_srcs(rec: dict) -> str:
    return "\n".join(
        f"[{n}] {(s.get('excerpt') or '')[:400]}"
        for n, s in enumerate(rec.get("sources", [])[:3], 1)
    )


_call_counts: dict[str, int] = {}
_parse_fails: dict[str, int] = {}
_count_lock = threading.Lock()

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _parse_winner(raw: str) -> str | None:
    """JSON에서 winner 추출. 코드펜스 관용(run 1 결함 수정 — haiku가 ```json 펜스로 감싸
    240표 전부가 파싱 실패 → tie로 죽었다. b2에 문서화된 haiku C1 특성). 실패 시 None."""
    s = _FENCE.sub("", (raw or "").strip())
    try:
        w = str(json.loads(s).get("winner", "")).strip()
    except Exception:  # noqa: BLE001
        m = re.search(r"\{[^{}]*\}", s, re.S)
        if not m:
            return None
        try:
            w = str(json.loads(m.group(0)).get("winner", "")).strip()
        except Exception:  # noqa: BLE001
            return None
    return w if w in ("A", "B", "tie") else None


def _vote(judge_slug: str, judge, sys_p: str, user_p: str) -> str:  # noqa: ANN001
    with _count_lock:
        _call_counts[judge_slug] = _call_counts.get(judge_slug, 0) + 1
    try:
        raw = judge.complete(sys_p, user_p, json_only=True)
    except Exception:  # noqa: BLE001
        raw = None
    w = _parse_winner(raw) if raw is not None else None
    if w is None:
        with _count_lock:
            _parse_fails[judge_slug] = _parse_fails.get(judge_slug, 0) + 1
        return "tie"
    return w


def judge_verdict(judge_slug: str, judge, task: str, dom_rec: dict, fr_rec: dict) -> dict:  # noqa: ANN001
    """판정자 1인, 양방향. A=국내 고정 방향 + 스왑. 일치 시만 win/loss."""
    if task == "email":
        q = _email_q(dom_rec)
        a, b = _email_a(dom_rec), _email_a(fr_rec)
        fwd = _vote(judge_slug, judge, EMAIL_SYS, email_prompt(q, a, b))
        rev = _vote(judge_slug, judge, EMAIL_SYS, email_prompt(q, b, a))
    else:
        q = dom_rec["question"]
        srcs = _t2_srcs(dom_rec)
        a, b = dom_rec.get("answer") or "", fr_rec.get("answer") or ""
        fwd = _vote(judge_slug, judge, T2_SYS, t2_prompt(q, srcs, a, b))
        rev = _vote(judge_slug, judge, T2_SYS, t2_prompt(q, srcs, b, a))
    if fwd == "A" and rev == "B":
        res = "win"  # 국내 승
    elif fwd == "B" and rev == "A":
        res = "loss"
    else:
        res = "tie"
    return {"fwd": fwd, "rev": rev, "verdict": res}


def _build_gpt4o_judge():  # noqa: ANN202
    """원 프로토콜 판정자(B4-X1 검증) — `judge/pairwise.py::main`과 동일 배선.

    prereg §10: quota 복구 시 `--judge gpt-4o` 한 명령으로 저장된 생성물 위에 240표를
    재판정한다(재생성 없음). quota가 죽어 있으면 preflight에서 즉시 중단."""
    import os

    from orthus.models.adapters.openai_compat import OpenAIChat

    base = os.environ.get("ORTHUS_LLM_BASE_URL", "https://api.openai.com/v1")
    key = (
        os.environ.get("ORTHUS_LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    ).strip()
    if not key:
        raise SystemExit("ORTHUS_LLM_API_KEY/OPENAI_API_KEY required for --judge gpt-4o")
    return OpenAIChat(base, key, "gpt-4o")


def run_battles(workers: int, bedrock_workers: int, judge_mode: str = "panel") -> None:
    judges: dict[str, object] = {}
    if judge_mode == "gpt-4o":
        j = _build_gpt4o_judge()
        try:
            j.complete("You output JSON only.", 'Respond {"ok": true}', json_only=True)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"gpt-4o judge preflight failed: {type(exc).__name__}: {exc}")
        _call_counts["gpt-4o"] = 1
        judges["gpt-4o"] = j
    else:
        for slug in {"claude-haiku-4-5", *DOMESTIC}:
            ep = b2_run.build_endpoint(slug)
            pf = b2_run.preflight(ep)
            if pf is not None:
                raise SystemExit(f"judge {slug} preflight failed: {pf}")
            _call_counts[slug] = _call_counts.get(slug, 0) + 1  # preflight 1콜 포함
            judges[slug] = ep

    verdicts: list[dict] = []
    v_lock = threading.Lock()
    jobs = []
    for task in ("t2", "email"):
        data = {}
        for slug in DOMESTIC + FRONTIER:
            try:
                data[slug] = _load(task, slug)
            except FileNotFoundError:
                print(f"[warn] arena_{task}_{slug}.jsonl missing — skipping its battles")
        # t2 retrieval sanity: 두 arm의 source slug 집합 비교 (prereg §4)
        if task == "t2" and "exaone" in data:
            for fr in FRONTIER:
                if fr not in data:
                    continue
                mism = sum(
                    1
                    for qid, r in data["exaone"].items()
                    if qid in data[fr]
                    and {s["slug"] for s in r.get("sources", [])}
                    != {s["slug"] for s in data[fr][qid].get("sources", [])}
                )
                print(
                    f"  [sanity] t2 source-slug mismatch exaone vs {fr}: "
                    f"{mism}/{len(data['exaone'])}"
                )
        for dom in DOMESTIC:
            for fr in FRONTIER:
                if dom not in data or fr not in data:
                    continue
                panel = ["gpt-4o"] if judge_mode == "gpt-4o" else PANEL[dom]
                for qid in data[dom]:
                    if qid in data[fr]:
                        for jslug in panel:
                            jobs.append((task, dom, fr, qid, jslug, data[dom][qid], data[fr][qid]))

    def work(job) -> None:  # noqa: ANN001
        task, dom, fr, qid, jslug, dr, frr = job
        v = judge_verdict(jslug, judges[jslug], task, dr, frr)
        with v_lock:
            verdicts.append(
                {"task": task, "dom": dom, "frontier": fr, "id": qid, "judge": jslug, **v}
            )

    # bedrock(haiku) 잡과 국내 잡을 분리 실행 — bedrock 동시성 ≤4 유지(prereg §8).
    bed_jobs = [j for j in jobs if j[4] == "claude-haiku-4-5"]
    dom_jobs = [j for j in jobs if j[4] != "claude-haiku-4-5"]
    for pool_jobs, w in ((dom_jobs, workers), (bed_jobs, min(bedrock_workers, 4))):
        with ThreadPoolExecutor(max_workers=w) as ex:
            done = 0
            for _ in as_completed([ex.submit(work, j) for j in pool_jobs]):
                done += 1
                if done % 40 == 0:
                    print(f"  ... {done}/{len(pool_jobs)} judge jobs in pool", flush=True)

    out = _out_path(judge_mode)
    with open(out, "w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
    print("\nexact judge .complete() calls (incl. 1 preflight each):")
    for slug, n in sorted(_call_counts.items()):
        print(f"  {slug:<18} {n}  (parse/call failures -> tie: {_parse_fails.get(slug, 0)})")
    print(f"-> {out}")


def _out_path(judge_mode: str) -> Path:
    stem = "arena_judge_gpt4o" if judge_mode == "gpt-4o" else "arena_judge"
    return RAW / f"{stem}{_OUT_SUFFIX}.jsonl"


def _binom_two_sided(wins: int, decided: int) -> float:
    """정확 이항검정(양측, p=0.5). scipy 없이 계산."""
    from math import comb

    if decided == 0:
        return 1.0
    p_hi = sum(comb(decided, k) for k in range(wins, decided + 1)) / 2**decided  # P(X >= w)
    p_lo = sum(comb(decided, k) for k in range(0, wins + 1)) / 2**decided  # P(X <= w)
    return min(1.0, 2 * min(p_hi, p_lo))


def collapse(verdicts: list[dict]) -> list[dict]:
    """항목별 패널 collapse: 두 판정자 verdict가 같은 승자일 때만 win/loss (prereg §8)."""
    by_key: dict[tuple, dict[str, str]] = {}
    for v in verdicts:
        by_key.setdefault((v["task"], v["dom"], v["frontier"], v["id"]), {})[v["judge"]] = v[
            "verdict"
        ]
    out = []
    for (task, dom, fr, qid), jv in by_key.items():
        vals = list(jv.values())
        if len(vals) == 1:
            res = vals[0]  # 단독 판정자 모드(gpt-4o) — pairwise.py 원 프로토콜 그대로
        elif len(vals) == 2 and vals[0] == vals[1] and vals[0] != "tie":
            res = vals[0]
        else:
            res = "tie"
        out.append({"task": task, "dom": dom, "frontier": fr, "id": qid, "result": res, "jv": jv})
    return out


def score(judge_mode: str = "panel") -> None:
    out = _out_path(judge_mode)
    verdicts = [json.loads(x) for x in out.read_text("utf-8").splitlines() if x.strip()]
    items = collapse(verdicts)
    label = (
        "검증 판정자 gpt-4o 양방향 일치 · prereg §10"
        if judge_mode == "gpt-4o"
        else "2인 패널 양방향 일치 · prereg §8"
    )
    print(f"\n══ Assistant Arena — W/T/L (국내 관점, {label}) ══")
    for dom in DOMESTIC:
        tag = "주 배틀" if dom == "exaone" else "보조(report-only)"
        panel = ["gpt-4o"] if judge_mode == "gpt-4o" else PANEL[dom]
        print(f"\n── 국내 {dom} ({tag}) — 판정자 {panel} ──")
        for task in ("t2", "email"):
            for fr in FRONTIER:
                vs = [
                    v for v in items if (v["task"], v["dom"], v["frontier"]) == (task, dom, fr)
                ]
                if not vs:
                    continue
                w = sum(1 for v in vs if v["result"] == "win")
                lo = sum(1 for v in vs if v["result"] == "loss")
                t = sum(1 for v in vs if v["result"] == "tie")
                dec = w + lo
                wr = w / dec * 100 if dec else float("nan")
                p = _binom_two_sided(w, dec)
                print(
                    f"  {task:5} vs {fr:18} n={len(vs):2}  W{w:2} T{t:2} L{lo:2}  "
                    f"승률(decided) {wr:5.1f}%  tie율 {t / len(vs) * 100:4.0f}%  "
                    f"binom p={p:.3f}"
                )
        # 판정자별 원 verdict 분포(패널 collapse 전) — 편향 방향 점검용
        for jslug in panel:
            jv = [v for v in verdicts if v["dom"] == dom and v["judge"] == jslug]
            w = sum(1 for v in jv if v["verdict"] == "win")
            lo = sum(1 for v in jv if v["verdict"] == "loss")
            t = sum(1 for v in jv if v["verdict"] == "tie")
            print(f"    (judge {jslug:18} raw: W{w} T{t} L{lo})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--score-only", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--bedrock-workers", type=int, default=4)
    ap.add_argument(
        "--judge",
        default="panel",
        choices=["panel", "gpt-4o"],
        help="panel=§8 대체 2인 패널(기본) / gpt-4o=원 프로토콜 검증 판정자(§10 — quota 복구 시)",
    )
    ap.add_argument(
        "--frontier",
        default=",".join(FRONTIER),
        help="comma frontier slugs to battle (default: original sonnet battles)",
    )
    ap.add_argument(
        "--out-suffix",
        default="",
        help="suffix for the verdict file name (avoid clobbering prior runs)",
    )
    a = ap.parse_args()
    global _OUT_SUFFIX
    FRONTIER[:] = [s.strip() for s in a.frontier.split(",") if s.strip()]
    _OUT_SUFFIX = a.out_suffix
    if not a.score_only:
        run_battles(a.workers, a.bedrock_workers, a.judge)
    score(a.judge)


if __name__ == "__main__":
    main()
