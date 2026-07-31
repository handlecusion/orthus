"""E2 §4.4 — 3-스텝 tool-use 루프 실측 (국내 3종 × 10 시나리오).

프로브(e2_toolcall_probe.py) 결과로 경로가 갈린다:
  solar / exaone : NATIVE   — OpenAI-호환 `tools` + `tool_calls` 응답
  ax             : IGNORED  — 200인데 tool_calls 없이 산문 (400도 아니라 **조용히 무시**)
                              → JSON-ReAct 폴백(3종 모두 json_only 5/5 PASS라 실현 가능)

시나리오: orthus 지식 루프를 본뜬 3-스텝 (wiki_search → read_page → aggregate_rows).
tool 실행은 **결정론 모의 환경**이 처리한다(모델의 tool 선택/인자만 재는 게 목적 — 실DB 접속
변동이 채점에 섞이지 않게).

지표(§4.4): 스텝별 (올바른 tool 선택 / 인자 유효성 / 환각 tool 이름 비율 / 루프 완주율).
판정선(§5): 최소 1모델 스텝 정확도 ≥ 80% + 환각 tool 이름 ≤ 5%.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from e2_toolcall_probe import TOOLS  # noqa: E402
from pool import SPECS, _load_keys  # noqa: E402
from orthus.models.adapters.openai_compat import _post_json  # noqa: E402

RAW = HERE / "analysis" / "raw"
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# ── 결정론 모의 환경 ─────────────────────────────────────────────────────────
# 실DB 값 기반(실측 2026-07-13) — 모의지만 값은 진짜다.
_PAGES = {
    "nova-overview": "NOVA는 스토리보드와 컷 단위 컨텍스트, 단계별 렌더링을 결합한 영상 제작 운영체제다.",
    "atlas-overview": "atlas은 가온엔터 보조출연 플랫폼 관련 프로젝트로 격주 금요일 배포한다.",
    "deploy-notice": "배포 공지는 배포 일정과 담당자를 담는다.",
}
_ROWS = {
    ("협업업무표", None): 794, ("협업업무표", "상태"): {"완료": 771, "보류": 16, "시작 전": 7},
    ("Nova 개발 로드맵", None): 279,
    ("Nova 개발 로드맵", "상태"): {"Done": 209, "Not Started": 27, "Review": 22, "In Progress": 15},
    ("🎯 NOVA 영입 후보 리스트", None): 19,
    ("AI관련툴", None): 49,
    ("회사 미팅 기록", None): 28,
    ("배포 공지", None): 48,
}


def exec_tool(name: str, args: dict) -> str:
    """모의 실행. 알 수 없는 tool/인자는 에러 문자열(모델이 복구하는지도 관찰)."""
    if name == "wiki_search":
        q = str(args.get("query", "")).lower()
        hits = [s for s in _PAGES if any(w in s for w in q.split()) or q[:4] in s]
        hits = hits or list(_PAGES)[:2]
        return json.dumps({"results": [{"slug": s} for s in hits[:3]]}, ensure_ascii=False)
    if name == "read_page":
        slug = str(args.get("slug", ""))
        return json.dumps({"slug": slug, "body": _PAGES.get(slug, "(페이지 없음)")}, ensure_ascii=False)
    if name == "aggregate_rows":
        db = str(args.get("db_name", ""))
        gb = args.get("group_by") or None
        val = _ROWS.get((db, gb))
        if val is None:
            val = _ROWS.get((db, None))
            if val is None:
                return json.dumps({"error": f"unknown db_name: {db}"}, ensure_ascii=False)
        return json.dumps({"db_name": db, "group_by": gb, "result": val}, ensure_ascii=False)
    return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)


# ── 시나리오 10건 — 각 3스텝 (기대 tool 시퀀스 + 기대 인자 핵심 키) ───────────
SCEN: list[dict] = [
    {"id": "TL-01", "q": "협업업무표에 완료 상태인 티켓이 몇 개인지 알려줘.",
     "steps": ["aggregate_rows"], "expect_args": {"db_name": "협업업무표"}, "gold": 771},
    {"id": "TL-02", "q": "nova가 어떤 서비스인지 위키에서 찾아서 설명해줘.",
     "steps": ["wiki_search", "read_page"], "expect_args": {}, "gold_kw": "영상 제작 운영체제"},
    {"id": "TL-03", "q": "Nova 개발 로드맵 항목이 상태별로 몇 개인지 집계해줘.",
     "steps": ["aggregate_rows"], "expect_args": {"db_name": "Nova 개발 로드맵", "group_by": "상태"},
     "gold": 209},
    {"id": "TL-04", "q": "atlas 프로젝트를 위키에서 찾아 읽고, 무슨 프로젝트인지 알려줘.",
     "steps": ["wiki_search", "read_page"], "expect_args": {}, "gold_kw": "가온엔터"},
    {"id": "TL-05", "q": "NOVA 영입 후보 리스트에 총 몇 명 있어?",
     "steps": ["aggregate_rows"], "expect_args": {"db_name": "🎯 NOVA 영입 후보 리스트"}, "gold": 19},
    {"id": "TL-06", "q": "AI관련툴이 총 몇 개인지 세줘.",
     "steps": ["aggregate_rows"], "expect_args": {"db_name": "AI관련툴"}, "gold": 49},
    {"id": "TL-07", "q": "nova를 위키에서 검색해 읽고, 협업업무표 완료 티켓 수도 함께 알려줘.",
     "steps": ["wiki_search", "read_page", "aggregate_rows"], "expect_args": {}, "gold": 771},
    {"id": "TL-08", "q": "회사 미팅 기록이 몇 건인지 집계하고, atlas 위키도 찾아서 요약해줘.",
     "steps": ["aggregate_rows", "wiki_search", "read_page"], "expect_args": {}, "gold": 28},
    {"id": "TL-09", "q": "배포 공지가 몇 건이야?",
     "steps": ["aggregate_rows"], "expect_args": {"db_name": "배포 공지"}, "gold": 48},
    {"id": "TL-10", "q": "협업업무표를 상태별로 집계하고, 배포 공지 건수도 알려줘.",
     "steps": ["aggregate_rows", "aggregate_rows"], "expect_args": {}, "gold": 48},
]

SYSTEM_NATIVE = (
    "너는 회사 지식 어시스턴트다. 주어진 도구를 호출해 사실을 확인한 뒤 최종 답을 한국어로 말하라. "
    "도구 결과에 없는 사실은 지어내지 마라."
)

SYSTEM_REACT = (
    "너는 회사 지식 어시스턴트다. 아래 도구만 쓸 수 있다.\n"
    + json.dumps([t["function"] for t in TOOLS], ensure_ascii=False)
    + "\n\n매 턴 JSON 객체 하나만 출력하라. 도구를 쓰려면 "
    '{"tool": "<이름>", "args": {...}} 를, 최종 답을 낼 준비가 되면 '
    '{"final": "<한국어 답>"} 를 출력하라. 다른 텍스트는 절대 출력하지 마라.'
)

MAX_TURNS = 6


def _key_for(spec):
    entries = _load_keys()
    e = next((x for x in entries if spec.provider_match in str(x.get("provider", "")).lower()), None)
    return e["key"], (spec.model or str(e.get("model", "")).split()[0])


def run_native(spec, key, model, scen) -> dict:
    msgs = [{"role": "system", "content": SYSTEM_NATIVE},
            {"role": "user", "content": scen["q"]}]
    calls, halluc, bad_args = [], 0, 0
    final = ""
    for _ in range(MAX_TURNS):
        body = {"model": model, "messages": msgs, "temperature": 0,
                "tools": TOOLS, "tool_choice": "auto"}
        body.update(spec.extra_body)
        data = _post_json(spec.base_url, "/chat/completions", key, body, spec.timeout)
        msg = data["choices"][0].get("message", {}) or {}
        tcs = msg.get("tool_calls") or []
        if not tcs:
            final = msg.get("content") or ""
            break
        msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tcs})
        for tc in tcs:
            fn = tc.get("function", {}) or {}
            name = fn.get("name") or ""
            raw = fn.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if not isinstance(args, dict):
                    raise ValueError
            except Exception:  # noqa: BLE001
                args, bad_args = {}, bad_args + 1
            if name not in TOOL_NAMES:
                halluc += 1
            calls.append({"tool": name, "args": args})
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                         "content": exec_tool(name, args)})
    return {"calls": calls, "halluc": halluc, "bad_args": bad_args, "final": final,
            "completed": bool(final)}


def run_react(spec, key, model, scen) -> dict:
    """JSON-ReAct 폴백 — tools 파라미터를 무시하는 모델(A.X)용."""
    transcript = f"질문: {scen['q']}"
    calls, halluc, bad_args = [], 0, 0
    final = ""
    for _ in range(MAX_TURNS):
        body = {"model": model, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM_REACT},
                             {"role": "user", "content": transcript}],
                "response_format": {"type": "json_object"}}
        body.update(spec.extra_body)
        data = _post_json(spec.base_url, "/chat/completions", key, body, spec.timeout)
        content = (data["choices"][0].get("message", {}) or {}).get("content") or ""
        try:
            step = json.loads(content)
        except Exception:  # noqa: BLE001
            bad_args += 1
            break
        if "final" in step:
            final = str(step["final"])
            break
        name = str(step.get("tool", ""))
        args = step.get("args") or {}
        if not isinstance(args, dict):
            args, bad_args = {}, bad_args + 1
        if name not in TOOL_NAMES:
            halluc += 1
        calls.append({"tool": name, "args": args})
        obs = exec_tool(name, args)
        transcript += f"\n도구 {name}({json.dumps(args, ensure_ascii=False)}) → {obs}"
    return {"calls": calls, "halluc": halluc, "bad_args": bad_args, "final": final,
            "completed": bool(final)}


def score(scen, res) -> dict:
    used = [c["tool"] for c in res["calls"]]
    want = scen["steps"]
    # 스텝 정확도: 기대 tool 시퀀스가 (순서 무관, 중복 허용) 실제 호출에 포함됐는가
    step_ok = sum(1 for w in want if w in used)
    gold_ok = False
    if "gold" in scen:
        gold_ok = str(scen["gold"]) in (res["final"] or "")
    elif "gold_kw" in scen:
        gold_ok = scen["gold_kw"] in (res["final"] or "")
    return {"steps_hit": step_ok, "steps_total": len(want),
            "answer_ok": gold_ok, "completed": res["completed"],
            "halluc": res["halluc"], "bad_args": res["bad_args"], "n_calls": len(used),
            "used": used}


MODE = {"solar": "native", "exaone": "native", "ax": "react"}


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    print("== E2 3-스텝 tool-use 루프 (10 시나리오 × 3 모델) ==")
    print("   solar/exaone=NATIVE tools · ax=JSON-ReAct 폴백(네이티브 무시 확인됨)\n")
    summary = {}
    for spec in SPECS:
        key, model = _key_for(spec)
        mode = MODE[spec.slug]
        rows = []
        for scen in SCEN:
            t0 = time.monotonic()
            try:
                res = (run_native if mode == "native" else run_react)(spec, key, model, scen)
            except Exception as e:  # noqa: BLE001
                res = {"calls": [], "halluc": 0, "bad_args": 0, "final": "",
                       "completed": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}
            s = score(scen, res)
            s.update({"id": scen["id"], "worker": spec.slug, "mode": mode,
                      "ms": int((time.monotonic() - t0) * 1000),
                      "final": (res.get("final") or "")[:200],
                      "error": res.get("error")})
            rows.append(s)
            mark = "O" if s["answer_ok"] else "X"
            print(f"  {mark} {scen['id']} {spec.slug:7} {mode:6} "
                  f"step {s['steps_hit']}/{s['steps_total']} "
                  f"calls={s['n_calls']} halluc={s['halluc']} {s['ms']:>6}ms")

        hit = sum(r["steps_hit"] for r in rows)
        tot = sum(r["steps_total"] for r in rows)
        ans = sum(1 for r in rows if r["answer_ok"])
        comp = sum(1 for r in rows if r["completed"])
        hal = sum(r["halluc"] for r in rows)
        ncalls = sum(r["n_calls"] for r in rows)
        summary[spec.slug] = {
            "mode": mode, "step_acc": hit / tot * 100 if tot else 0,
            "answer_acc": ans / len(rows) * 100, "complete_rate": comp / len(rows) * 100,
            "halluc_rate": hal / ncalls * 100 if ncalls else 0.0, "n_calls": ncalls,
        }
        print(f"  → {spec.slug}: 스텝 {hit}/{tot} ({hit/tot*100:.0f}%) · "
              f"정답 {ans}/{len(rows)} · 완주 {comp}/{len(rows)} · "
              f"환각 {hal}/{ncalls}\n")

        with open(RAW / f"e2_toolloop_{spec.slug}.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("== 판정 (§5: 스텝 정확도 ≥80% + 환각 tool ≤5%) ==")
    for slug, s in summary.items():
        ok = s["step_acc"] >= 80 and s["halluc_rate"] <= 5
        print(f"  {slug:8} {s['mode']:6} 스텝 {s['step_acc']:5.1f}% · 정답 {s['answer_acc']:5.1f}% · "
              f"완주 {s['complete_rate']:5.1f}% · 환각 {s['halluc_rate']:4.1f}%  "
              f"{'PASS' if ok else 'FAIL'}")
    passing = [k for k, s in summary.items() if s["step_acc"] >= 80 and s["halluc_rate"] <= 5]
    print(f"\n  H-E2c: {'지지 — ' + str(passing) if passing else '기각 (≥80% 스텝 정확도 모델 없음)'}")
    (RAW / "e2_toolloop_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
