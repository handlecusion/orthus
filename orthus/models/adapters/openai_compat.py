"""Real adapters over an OpenAI-compatible HTTP API (chat/completions, embeddings).

Upstage Solar speaks this protocol, so this single adapter backs every real
slot in the system: ORTHUS_LLM=solar (chat/compile), ORTHUS_EMBEDDING=solar
(retrieval index), and the in-process agentic tool loop (`run_tool_loop`).
Key comes from env only."""

from __future__ import annotations

import json
import random
import time

import httpx

# Bulk authoring (wiki rebuild) makes many calls in bursts; a full company
# compile can sustain OpenAI rate limits for a while. Retry transient
# timeout/429/5xx with exponential backoff + jitter, and honor the server's
# Retry-After when present, so one rate-limited call waits out the window
# instead of aborting the whole run.
_TIMEOUT = 60.0
_RETRIES = 6  # total attempts = _RETRIES + 1
_BACKOFF_BASE = 1.5
_BACKOFF_CAP = 30.0


def _retry_after_seconds(response: httpx.Response | None, attempt: int) -> float:
    """Prefer the server's Retry-After header; else exponential backoff + jitter."""
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return min(float(raw), _BACKOFF_CAP)
            except ValueError:
                pass
    backoff = min(_BACKOFF_CAP, _BACKOFF_BASE * (2**attempt))
    return backoff + random.uniform(0, backoff * 0.25)


def _is_insufficient_quota(response: httpx.Response) -> bool:
    """OpenAI returns this 429 when billing/quota is exhausted, not rate-limited."""
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    values: list[object]
    if isinstance(error, dict):
        values = [error.get("type"), error.get("code"), error.get("message")]
    else:
        values = [error]
    return any(isinstance(value, str) and "insufficient_quota" in value.lower() for value in values)


def _post_json(
    base: str, path: str, key: str, body: dict, timeout: float, retries: int = _RETRIES
) -> dict:
    """POST JSON with retry on transient timeout / 429 / 5xx.

    429/5xx wait out Retry-After (or exponential backoff + jitter) so a
    rate-limited bulk run converges instead of crashing on the first limit.
    OpenAI's insufficient_quota 429 is permanent until billing changes, so it
    fails fast instead of burning retry sleep time.

    `retries` is tunable because the right budget depends on whether the caller
    has anywhere else to go. Bulk authoring has no fallback, so it grinds through
    the default six attempts. An orchestration worker (docs/model-orchestration.md)
    *does* have a fallback sitting right behind it, so it bails after one retry
    rather than making a live request wait out ~76s of backoff to reach a model we
    could have used immediately.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(
                    f"{base}{path}",
                    headers={"Authorization": f"Bearer {key}"},
                    json=body,
                )
            if r.status_code == 429 and _is_insufficient_quota(r):
                r.raise_for_status()
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                time.sleep(_retry_after_seconds(r, attempt))
                continue
            r.raise_for_status()
            return r.json()
        except httpx.TimeoutException as e:
            last = e
            if attempt < retries:
                time.sleep(_retry_after_seconds(None, attempt))
                continue
            raise
    raise last  # pragma: no cover  # unreachable (loop returns or raises)


class OpenAIChat:
    """OpenAI-compatible chat (Solar). Knobs kept from the measurement campaign:

    - `extra_body`: extra request fields for vendors that need them (e.g. a
      reasoning-model thinking toggle). Empty for Solar.
    - `min_interval`: spaces out calls for vendors with a req/s cap, instead of
      relying on 429 retries. 0 for Solar.
    - `temperature`: defaults to the historical `0` for deterministic compile/routing
      calls. Some models reject any value but their default and return HTTP 400 for
      `temperature=0`. Pass `temperature=None` to omit the field from the request
      body entirely and let the API use its own default.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = _TIMEOUT,
        *,
        extra_body: dict | None = None,
        min_interval: float = 0.0,
        retries: int = _RETRIES,
        temperature: float | None = 0.0,
    ):
        self.model_id = model
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._extra_body = dict(extra_body or {})
        self._min_interval = min_interval
        self._retries = retries
        self._temperature = temperature
        self._last_call = 0.0

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        body: dict = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if json_only:
            body["response_format"] = {"type": "json_object"}
        body.update(self._extra_body)
        self._throttle()
        data = _post_json(
            self._base, "/chat/completions", self._key, body, self._timeout, self._retries
        )
        content = data["choices"][0]["message"].get("content")
        if content is None:
            # Reasoning models can spend the whole budget on `reasoning_content`
            # and return a null `content`. Fail loudly so the caller can fall back.
            raise ValueError(f"{self.model_id} returned no content (reasoning-only response)")
        return content

    def run_tool_loop(
        self,
        *,
        system: str,
        question: str,
        tools: list[dict],
        dispatch,
        on_event=None,
        max_turns: int = 6,
    ) -> str:
        """Drive an OpenAI-compatible function-calling loop and return the final text.

        Same contract as the agentic /ask engine expects: `tools` is a list of
        {name, description, input_schema(JSON Schema dict)}; `dispatch(name,
        input_dict) -> str` runs one tool and returns its model-facing textual
        result; `on_event(frame)` optionally receives progress frames. Bounded by
        `max_turns` and FAIL-OPEN: any exception (HTTP, dispatch, malformed
        response) returns whatever assistant text has accumulated instead of
        raising into the request handler — the deterministic tool backends
        (validation gate, wiki grounding, KG template gate) own correctness;
        this loop is only orchestration.
        """
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ]
        last_text = ""
        try:
            for _ in range(max_turns):
                body: dict = {
                    "model": self.model_id,
                    "messages": messages,
                    "tools": tool_defs,
                    "tool_choice": "auto",
                }
                if self._temperature is not None:
                    body["temperature"] = self._temperature
                body.update(self._extra_body)
                self._throttle()
                data = _post_json(
                    self._base, "/chat/completions", self._key, body, self._timeout, self._retries
                )
                message = data["choices"][0]["message"]
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    last_text = content.strip()
                    if on_event is not None:
                        on_event({"type": "agent_output", "chunk": last_text + "\n"})

                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    return last_text
                # Echo the assistant turn (with its tool_calls) back into history
                # before appending the tool results, as the protocol requires.
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls,
                    }
                )
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    try:
                        tool_input = json.loads(fn.get("arguments") or "{}")
                        if not isinstance(tool_input, dict):
                            tool_input = {}
                    except ValueError:
                        tool_input = {}
                    try:
                        result_text = dispatch(name, tool_input)
                    except Exception as exc:  # noqa: BLE001 - surface to model, never raise
                        result_text = f"[도구 오류: {type(exc).__name__}]"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": str(result_text),
                        }
                    )
            # Hit the turn cap with tools still pending — return best text so far.
            return last_text
        except Exception:  # noqa: BLE001 - fail-open feedback loop
            return last_text


# Output dimension requested from the embeddings API. Must match the
# embeddings.vec Vector(1024) schema column. text-embedding-3-* support
# dimension reduction via the `dimensions` request param.
_EMBED_DIMS = 1024


class OpenAIEmbedding:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        dimensions: int = _EMBED_DIMS,
        timeout: float = _TIMEOUT,
    ):
        if dimensions != _EMBED_DIMS:
            raise ValueError("ORTHUS_EMBEDDING_DIMENSIONS must be 1024")
        if not api_key.strip():
            raise ValueError("ORTHUS_EMBEDDING_SOLAR_API_KEY required")
        self._model = model
        self.model_version = f"{model}:{dimensions}"
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._dimensions = dimensions
        self._timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = _post_json(
            self._base,
            "/embeddings",
            self._key,
            {"model": self._model, "input": texts, "dimensions": self._dimensions},
            self._timeout,
        )
        return [d["embedding"] for d in data["data"]]
