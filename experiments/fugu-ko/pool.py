"""WorkerPool: 국내 3종 워커를 orthus ChatModel Protocol로 감싼다.

D0(2026-07-08) 실측 스펙 고정(`analysis/d0-findings.md`):
- solar  : api.upstage.ai/v1, model solar-pro (handoff의 v2는 오기)
- ax     : awf-gw.adot.ai/v1, model A.X-K1, RPS 3 하드캡
- exaone : friendli dedicated, model=엔드포인트ID(접미사 제거),
           chat_template_kwargs.enable_thinking=false 필수(reasoning 모델), ~36s/call

ChatModel Protocol(`orthus/models/base.py`)과 동일한 `.model_id` + `.complete(system, user,
*, json_only)`를 노출하므로 orthus의 distill/qa/structured/route 함수에 `chat_model=` 로
그대로 주입된다(registry.py 무수정).

orthus의 `_post_json`(retry/backoff/Retry-After)을 재사용한다. 키는 keys.json 또는 repo-root
`.env`(`orthus.settings.get_settings()` 경유, `ORTHUS_LLM_SOLAR_API_KEY` 등 production과 동일 env
var)에서 읽고 어디에도 로깅하지 않는다. keys.json에 실키가 있으면 그게 우선이고(기존 사용자
무변경), 없거나 플레이스홀더면 `.env` 값으로 폴백한다 — 아래 `_vendor_env_fallback()` 참고.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

from orthus.models.adapters.openai_compat import _post_json  # 재사용: retry/backoff

KEYS_PATH = os.environ.get(
    "FUGU_KEYS",
    "<로컬 키 저장소>/keys.json",
)


@dataclass
class WorkerSpec:
    slug: str
    provider_match: str           # keys.json provider 부분매칭
    base_url: str                 # /chat/completions 앞까지
    model: str | None = None      # None이면 keys.json model 첫 토큰
    extra_body: dict = field(default_factory=dict)
    timeout: float = 60.0
    min_interval: float = 0.0     # 호출 간 최소 간격(초) — RPS 하드캡


# D0 확정 스펙. 새 워커 추가는 여기 한 줄.
SPECS: list[WorkerSpec] = [
    WorkerSpec("solar", "upstage", "https://api.upstage.ai/v1", "solar-pro", timeout=40),
    WorkerSpec("ax", "a.x", "https://awf-gw.adot.ai/v1", "A.X-K1", timeout=40, min_interval=0.4),
    WorkerSpec(
        "exaone",
        "exaone",
        "https://api.friendli.ai/dedicated/v1",
        model=None,  # keys.json: "depmkuykpfon9lg (Endpoint ID)" → 첫 토큰
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        timeout=120,
    ),
]


class WorkerChat:
    """orthus ChatModel Protocol 호환. 워커 1종 = 인스턴스 1개.

    E5 계측: 응답 `usage`를 `usage_events`에 **누적**한다(단일 last_usage 필드는 항목당 2콜
    — json 재시도, compile 2-pass 등 — 이 되는 순간 조용히 과소계상한다). 하네스가 항목 전
    `reset_usage()` → 항목 후 `usage_totals()`로 합산한다. 벤더가 usage를 안 주면 콜은 세되
    토큰은 None으로 남겨(`missing=True`) "콜 0회(fast-path)"와 "콜은 했는데 usage 결측"을
    구분한다 — §5 완료 정의가 후자만 결측으로 센다.
    """

    def __init__(self, spec: WorkerSpec, key: str, model: str):
        self._spec = spec
        self._key = key
        self.model_id = f"{spec.slug}:{model}"  # orthus Protocol이 요구하는 필드
        self._model = model
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.usage_events: list[dict] = []

    def reset_usage(self) -> None:
        self.usage_events = []

    def usage_totals(self) -> dict:
        """항목 단위 집계: 도달한 LLM 콜 수 + 토큰 합(결측 콜 수 별도)."""
        ok = [e for e in self.usage_events if not e["missing"]]
        return {
            "n_llm_calls": len(self.usage_events),
            "in_tok": sum(e["in"] for e in ok) if ok else 0,
            "out_tok": sum(e["out"] for e in ok) if ok else 0,
            "n_usage_missing": sum(1 for e in self.usage_events if e["missing"]),
        }

    def _record_usage(self, data: dict) -> None:
        u = data.get("usage") or {}
        in_tok, out_tok = u.get("prompt_tokens"), u.get("completion_tokens")
        if in_tok is None or out_tok is None:
            self.usage_events.append({"in": None, "out": None, "missing": True})
        else:
            self.usage_events.append({"in": int(in_tok), "out": int(out_tok), "missing": False})

    def _throttle(self) -> None:
        if self._spec.min_interval <= 0:
            return
        with self._lock:
            wait = self._spec.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        body: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        if json_only:
            body["response_format"] = {"type": "json_object"}
        body.update(self._spec.extra_body)
        self._throttle()
        data = _post_json(self._spec.base_url, "/chat/completions", self._key, body, self._spec.timeout)
        self._record_usage(data)  # E5: 응답 usage 누적(콘텐츠 검증 전 — 실패 콜도 과금된다)
        content = data["choices"][0]["message"]["content"]
        if content is None:
            # reasoning 모델이 content 없이 reasoning만 낼 때(EXAONE thinking 미차단 등) 방어
            raise RuntimeError(f"{self.model_id}: null content (finish={data['choices'][0].get('finish_reason')})")
        return content


# keys.json 값이 이 마커를 포함하면 "실키 없음" 취급하고 .env 폴백으로 넘어간다
# (빈 문자열은 이미 falsy라 별도 처리 불필요).
_PLACEHOLDER_KEY_MARKERS = (
    "your_key", "your-key", "placeholder", "changeme", "change_me",
    "replace_me", "replace-me", "<key>", "todo", "xxxxx",
)


def _is_real_key(value: object) -> bool:
    v = str(value or "").strip()
    if not v:
        return False
    low = v.lower()
    return not any(marker in low for marker in _PLACEHOLDER_KEY_MARKERS)


def _load_keys() -> list[dict]:
    """keys.json은 이제 선택 사항이다 — 없으면 빈 리스트(모든 슬롯이 `.env` 폴백으로 감).
    존재하는데 JSON이 깨졌으면(오타 등) 그대로 raise해 조용히 무시하지 않는다.
    """
    if not os.path.exists(KEYS_PATH):
        return []
    return json.load(open(KEYS_PATH, encoding="utf-8"))


def _vendor_env_fallback(slug: str) -> WorkerChat | None:
    """keys.json에 slug의 실키가 없을 때 repo-root `.env`로 폴백한다.

    `orthus.models.registry.vendor_specs()`가 `orthus.settings.get_settings()`(pydantic
    `env_file=".env"`, repo root에서 실행할 때 자동 로드)를 읽으므로, production과 완전히
    같은 env var(`ORTHUS_LLM_SOLAR_API_KEY`/`_BASE_URL`/`_MODEL` 등, solar/ax/exaone 전부)를
    `.env`에 넣기만 하면 여기서 그대로 읽힌다. `build_vendor_chat(..., retries=0)`은
    **네트워크 호출 없이** production이 실제로 쓰는 "이 벤더가 설정됐는가"(키/모델 id
    non-empty) 판정만 재사용하기 위해 호출한다 — 그 리턴값(OpenAIChat)은 버린다. OpenAIChat은
    usage를 노출하지 않으므로(harness_e2e.py `_ProdAdapterUsageWrapper`가 그 경우 항상
    missing=True로 기록하는 이유와 동일), 대신 같은 VendorSpec 필드로 `WorkerSpec`을 만들어
    keys.json 경로와 동일한 `WorkerChat`을 새로 구성한다 — 이쪽이 실제 in/out 토큰까지
    누적하므로 usage 정밀도가 더 높다(의도적 선택이지 누락이 아님).
    """
    from orthus.models.registry import build_vendor_chat, vendor_specs

    vspec = vendor_specs()[slug]
    if build_vendor_chat(vspec, retries=0) is None:
        return None
    worker_spec = WorkerSpec(
        slug,
        provider_match="",
        base_url=vspec.base_url,
        model=vspec.model,
        extra_body=vspec.extra_body,
        timeout=vspec.timeout,
        min_interval=vspec.min_interval,
    )
    return WorkerChat(worker_spec, vspec.api_key, vspec.model)


def _baseline_chat() -> WorkerChat:
    """현행 prod 모델(env `ORTHUS_LLM_*`)을 **WorkerChat으로** 감싼다 — E5 계측 경로 통일.

    프로덕션 `OpenAIChat`은 usage를 노출하지 않아 기준선 원가를 낼 수 없었다. 두 어댑터의 요청
    body는 완전히 동일하고(messages 구조 · temperature=0 · json_only→response_format · 같은
    `_post_json`) OpenAI-호환 API도 같으므로, WorkerChat으로 교체해도 baseline의 행동은 불변이다
    (`OpenAIChat` default에 맞춰 timeout=60, min_interval=0). 프로덕션 코드 diff는 0줄.
    """
    from orthus.settings import get_settings

    s = get_settings()
    if s.llm != "openai":
        # mock/bedrock/cli는 이 OpenAI-호환 계측 경로로 원가를 낼 수 없다 — 조용히 다른 모델을
        # 기준선으로 재는 것보다 실패가 낫다(원가 표가 통째로 무효가 되므로).
        raise RuntimeError(f"baseline 계측은 ORTHUS_LLM=openai 전용 (현재: {s.llm}). node.env 로드 확인.")
    if not s.llm_api_key.strip():
        raise RuntimeError("ORTHUS_LLM_API_KEY 없음 — company node.env를 로드했는지 확인.")
    spec = WorkerSpec("baseline", provider_match="", base_url=s.llm_base_url, model=s.llm_model, timeout=60)
    return WorkerChat(spec, s.llm_api_key, s.llm_model)


def build_pool(slugs: list[str] | None = None):
    """slug→ChatModel. slugs=None이면 국내 SPECS 전체.

    solar/ax/exaone 각각: keys.json(`FUGU_KEYS`)에 실키가 있으면 그걸 우선 쓴다(기존
    keys.json-only 사용자 무변경). 없거나(파일 자체가 없거나) 플레이스홀더면 repo-root
    `.env`(production과 동일 `ORTHUS_LLM_SOLAR_API_KEY` 등 env var)로 폴백한다
    (`_vendor_env_fallback`). 둘 다 없으면 기존과 동일하게 KeyError.

    특수 slug ``baseline`` → orthus 현행 프로덕션 모델(env 기반, company 노드는 gpt-4o-mini)을
    같은 `WorkerChat`으로 감싼 것. 국내 워커와 동일 ChatModel Protocol + 동일 usage 계측 경로라
    하네스가 동일하게 실행/계측한다. 비교 기준선(현행 대비)이며 오케스트레이션 풀 멤버는 아니다.
    """
    entries = _load_keys()
    pool: dict = {}
    for spec in SPECS:
        if slugs and spec.slug not in slugs:
            continue
        entry = next((e for e in entries if spec.provider_match in str(e.get("provider", "")).lower()), None)
        if entry is not None and _is_real_key(entry.get("key")):
            model = spec.model or str(entry.get("model", "")).split()[0]
            pool[spec.slug] = WorkerChat(spec, entry["key"], model)
            continue
        fallback = _vendor_env_fallback(spec.slug)
        if fallback is not None:
            pool[spec.slug] = fallback
            continue
        raise KeyError(
            f"{spec.slug}: keys.json에 provider 매칭 실패({spec.provider_match}) 그리고 "
            f".env에도 ORTHUS_LLM_{spec.slug.upper()}_API_KEY 없음"
        )
    if slugs and "baseline" in slugs:
        pool["baseline"] = _baseline_chat()
    return pool


if __name__ == "__main__":
    # 스모크 = E5 **G-스모크 게이트**: 4모델 × 1콜로 usage 필드 존재를 먼저 확인한 뒤 본실행.
    # (키·응답원문 미노출 — 길이/지연/토큰만.) 사용:
    #   PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/pool.py [slug,...]
    import sys

    slugs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["solar", "ax", "exaone", "baseline"]
    pool = build_pool(slugs)
    missing: list[str] = []
    for slug, chat in pool.items():
        t0 = time.monotonic()
        try:
            chat.reset_usage()
            out = chat.complete("한 문장만 답하라.", "안녕?", json_only=False)
            u = chat.usage_totals()
            tok = f"in={u['in_tok']:>5} out={u['out_tok']:>5}" if not u["n_usage_missing"] else "usage 결측!"
            if u["n_usage_missing"]:
                missing.append(slug)
            print(f"{slug:8} OK  {int((time.monotonic()-t0)*1000):>6}ms  len={len(out):>4}  {tok}")
        except Exception as e:  # noqa: BLE001
            missing.append(slug)
            print(f"{slug:8} ERR {type(e).__name__}: {str(e)[:80]}")
    print("\nG-스모크: " + ("PASS — 전 모델 usage 노출" if not missing else f"FAIL — {missing} (§7 결측 처리 필요)"))
