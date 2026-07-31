"""M7 — Agentic Loop Bench runner (prereg: analysis/m7-prereg.md).

Single harness, two transports:
- openai  : OpenAI-compatible native function calling, multi-turn (assistant
            tool_calls -> role:"tool" results fed back). solar / exaone / ax /
            gpt-5.3.
- bedrock : Converse toolUse/toolResult loop — same message format as the
            production `BedrockConverseChat.run_tool_loop`. claude-sonnet-4-6
            (FULL prefix `anthropic.claude-sonnet-4-6`).

Tool backends are the deterministic fixture env (`m7_env.py`) — no DB, no nested
LLM. Raw transcripts go to raw/m7_<slug>[.canary].jsonl; scoring is separate
(`m7_score.py`). No .env edits; keys are read via pool.py / settings and never
printed.

Usage:
  .venv/bin/python experiments/fugu-ko/m7_run.py --models solar --canary
  .venv/bin/python experiments/fugu-ko/m7_run.py --models solar,exaone,claude-sonnet-4-6,ax
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

load_dotenv(REPO_ROOT / ".env")

# 아카이브: 프론티어 저지 arm은 Bedrock 어댑터를 썼다. Solar 단일 공개 빌드에는
# 그 어댑터가 없으므로, 이 스크립트는 실행 시 명확히 안내하고 종료한다.
try:
    from orthus.models.adapters.bedrock import BedrockConverseChat, _post_converse  # noqa: E402
except ImportError:  # pragma: no cover - archival script
    raise SystemExit(
        "archival: 이 스크립트의 프론티어 저지 arm은 Bedrock 어댑터가 필요하다 — "
        "Solar 단일 공개 빌드에는 포함되지 않는다 (experiments/README.md 참조)"
    )
from orthus.models.adapters.openai_compat import _post_json  # noqa: E402
from orthus.settings import get_settings  # noqa: E402

import pool  # noqa: E402
from m7_env import TOOL_SPECS, Env, ToolFormatError, system_prompt  # noqa: E402

GOLDEN = HERE / "golden" / "m7_tasks.json"
RAW_DIR = HERE / "raw"

MAX_TOKENS = 1024  # domestic/openai default cap (B2 rule); bedrock uses prod setting
CANARY_IDS = ["R1", "W1"]
_TOOL_RESULT_CAP = 4000


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@dataclass
class Ep:
    slug: str
    family: str  # "openai" | "bedrock"
    base_url: str = ""
    key: str = ""
    model: str = ""
    extra_body: dict = field(default_factory=dict)
    send_temperature: bool = True
    send_max_tokens: bool = True
    min_interval: float = 0.0
    timeout: float = 90.0
    adapter: object = None  # bedrock only
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last: float = 0.0

    def throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


_BEDROCK_MODEL_IDS = {
    "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",  # FULL prefix (trap: short slug 404s)
    # FULL versioned id mandatory — short forms 400 (verified on the team key).
    "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    # 4.6 runtime id is `-v1` WITHOUT date and WITHOUT `:0` (appending `:0` 400s).
    # Already `us.`-prefixed — `_normalize_model_id` passes it through untouched
    # (no double `us.` prefixing).
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
}


def build_endpoint(slug: str) -> Ep:
    if slug in _BEDROCK_MODEL_IDS:
        s = get_settings()
        key = (
            s.llm_bedrock_api_key
            or s.llm_api_key
            or os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY", "")
            or os.environ.get("ORTHUS_LLM_API_KEY", "")
        ).strip()
        adapter = BedrockConverseChat(
            api_key=key,
            region=s.llm_bedrock_region or "us-east-1",
            model_id=_BEDROCK_MODEL_IDS[slug],
            inference_prefix=s.llm_bedrock_inference_prefix,
            max_tokens=s.llm_bedrock_max_tokens,
            temperature=s.llm_bedrock_temperature,
            base_url=s.llm_bedrock_base_url or None,
        )
        return Ep(slug=slug, family="bedrock", adapter=adapter, timeout=adapter._timeout)

    if slug in ("gpt-5.3", "gpt-5.5"):
        key = (os.environ.get("ORTHUS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")).strip()
        if not key:
            raise ValueError(f"{slug}: ORTHUS_LLM_API_KEY/OPENAI_API_KEY required")
        # gpt-5.5 is a reasoning model: caps go through `max_completion_tokens`
        # (not `max_tokens`) and small budgets return empty content, so send no
        # cap at all. Both gpt-5.x reject any non-default temperature.
        return Ep(
            slug=slug,
            family="openai",
            base_url="https://api.openai.com/v1",
            key=key,
            model="gpt-5.3-chat-latest" if slug == "gpt-5.3" else "gpt-5.5",
            send_temperature=False,  # vendor rejects any non-default temperature
            send_max_tokens=False,  # vendor rejects max_tokens
        )

    if slug in ("deepseek-v4-pro", "glm-5.2"):
        # Both are reasoning models: a small `max_tokens` budget is consumed by
        # reasoning tokens and returns EMPTY content (documented GLM-5.2 cost
        # incident) — send no cap at all, mirroring the gpt-5.5 rule above.
        # Both accept temperature=0 (verified via the E2E harness arms).
        if slug == "deepseek-v4-pro":
            key = os.environ.get("ORTHUS_LLM_DEEPSEEK_API_KEY", "").strip()
            base = os.environ.get("ORTHUS_LLM_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        else:
            key = os.environ.get("ORTHUS_GLM_API_KEY", "").strip()
            base = "https://api.z.ai/api/paas/v4"  # harness_e2e._GLM_BASE_URL (verified live)
        if not key:
            raise ValueError(f"{slug}: vendor API key env required (see harness_e2e.py builders)")
        return Ep(
            slug=slug,
            family="openai",
            base_url=base.rstrip("/"),
            key=key,
            model=slug,
            send_max_tokens=False,  # reasoning model: no cap (empty-content trap)
            timeout=180.0,  # heavy reasoning tails (E2E p95 ~9-12s, multi-turn headroom)
        )

    worker = pool.build_pool([slug])[slug]
    extra = dict(worker._spec.extra_body)
    if slug == "exaone":
        extra["parse_reasoning"] = False  # keep budget out of reasoning (B2 rule)
    return Ep(
        slug=slug,
        family="openai",
        base_url=worker._spec.base_url.rstrip("/"),
        key=worker._key,
        model=worker._model,
        extra_body=extra,
        min_interval=worker._spec.min_interval,
        timeout=worker._spec.timeout,
    )


def preflight(ep: Ep) -> str | None:
    try:
        if ep.family == "bedrock":
            a = ep.adapter
            body = {
                "messages": [{"role": "user", "content": [{"text": "ping — respond 'pong'"}]}],
                "inferenceConfig": {"maxTokens": 16, "temperature": a._temperature},
            }
            _post_converse(a._base, a._model_id, a._key, body, timeout=a._timeout)
        else:
            body: dict = {
                "model": ep.model,
                "messages": [{"role": "user", "content": "ping — respond 'pong'"}],
            }
            if ep.send_max_tokens:
                body["max_tokens"] = 16
            for k, v in ep.extra_body.items():
                body.setdefault(k, v)
            _post_json(ep.base_url, "/chat/completions", ep.key, body, ep.timeout)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:200]}"


# --------------------------------------------------------------------------- #
# Tool dispatch shared by both transports
# --------------------------------------------------------------------------- #
def dispatch_call(env: Env, name: str, raw_args: object) -> dict:
    """Returns {"name", "input", "result", "fmt_error": bool, "ok": bool}."""
    rec: dict = {"name": name, "input": raw_args, "fmt_error": False, "ok": False}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            rec["fmt_error"] = True
            rec["result"] = "[포맷 오류] arguments JSON 파싱 실패 — 올바른 JSON 으로 다시 호출하라."
            return rec
        rec["input"] = raw_args
    try:
        result = env.dispatch(name, raw_args if isinstance(raw_args, dict) else {})
        rec["result"] = result[:_TOOL_RESULT_CAP]
        rec["ok"] = not result.startswith("[도구 오류]")
    except ToolFormatError as exc:
        rec["fmt_error"] = True
        rec["result"] = f"[포맷 오류] {exc}"
    return rec


# --------------------------------------------------------------------------- #
# Multi-turn loops
# --------------------------------------------------------------------------- #
_OA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in TOOL_SPECS
]

_BR_TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": {"json": t["input_schema"]},
            }
        }
        for t in TOOL_SPECS
    ],
    "toolChoice": {"auto": {}},
}


def run_task_openai(ep: Ep, task: dict) -> dict:
    env = Env(rig=dict(task.get("rig") or {}))
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": task["prompt"]},
    ]
    turns: list[dict] = []
    n_llm = 0
    status = "max_turns"
    final_text = ""
    t0 = time.monotonic()
    try:
        for _ in range(int(task["max_turns"])):
            body: dict = {
                "model": ep.model,
                "messages": messages,
                "tools": _OA_TOOLS,
                "tool_choice": "auto",
            }
            if ep.send_temperature:
                body["temperature"] = 0
            if ep.send_max_tokens:
                body["max_tokens"] = MAX_TOKENS
            for k, v in ep.extra_body.items():
                body.setdefault(k, v)
            ep.throttle()
            data = _post_json(ep.base_url, "/chat/completions", ep.key, body, ep.timeout)
            n_llm += 1
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            turn_rec: dict = {"text": text, "tool_calls": []}
            if not tool_calls:
                final_text = text
                status = "final"
                turns.append(turn_rec)
                break
            norm_calls = []
            for i, tc in enumerate(tool_calls):
                fn = tc.get("function") or {}
                tc_id = tc.get("id") or f"call_{n_llm}_{i}"
                norm_calls.append(
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments")
                            if isinstance(fn.get("arguments"), str)
                            else json.dumps(fn.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                )
            messages.append({"role": "assistant", "content": text, "tool_calls": norm_calls})
            for tc in norm_calls:
                rec = dispatch_call(env, tc["function"]["name"], tc["function"]["arguments"])
                turn_rec["tool_calls"].append(rec)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(rec["result"]),
                    }
                )
            if text:
                final_text = text
            turns.append(turn_rec)
    except Exception as exc:  # noqa: BLE001 — vendor/API failure: record, don't crash the sweep
        status = "api_error"
        turns.append({"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return _pack(task, ep, status, final_text, turns, env, time.monotonic() - t0, n_llm)


def run_task_bedrock(ep: Ep, task: dict) -> dict:
    a = ep.adapter
    env = Env(rig=dict(task.get("rig") or {}))
    messages: list[dict] = [{"role": "user", "content": [{"text": task["prompt"]}]}]
    turns: list[dict] = []
    n_llm = 0
    status = "max_turns"
    final_text = ""
    t0 = time.monotonic()
    try:
        for _ in range(int(task["max_turns"])):
            body = {
                "messages": messages,
                "system": [{"text": system_prompt()}],
                "inferenceConfig": {"maxTokens": a._max_tokens, "temperature": a._temperature},
                "toolConfig": _BR_TOOL_CONFIG,
            }
            data = _post_converse(a._base, a._model_id, a._key, body, timeout=a._timeout)
            n_llm += 1
            message = (data.get("output") or {}).get("message") or {}
            content = message.get("content") or []
            messages.append({"role": "assistant", "content": content})
            text_parts = [b["text"] for b in content if isinstance(b, dict) and "text" in b]
            text = "\n".join(text_parts).strip()
            tool_uses = [b["toolUse"] for b in content if isinstance(b, dict) and "toolUse" in b]
            turn_rec: dict = {"text": text, "tool_calls": []}
            if data.get("stopReason") != "tool_use" or not tool_uses:
                final_text = text
                status = "final"
                turns.append(turn_rec)
                break
            tool_results = []
            for tu in tool_uses:
                rec = dispatch_call(env, tu.get("name", ""), tu.get("input") or {})
                turn_rec["tool_calls"].append(rec)
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tu.get("toolUseId"),
                            "content": [{"text": str(rec["result"])}],
                            "status": "success",
                        }
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            if text:
                final_text = text
            turns.append(turn_rec)
    except Exception as exc:  # noqa: BLE001
        status = "api_error"
        turns.append({"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return _pack(task, ep, status, final_text, turns, env, time.monotonic() - t0, n_llm)


def _pack(task, ep, status, final_text, turns, env, wall_s, n_llm) -> dict:  # noqa: ANN001
    all_calls = [c for t in turns for c in t.get("tool_calls", [])]
    return {
        "task_id": task["id"],
        "model": ep.slug,
        "status": status,
        "final_text": final_text,
        "n_llm_calls": n_llm,
        "n_tool_calls": len(all_calls),
        "n_format_failures": sum(1 for c in all_calls if c["fmt_error"]),
        "n_ok_tool_calls": sum(1 for c in all_calls if c["ok"]),
        "turns": turns,
        "end_state": env.end_state(),
        "wall_s": round(wall_s, 2),
    }


def run_task(ep: Ep, task: dict) -> dict:
    if ep.family == "bedrock":
        return run_task_bedrock(ep, task)
    return run_task_openai(ep, task)


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,exaone,claude-sonnet-4-6,ax")
    ap.add_argument("--canary", action="store_true", help="R1+W1 (ax: R1) only, *.canary.jsonl")
    ap.add_argument("--tasks", default="", help="comma-separated task ids (override)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    tasks = golden["tasks"]
    by_id = {t["id"]: t for t in tasks}
    probe_ids = golden["meta"]["ax_probe_task_ids"]
    RAW_DIR.mkdir(exist_ok=True)

    for slug in [m.strip() for m in args.models.split(",") if m.strip()]:
        if args.tasks:
            todo = [by_id[i] for i in args.tasks.split(",")]
        elif args.canary:
            todo = [by_id[i] for i in (["R1"] if slug == "ax" else CANARY_IDS)]
        elif slug == "ax":
            todo = [by_id[i] for i in probe_ids]  # prereg §3: 5-task probe only
        else:
            todo = list(tasks)
        try:
            ep = build_endpoint(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"[{slug}] endpoint build FAILED: {type(exc).__name__}: {exc}")
            continue
        pf = preflight(ep)
        if pf is not None:
            print(f"[{slug}] preflight FAILED — skipping model: {pf}")
            out = RAW_DIR / f"m7_{slug}.preflight_failed.txt"
            out.write_text(pf + "\n", encoding="utf-8")
            continue
        print(f"[{slug}] preflight OK — running {len(todo)} tasks")
        workers = 1 if slug == "ax" else min(args.workers, len(todo))
        results: dict[str, dict] = {}
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(run_task, ep, t): t["id"] for t in todo}
            for fut in as_completed(futs):
                tid = futs[fut]
                results[tid] = fut.result()
                r = results[tid]
                print(
                    f"  [{slug}] {tid}: status={r['status']} llm={r['n_llm_calls']} "
                    f"tools={r['n_tool_calls']} fmt_fail={r['n_format_failures']} "
                    f"{r['wall_s']}s"
                )
        suffix = ".canary" if args.canary else ""
        out = RAW_DIR / f"m7_{slug}{suffix}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for t in todo:
                f.write(json.dumps(results[t["id"]], ensure_ascii=False) + "\n")
        print(f"[{slug}] done in {time.monotonic() - t0:.0f}s -> {out}")


if __name__ == "__main__":
    main()
