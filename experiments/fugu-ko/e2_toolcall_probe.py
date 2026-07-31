"""E2 §4.4-1 네이티브 툴콜 지원 프로브 (D0 방식 타임박스).

질문: 국내 3종이 OpenAI-호환 `tools`/`tool_choice`를 실제로 지원하는가?
(orthus의 inline agentic `bedrock` 엔진이 native tool-use 루프를 쓴다 — 국내 모델이 그
두뇌가 될 수 있는지는 아무도 안 잰 축.)

`WorkerChat.complete()`는 tools를 못 실어서 여기서는 `_post_json`을 직접 친다(같은 retry/backoff).
판정: 응답에 `tool_calls`가 오면 NATIVE, 400/422면 UNSUPPORTED, 200인데 tool_calls 없이
본문만 오면 IGNORED(파라미터를 조용히 무시 — 가장 위험한 실패 모드).

실행: PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_toolcall_probe.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from pool import SPECS, _load_keys  # noqa: E402
from orthus.models.adapters.openai_compat import _post_json  # noqa: E402

RAW = HERE / "analysis" / "raw"

# orthus 지식 루프를 본뜬 tool 3종 (E2 §4.4-3 시나리오와 동일 계약)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "wiki_search",
            "description": "회사 위키에서 질문과 관련된 페이지를 검색한다.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "검색어"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "위키 페이지 슬러그로 본문을 읽는다.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string", "description": "페이지 슬러그"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_rows",
            "description": "Notion DB에서 행 개수를 집계한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "db_name": {"type": "string", "description": "DB 이름"},
                    "group_by": {"type": "string", "description": "그룹핑할 속성(없으면 전체 개수)"},
                },
                "required": ["db_name"],
            },
        },
    },
]

PROBE_SYSTEM = "너는 회사 지식 어시스턴트다. 필요한 도구를 호출해 답하라."
PROBE_USER = "협업업무표에 완료 상태인 티켓이 몇 개인지 알려줘."


def probe(spec, key: str, model: str) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROBE_SYSTEM},
            {"role": "user", "content": PROBE_USER},
        ],
        "temperature": 0,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    body.update(spec.extra_body)
    t0 = time.monotonic()
    try:
        data = _post_json(spec.base_url, "/chat/completions", key, body, spec.timeout)
    except Exception as e:  # noqa: BLE001
        return {
            "worker": spec.slug, "verdict": "UNSUPPORTED",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
            "ms": int((time.monotonic() - t0) * 1000),
        }

    msg = data["choices"][0].get("message", {}) or {}
    calls = msg.get("tool_calls") or []
    ms = int((time.monotonic() - t0) * 1000)
    if calls:
        fn = calls[0].get("function", {}) or {}
        args_raw = fn.get("arguments")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            args_ok = isinstance(args, dict)
        except Exception:  # noqa: BLE001
            args, args_ok = args_raw, False
        return {
            "worker": spec.slug, "verdict": "NATIVE", "n_calls": len(calls),
            "tool": fn.get("name"), "args": args, "args_valid_json": args_ok,
            "finish": data["choices"][0].get("finish_reason"), "ms": ms,
        }
    return {
        "worker": spec.slug, "verdict": "IGNORED",
        "content": (msg.get("content") or "")[:200],
        "finish": data["choices"][0].get("finish_reason"), "ms": ms,
    }


def main() -> None:
    entries = _load_keys()
    RAW.mkdir(parents=True, exist_ok=True)
    rows = []
    print("== E2 네이티브 툴콜 프로브 (tools + tool_choice=auto) ==")
    print(f"   시나리오: {PROBE_USER}\n")
    for spec in SPECS:
        entry = next(
            (e for e in entries if spec.provider_match in str(e.get("provider", "")).lower()), None
        )
        if entry is None:
            continue
        model = spec.model or str(entry.get("model", "")).split()[0]
        r = probe(spec, entry["key"], model)
        rows.append(r)
        if r["verdict"] == "NATIVE":
            print(f"  {spec.slug:8} NATIVE      tool={r['tool']} args={r['args']} ({r['ms']}ms)")
        elif r["verdict"] == "IGNORED":
            print(f"  {spec.slug:8} IGNORED     본문만 반환 ({r['ms']}ms): {r['content'][:70]}")
        else:
            print(f"  {spec.slug:8} UNSUPPORTED {r['error'][:80]}")

    out = RAW / "e2_toolcall_probe.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    native = [r["worker"] for r in rows if r["verdict"] == "NATIVE"]
    print(f"\n  네이티브 지원: {native or '없음'} → 폴백(JSON-ReAct) 필요: "
          f"{[r['worker'] for r in rows if r['verdict'] != 'NATIVE'] or '없음'}")
    print(f"  원자료: {out}")


if __name__ == "__main__":
    main()
