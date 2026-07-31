"""WB — WorkBench 한국어 슬라이스 실행 러너 (prereg: analysis/wb-prereg.md §3).

WorkBench 하네스(generate_results, structured_outputs=True)를 무수정 재사용하되,
모델 플러그만 주입한다:
- solar / exaone : OpenAI-compat native FC. MODEL_REGISTRY + provider 맵에 등록.
  exaone은 chat_template_kwargs.enable_thinking=false(B2 규칙)를 extra_body로 전달.
- claude-sonnet-4-6 : Bedrock Converse. `_call_llm_structured`를 provider-aware
  래퍼로 patch — OpenAI 메시지 포맷을 Converse toolUse/toolResult로 변환(FULL
  prefix `us.anthropic.claude-sonnet-4-6`, m7_run.py 변환 패턴 재사용).
- gpt-5.3 : 기존 registry 항목(temperature 미전송). quota preflight 실패 시 skip.

키는 repo-root .env를 in-process 로드만 한다(.env 무수정, 로깅 금지). 실행
설정은 2026 Revisited 공개 런과 동일: structured_outputs=True, tool_selection=all,
act_without_confirmation=True.

실행 (cwd 무관, 내부에서 workbench clone으로 chdir):
  external/.cache/workbench/.venv/bin/python external/wb_run_ko.py \
      --models solar,exaone,sonnet --files email_ko,calendar_ko --canary
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import pandas as pd
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
WB = HERE / ".cache" / "workbench"
REPO_ROOT = HERE.parent.parent.parent
TAO = WB / "data" / "processed" / "tasks_and_outcomes"

load_dotenv(REPO_ROOT / ".env")
os.chdir(WB)
sys.path.insert(0, str(WB))

from src.evals import agent  # noqa: E402
from src.evals.agent import ModelConfig  # noqa: E402
from src.evals.inference import generate_results  # noqa: E402

# --------------------------------------------------------------------------- #
# 모델 등록 (in-process만 — clone 소스 무수정)
# --------------------------------------------------------------------------- #
agent.MODEL_REGISTRY["solar"] = ModelConfig(
    "solar/" + os.environ.get("ORTHUS_LLM_SOLAR_MODEL", "solar-pro"), True, "solar"
)
agent.MODEL_REGISTRY["exaone"] = ModelConfig(
    "exaone/" + os.environ.get("ORTHUS_LLM_EXAONE_MODEL", ""), True, "exaone"
)
agent.MODEL_REGISTRY["claude-sonnet-4-6-bedrock"] = ModelConfig(
    "bedrock/us.anthropic.claude-sonnet-4-6", False, "bedrock"
)
agent.MODEL_REGISTRY["claude-opus-4-5-bedrock"] = ModelConfig(
    "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0", False, "bedrock"
)
# Opus 4.6 runtime ID는 date/`:0` 없는 `-v1` 형식 — `:0`을 붙이면 400 (2026-07-23 실측)
agent.MODEL_REGISTRY["claude-opus-4-6-bedrock"] = ModelConfig(
    "bedrock/us.anthropic.claude-opus-4-6-v1", False, "bedrock"
)
# 이 키로는 `gpt-5.3`이 404 — m7_run.py와 동일하게 chat-latest 별칭 사용(temperature 미전송)
agent.MODEL_REGISTRY["gpt-5.3-chat"] = ModelConfig("openai/gpt-5.3-chat-latest", False, "openai")
agent._PROVIDER_BASE_URLS.update(
    {
        "solar": os.environ.get("ORTHUS_LLM_SOLAR_BASE_URL", "https://api.upstage.ai/v1"),
        "exaone": os.environ.get(
            "ORTHUS_LLM_EXAONE_BASE_URL", "https://api.friendli.ai/dedicated/v1"
        ),
        "bedrock": "https://bedrock-runtime."
        + os.environ.get("ORTHUS_LLM_BEDROCK_REGION", "us-east-1")
        + ".amazonaws.com",
    }
)
agent._PROVIDER_API_KEY_ENV.update(
    {
        "solar": "ORTHUS_LLM_SOLAR_API_KEY",
        "exaone": "ORTHUS_LLM_EXAONE_API_KEY",
        "bedrock": "ORTHUS_LLM_BEDROCK_API_KEY",
    }
)

_EXTRA_BODY = {
    "exaone": {
        "chat_template_kwargs": {"enable_thinking": False},
        "parse_reasoning": False,
    }
}

MODEL_SLUGS = {
    "solar": "solar",
    "exaone": "exaone",
    "sonnet": "claude-sonnet-4-6-bedrock",
    "opus": "claude-opus-4-5-bedrock",
    "opus-4-6": "claude-opus-4-6-bedrock",
    "gpt-5.3": "gpt-5.3-chat",
}

# --------------------------------------------------------------------------- #
# LLM 호출 계수 + provider-aware structured 호출 patch
# --------------------------------------------------------------------------- #
_call_counts: dict[str, int] = {}
_count_lock = threading.Lock()


def _bump(provider: str) -> None:
    with _count_lock:
        _call_counts[provider] = _call_counts.get(provider, 0) + 1


def _openai_msgs_to_converse(messages: list[dict]) -> list[dict]:
    """OpenAI chat 포맷 → Bedrock Converse 포맷 (m7_run.py 패턴)."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": [{"text": str(m.get("content") or "")}]})
        elif role == "assistant":
            content: list[dict] = []
            if m.get("content"):
                content.append({"text": str(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                args = fn.get("arguments") or "{}"
                content.append(
                    {
                        "toolUse": {
                            "toolUseId": tc["id"],
                            "name": fn["name"],
                            "input": json.loads(args) if isinstance(args, str) else args,
                        }
                    }
                )
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            block = {
                "toolResult": {
                    "toolUseId": m["tool_call_id"],
                    "content": [{"text": str(m.get("content") or "")}],
                    "status": "success",
                }
            }
            if out and out[-1]["role"] == "user" and "toolResult" in out[-1]["content"][-1]:
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def _bedrock_post(model_id: str, api_key: str, body: dict, timeout: float = 90.0) -> dict:
    base = agent._PROVIDER_BASE_URLS["bedrock"]
    url = f"{base}/model/{quote(model_id, safe=':.')}/converse"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout) as c:
                resp = c.post(url, headers=headers, json=body)
            if resp.status_code in (429,) or resp.status_code >= 500:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"bedrock converse failed: {last}")


def _call_bedrock_structured(
    route: agent.Route, system_prompt: str, messages: list[dict], tools_schema: list[dict]
) -> dict:
    body = {
        "messages": _openai_msgs_to_converse(messages),
        "inferenceConfig": {"maxTokens": 4096, "temperature": 0},
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["function"]["name"],
                        "description": t["function"].get("description", ""),
                        "inputSchema": {"json": t["function"]["parameters"]},
                    }
                }
                for t in tools_schema
            ],
            "toolChoice": {"auto": {}},
        },
    }
    if system_prompt.strip():
        body["system"] = [{"text": system_prompt}]
    data = _bedrock_post(route.model_id, route.api_key, body)
    message = (data.get("output") or {}).get("message") or {}
    content = message.get("content") or []
    text = "\n".join(b["text"] for b in content if isinstance(b, dict) and "text" in b).strip()
    tool_calls = [
        {
            "id": b["toolUse"].get("toolUseId") or f"tu_{i}",
            "name": b["toolUse"].get("name", ""),
            "arguments": b["toolUse"].get("input") or {},
        }
        for i, b in enumerate(content)
        if isinstance(b, dict) and "toolUse" in b
    ]
    if data.get("stopReason") != "tool_use":
        tool_calls = []
    return {"content": text, "tool_calls": tool_calls}


_orig_call_llm_structured = agent._call_llm_structured


def _patched_call_llm_structured(
    route: agent.Route,
    system_prompt: str,
    messages: list[dict],
    tools_schema: list[dict],
    temperature: float,
) -> dict:
    _bump(route.provider)
    if route.provider == "bedrock":
        return _call_bedrock_structured(route, system_prompt, messages, tools_schema)
    extra = _EXTRA_BODY.get(route.provider)
    if extra:
        client = agent._get_openai_client(route.api_key, route.base_url)
        kwargs: dict = {
            "model": route.model_id,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "tools": tools_schema,
            "extra_body": extra,
        }
        if route.supports_temperature:
            kwargs["temperature"] = temperature
        response = agent._create_with_deadline(client, **kwargs)
        msg = response.choices[0].message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    {"id": tc.id, "name": tc.function.name, "arguments": json.loads(tc.function.arguments)}
                )
        return {"content": msg.content or "", "tool_calls": tool_calls}
    return _orig_call_llm_structured(route, system_prompt, messages, tools_schema, temperature)


agent._call_llm_structured = _patched_call_llm_structured


# --------------------------------------------------------------------------- #
def preflight(model_name: str) -> str | None:
    """모델당 1회 소형 호출. 실패 사유 문자열 반환(성공 시 None)."""
    try:
        route = agent.resolve_route(model_name)
        if route.provider == "bedrock":
            _bedrock_post(
                route.model_id,
                route.api_key,
                {
                    "messages": [{"role": "user", "content": [{"text": "ping"}]}],
                    "inferenceConfig": {"maxTokens": 16, "temperature": 0},
                },
                timeout=30.0,
            )
        else:
            client = agent._get_openai_client(route.api_key, route.base_url)
            kwargs: dict = {
                "model": route.model_id,
                "messages": [{"role": "user", "content": "ping"}],
            }
            extra = _EXTRA_BODY.get(route.provider)
            if extra:
                kwargs["extra_body"] = extra
            client.chat.completions.create(**kwargs)
        return None
    except Exception as exc:  # noqa: BLE001 — preflight은 사유 기록 후 skip
        return f"{type(exc).__name__}: {str(exc)[:300]}"


def ensure_canary_files() -> list[str]:
    out = []
    for domain, n in [("email_ko", 3), ("calendar_ko", 2)]:
        src = TAO / f"{domain}_tasks_and_outcomes.csv"
        dst = TAO / f"{domain.replace('_ko', '_kocanary')}_tasks_and_outcomes.csv"
        pd.read_csv(src, dtype=str).head(n).to_csv(dst, index=False)
        out.append(dst.stem.replace("_tasks_and_outcomes", ""))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,exaone,sonnet")
    ap.add_argument("--files", default="email_ko,calendar_ko")
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    domains = ensure_canary_files() if args.canary else args.files.split(",")

    for short in [m.strip() for m in args.models.split(",") if m.strip()]:
        model_name = MODEL_SLUGS[short]
        pf = preflight(model_name)
        if pf is not None:
            print(f"[{short}] preflight FAILED — skipping: {pf}")
            continue
        print(f"[{short}] preflight OK")
        for domain in domains:
            tasks_path = str(TAO / f"{domain}_tasks_and_outcomes.csv")
            before = dict(_call_counts)
            t0 = time.monotonic()
            generate_results(
                tasks_path,
                model_name,
                tool_selection="all",
                workers=args.workers,
                log_traces=True,
                act_without_confirmation=True,
                structured_outputs=True,
                resume=args.resume,
            )
            wall = time.monotonic() - t0
            delta = {
                k: _call_counts.get(k, 0) - before.get(k, 0)
                for k in _call_counts
                if _call_counts.get(k, 0) != before.get(k, 0)
            }
            line = {
                "model": short,
                "domain": domain,
                "wall_s": round(wall, 1),
                "llm_calls": delta,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(WB / "wb_call_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            print(f"[{short}] {domain} done: {line}")

    print(f"total llm calls by provider: {_call_counts}")


if __name__ == "__main__":
    main()
