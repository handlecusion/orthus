"""Arena P5 실측 실행 러너.

문항: `golden/arena_gold/gold_labels.jsonl`(gold) + `golden/arena_parts/*.jsonl`(input),
id로 join. `split=="holdout"` 그리고 `retired!=true`인 것만 대상(1,589건).

조건: 집중 6태스크(contract/toolcall/delegation/distill/decompose/email — gold task
값 기준)는 bare/glue 2조건, 회귀 7태스크(structured/intent/gap_suggest/graph_bind/
routing/claim_headline/wiki_qa)는 bare 1조건. 프롬프트는 `arena_prompts.py`.

벤더 호출: solar/ax/exaone은 `orthus.models.registry.vendor_specs()`(프로덕션과 동일
env 기반 설정 — `ORTHUS_LLM_SOLAR_API_KEY` 등)에서 base_url/model/extra_body/
min_interval을 그대로 재사용한다(D0 확정 스펙, `pool.py`와 동일 소스). gpt-5.3/gpt-4o/
glm-5.2/deepseek-v4-pro는 `experiments/fugu-ko/harness_e2e.py`의 `_build_openai_chat`/
`_build_glm_chat`/`_build_deepseek_chat` 패턴(env var 이름·base_url·model 문자열)을
재사용한다. 실제 HTTP 재시도/백오프는 두 경로 모두 `orthus.models.adapters.openai_compat
._post_json`(429/5xx Retry-After 존중 + 지수 백오프, 기본 6회 재시도)을 그대로 쓴다 —
`pool.WorkerChat`을 직접 쓰지 않는 이유는 그 클래스의 usage 계측이 인스턴스 공유 상태
(`self.usage_events`)라 이 러너의 스레드 동시성(요구사항 4)에서 안전하지 않기 때문이다
(concurrent 스레드가 같은 리스트에 append하면 어느 콜의 usage인지 뒤섞인다). 대신 여기
`VendorClient.complete()`는 HTTP 응답의 usage를 그 호출 프레임 안에서만 다루고 바로
반환한다(공유 mutable state 없음) — pool.py가 쓰는 것과 동일한 스펙/재시도 유틸을
스레드 세이프하게 감싼 것.

체크포인트: 결과는 `analysis/raw/arena/<system>.jsonl`에 문항 단위 append. 재실행 시
이미 파일에 있는 (id, condition) 쌍은 건너뛴다(중단 후 그 지점부터 재개).

비용 상한: 응답 usage(prompt/completion tokens)로 하드코딩 단가 테이블을 이용해 누적
비용을 추정하고, `--cost-cap-usd` 도달 시 새 문항 제출을 멈춘다(이미 진행 중인 콜은
완료됨). 체크포인트가 이미 append-as-you-go라 재개 시 그 지점부터 이어진다.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# repo root 탐색: 이 파일이 `.worktrees/<topic>/experiments/fugu-ko/arena_run.py`(워크트리)든
# `experiments/fugu-ko/arena_run.py`(메인 체크아웃)든 동작해야 한다. `.worktrees`라는 이름의
# 조상 디렉터리를 찾으면 그 부모가 메인 저장소 루트(= `.env`가 있는 곳)다. 못 찾으면(메인
# 체크아웃에서 직접 실행) harness_e2e.py와 동일하게 experiments/fugu-ko -> experiments -> root.
def _find_repo_root(start: Path) -> Path:
    for p in start.parents:
        if p.name == ".worktrees":
            return p.parent
    return start.parent.parent


_REPO_ROOT = _find_repo_root(HERE)

import sys  # noqa: E402

for _p in (str(HERE), str(HERE.parent.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from arena_prompts import ALL_TASKS, FOCUSED_TASKS, REGRESSION_TASKS, build_prompt  # noqa: E402

GOLD_PATH = HERE / "golden" / "arena_gold" / "gold_labels.jsonl"
PARTS_DIR = HERE / "golden" / "arena_parts"
DEFAULT_OUT_DIR = HERE / "analysis" / "raw" / "arena"

# 시스템 슬러그 -> (in $/1M tok, out $/1M tok). 사용자 지정 하드코딩 단가(GLM 과금사고 이력 때문에
# 필수 — 실측 usage 없이는 상한을 걸 수 없다).
PRICING: dict[str, tuple[float, float]] = {
    "gpt-5.3": (2.5, 15.0),
    "gpt-4o": (2.5, 10.0),
    "glm-5.2": (1.4, 4.4),
    "deepseek-v4-pro": (1.74, 3.48),
    "solar": (0.15, 0.6),
    "exaone": (0.2, 0.8),
    "ax": (0.3, 1.2),
    # Bedrock Claude 2종 (Sonnet 4.6 / Haiku 4.5) — 모델 id는 harness_e2e.py의
    # 실사용 문자열과 동일 (PHASE6_MODEL_IDS.md 확정치).
    "claude-sonnet-4.6": (3.0, 15.0),
    "claude-haiku-4.5": (1.0, 5.0),
    # P5 추가 2종 (2026-07-23). gpt-5.5 단가는 실제 공개가 미확인 — gpt-5.4 참고 추정치
    # $2.5/$15 per 1M을 표시용으로 가정(실제와 다를 수 있음, 카나리에서 usage는 실측).
    "gpt-5.5": (2.5, 15.0),
    # claude-opus-4.6 단가는 사용자 지정 $5/$25 per 1M(Sonnet 4.6 $3/$15 단가비 외삽).
    "claude-opus-4.6": (5.0, 25.0),
    # glm-5-bedrock: z.ai 직접 API(glm-5.2)가 잔고 충전 후에도 지속 429/에러(12시도
    # 0성공, 790/2,899 좌초)라 Bedrock 경유로 전환한 신규 시스템. 엔드포인트도 모델
    # 버전도 다르므로(GLM-5, zai.glm-5) glm-5.2와 완전히 분리된 체크포인트로 측정한다.
    # 단가는 사용자 지정 $1.4/$4.4 per 1M(=z.ai 공개 단가 가정) 표시용 — Bedrock
    # 실제 청구 단가는 다를 수 있음(결과 파일에 명시).
    "glm-5-bedrock": (1.4, 4.4),
    # P6 로스터 확장 2종 (2026-07-24, 6종 주판정 track). claude-opus-4.8 단가는
    # claude-opus-4.6과 동일 가정치($5/$25, 실제 공개가 미확인) 표시용.
    "claude-opus-4.8": (5.0, 25.0),
    # gpt-5.6은 OpenAI 계정에 단일 "gpt-5.6"이 없고 코드네임 3종
    # (gpt-5.6-luna/sol/terra, 메타데이터로 구별 불가)만 있다. 사용자 지정으로
    # "gpt-5.6-luna"를 대표로 확정(2026-07-24). 단가는 gpt-5.5와 동일 가정치.
    "gpt-5.6": (2.5, 15.0),
    # KO-PARITY 로스터(KOREAN_ADVANTAGE_PLAN §3.1b)의 "DeepSeek V3.2" — arena에는
    # v4-pro만 있었다. ⚠️ 이 슬러그는 **직접 API 사문**이다(아래 _DEEPSEEK_MODEL_IDS
    # 주석). 실사용 V3.2 슬롯은 Bedrock 경로 `deepseek-v3.2-bedrock`이다.
    "deepseek-v3.2": (1.74, 3.48),
    # ✅ 2026-07-27 owner 최종 결정: **DeepSeek 로스터 슬롯 = V3.2 Bedrock 경로**.
    # 직접 API에는 V3.2가 없고(400) Bedrock에는 있다(`deepseek.v3.2`, ON_DEMAND).
    # ⚠️ 단가는 **추정치**다 — Bedrock DeepSeek V3.2 공개 단가를 확인하지 못해
    # 직접 API v4-pro 단가($1.74/$3.48 per 1M)를 잠정 적용한다. 실제 Bedrock 청구와
    # 다를 수 있으며, 이 표는 비용 상한(--cost-cap-usd)과 추정 리포트 표시용이다.
    "deepseek-v3.2-bedrock": (1.74, 3.48),  # 추정 — v4-pro 단가 차용(공개가 미확인)
    # 2026-07-24 추가 측정: 두 번째 코드네임 후보 "gpt-5.6-sol"(사용자 지정, luna와
    # 비교용 — REFERENCE_EXTERNAL 진단군, 주판정 EXTERNAL_SYSTEMS는 luna 유지).
    # 단가 가정은 gpt-5.6(luna)과 동일.
    "gpt-5.6-sol": (2.5, 15.0),
}
SYSTEMS = tuple(PRICING.keys())

# claude-* 시스템 슬러그 -> bare Bedrock modelId (us. 접두는 BedrockConverseChat과
# 동일한 _normalize_model_id가 붙인다).
_CLAUDE_BEDROCK_MODEL_IDS: dict[str, str] = {
    "claude-sonnet-4.6": "anthropic.claude-sonnet-4-6",
    "claude-haiku-4.5": "anthropic.claude-haiku-4-5-20251001-v1:0",
    # 2026-07-23 카나리로 확정: `anthropic.claude-opus-4-6` / `-v1:0` / 날짜접미 변형은
    # 전부 400 invalid, Bedrock ListFoundationModels(byProvider=anthropic)로 실제
    # modelId를 조회해 `anthropic.claude-opus-4-6-v1`(끝에 `:0` 없음)로 확정.
    "claude-opus-4.6": "anthropic.claude-opus-4-6-v1",
    # 2026-07-24 ListFoundationModels(byProvider=anthropic) 조회로 바로 확인:
    # opus-4.6과 달리 `anthropic.claude-opus-4-8`가 그대로 modelId(끝에 `-v1` 없음).
    "claude-opus-4.8": "anthropic.claude-opus-4-8",
}

# glm-5-bedrock 전용 Bedrock modelId (사용자 지정 `zai.glm-5`). 400/404가 나면
# ListFoundationModels(byProvider=zai 또는 unfiltered)로 실제 modelId를 조회해
# 여기를 갱신한다(claude-opus-4.6의 `-v1` suffix 확정 선례와 동일 절차).
_GLM_BEDROCK_MODEL_IDS: dict[str, str] = {
    "glm-5-bedrock": "zai.glm-5",
}

# DeepSeek OpenAI 호환 API 모델 id (**직접 API 경로**).
# ⚠️ 2026-07-27 카나리 확정: 이 **직접 API 계정**에 V3.2 계열은 존재하지 않는다. `GET /models`가
# `deepseek-v4-flash` / `deepseek-v4-pro` 둘만 노출하고, `deepseek-v3.2` /
# `deepseek-v3-2` / `deepseek-v3.2-exp`는 전부 400 —
# "The supported API model names are deepseek-v4-pro or deepseek-v4-flash".
# (`deepseek-chat`·`deepseek-reasoner`는 200이지만 응답 `model`이 `deepseek-v4-flash`로
# 해석되는 별칭이라 V3.2 대체가 아니다.)
#
# 관측 사실 정리(혼동 금지 — 두 경로는 **다른 모델 버전 + 다른 엔드포인트**다):
#   · 직접 API(api.deepseek.com): V4 계열만 있고 **V3.2는 없다**.
#   · Bedrock(us-east-1): DeepSeek은 `deepseek.v3.2`(ON_DEMAND)와
#     `deepseek.r1-v1:0`(INFERENCE_PROFILE `us.deepseek.r1-v1:0`)뿐이고 **V4 계열은 없다**
#     (`deepseek.v4-pro` Converse는 400 "The provided model identifier is invalid";
#     전 provider 통틀어 `v4` 문자열은 `cohere.embed-v4:0`밖에 없다).
#
# ✅ 2026-07-27 owner **최종** 결정: 로스터 DeepSeek 슬롯 = **V3.2 Bedrock 경로**
# (`deepseek-v3.2-bedrock`, 아래 `_DEEPSEEK_BEDROCK_MODEL_IDS`). 근거는 (1) 원래 계획서가
# 지정한 버전이 V3.2이고 (2) 같은 2문항 카나리에서 p50 1,472ms로 v4-pro 3,093ms보다
# 2.10배 빠르며, completion 토큰도 7.0 vs 167.5로 추론 토큰 상수가 붙지 않는다.
#
# 아래 두 직접 API 항목은 **삭제하지 않고 남긴다**:
#   · `deepseek-v4-pro` — 호출 가능한 대체 옵션(카나리 PASS 3,093ms). 기본 로스터에서는
#     제외됐다(`koparity_canary.ROSTER`, `koparity_cost.ROSTER_FRONTIER`,
#     `koparity_analyze.FRONTIER_SYSTEMS`). 필요하면 `--systems deepseek-v4-pro`로 부른다.
#   · `deepseek-v3.2` — **호출 불가능한 사문(死文)**(직접 API가 400). 슬러그를 조용히 없애면
#     진행 중 스크립트가 KeyError로 깨져서 남겨둔다. **이 슬러그로 측정하지 마라** —
#     실사용 V3.2는 `deepseek-v3.2-bedrock`이다.
_DEEPSEEK_MODEL_IDS: dict[str, str] = {
    "deepseek-v4-pro": "deepseek-v4-pro",  # 대체 옵션 — 기본 로스터 제외
    "deepseek-v3.2": "deepseek-v3.2",  # dead — 위 주석 참조 (Bedrock 슬러그와 혼동 금지)
}

# DeepSeek Bedrock 경로 — **로스터 확정 슬롯**(owner 결정 2026-07-27).
# `zai.glm-5`가 접두 없이 동작한 선례가 있어 접두 필요 여부를 카나리로 **실측 확인**했다
# (2026-07-27): 접두 없는 `deepseek.v3.2` = 200 PASS(p50 1,472ms, 2/2), 반면
# `us.deepseek.v3.2` = 400 "The provided model identifier is invalid".
# → ON_DEMAND 모델이라 cross-region inference profile 접두를 **붙이면 안 된다**(기본 "").
# claude/glm과 별도 env로 분리해 서로 영향 없게 한다.
_DEEPSEEK_BEDROCK_MODEL_IDS: dict[str, str] = {
    "deepseek-v3.2-bedrock": "deepseek.v3.2",
}


# --------------------------------------------------------------------------- #
# 데이터 로드 + join
# --------------------------------------------------------------------------- #
def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_gold() -> dict[str, dict]:
    return {row["id"]: row for row in _load_jsonl(GOLD_PATH)}


def load_inputs() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for fp in sorted(glob.glob(str(PARTS_DIR / "*.jsonl"))):
        for row in _load_jsonl(Path(fp)):
            out[row["id"]] = row
    return out


def build_joined_records(tasks_wanted: set[str] | None = None) -> list[dict]:
    """id로 join, split=="holdout" and retired!=True 필터, canonical task(gold 기준) 부여.

    `tasks_wanted`가 주어지면 그 canonical task만 남긴다. 반환은 id 오름차순 정렬
    (canary/limit 절단이 매 실행 결정적이도록).
    """
    gold_by_id = load_gold()
    input_by_id = load_inputs()

    records: list[dict] = []
    missing_input = 0
    for gid, grow in gold_by_id.items():
        irow = input_by_id.get(gid)
        if irow is None:
            missing_input += 1
            continue
        if irow.get("split") != "holdout" or irow.get("retired") is True:
            continue
        task = grow["task"]
        if tasks_wanted is not None and task not in tasks_wanted:
            continue
        records.append(
            {
                "id": gid,
                "task": task,
                "gold": grow.get("gold"),
                "input": irow.get("input", {}),
                "track": irow.get("track"),
                "axis": irow.get("axis"),
                "trap_kind": irow.get("trap_kind"),
            }
        )
    if missing_input:
        print(f"[warn] gold_labels rows with no matching arena_parts input: {missing_input}")
    records.sort(key=lambda r: r["id"])
    return records


def expand_work_items(records: list[dict]) -> list[dict]:
    """레코드 1개 -> 집중 태스크는 bare+glue 2개 work item, 회귀 태스크는 bare 1개."""
    items: list[dict] = []
    for r in records:
        conditions = ("bare", "glue") if r["task"] in FOCUSED_TASKS else ("bare",)
        for cond in conditions:
            items.append({**r, "condition": cond})
    items.sort(key=lambda it: (it["task"], it["id"], it["condition"]))
    return items


# --------------------------------------------------------------------------- #
# 벤더 클라이언트 — thread-safe, per-call usage (공유 mutable state 없음)
# --------------------------------------------------------------------------- #
class VendorClient:
    def __init__(
        self,
        slug: str,
        base_url: str,
        api_key: str,
        model: str,
        *,
        extra_body: dict | None = None,
        timeout: float = 60.0,
        min_interval: float = 0.0,
        temperature: float | None = 0.0,
    ):
        self.slug = slug
        self.model_id = model
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._extra_body = dict(extra_body or {})
        self._timeout = timeout
        self._min_interval = min_interval
        self._temperature = temperature
        self._throttle_lock = threading.Lock()
        self._last_call = 0.0

    def _throttle(self) -> None:
        # A.X류 팀 레이트캡(RPS 하드캡)은 스레드 동시성과 무관하게 실제 호출 간
        # 최소 간격을 강제해야 하므로, 이 락은 의도적으로 인스턴스 전역이다(공유 usage
        # state와 달리 여기선 공유가 바로 요구사항이다).
        if self._min_interval <= 0:
            return
        with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def complete(self, system: str | None, user: str, *, json_only: bool) -> tuple[str, dict]:
        from orthus.models.adapters.openai_compat import _post_json

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body: dict[str, Any] = {"model": self.model_id, "messages": messages}
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if json_only:
            body["response_format"] = {"type": "json_object"}
        body.update(self._extra_body)

        self._throttle()
        data = _post_json(self._base, "/chat/completions", self._key, body, self._timeout)
        choice = data["choices"][0]["message"]
        content = choice.get("content")
        if content is None:
            raise RuntimeError(
                f"{self.slug}: null content (finish={data['choices'][0].get('finish_reason')})"
            )
        usage = data.get("usage") or {}
        return content, {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }


class BedrockVendorClient:
    """Thread-safe Bedrock Converse client for `claude-*` slugs, symmetric to
    `VendorClient` above (same reasoning: `pool.WorkerChat`/`BedrockConverseChat`
    instances share mutable usage state across threads, which this runner's
    concurrency (--concurrency) would corrupt — see module docstring).

    Reuses the REAL production Bedrock wire functions
    (`orthus.models.adapters.bedrock._normalize_model_id/_post_converse/
    _extract_response_text`) rather than reimplementing the Converse request/
    response shape — only adds per-call token usage extraction, which
    `BedrockConverseChat.complete()` does not expose (it returns text only).
    """

    def __init__(
        self,
        slug: str,
        *,
        api_key: str,
        region: str,
        model_id: str,
        inference_prefix: str = "us",
        max_tokens: int = 4096,
        temperature: float | None = 0.0,
        timeout: float = 60.0,
    ):
        from orthus.models.adapters.bedrock import _normalize_model_id

        self.slug = slug
        self._key = api_key
        self._base = f"https://bedrock-runtime.{region}.amazonaws.com"
        self._model_id = _normalize_model_id(model_id, inference_prefix)
        self.model_id = f"bedrock:{self._model_id}"
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout = timeout

    def complete(self, system: str | None, user: str, *, json_only: bool) -> tuple[str, dict]:
        from orthus.models.adapters.bedrock import _extract_response_text, _post_converse

        system_text = (system or "").strip()
        if json_only:
            json_instruction = "Return only a valid JSON object. Do not use Markdown fences."
            system_text = (
                f"{system_text}\n\n{json_instruction}" if system_text else json_instruction
            )

        inference_config: dict[str, Any] = {"maxTokens": self._max_tokens}
        if self._temperature is not None:
            inference_config["temperature"] = self._temperature
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": inference_config,
        }
        if system_text:
            body["system"] = [{"text": system_text}]

        data = _post_converse(
            self._base, self._model_id, self._key, body, timeout=self._timeout
        )
        text = _extract_response_text(data)
        usage = data.get("usage") or {}
        return text, {
            "prompt_tokens": usage.get("inputTokens"),
            "completion_tokens": usage.get("outputTokens"),
        }


class DryRunClient:
    """--dry-run 전용: 네트워크 호출 없이 고정 문자열 + 고정 usage를 반환."""

    slug = "dry-run"
    model_id = "dry-run"

    def complete(self, system: str | None, user: str, *, json_only: bool) -> tuple[str, dict]:
        time.sleep(0.001)
        fake = '{"dry_run": true}' if json_only else "dry-run output"
        return fake, {"prompt_tokens": 10, "completion_tokens": 5}


def build_client(system: str) -> "VendorClient | BedrockVendorClient":
    if system not in SYSTEMS:
        raise ValueError(f"unknown --system {system!r}, expected one of {SYSTEMS}")

    if system in ("solar", "ax", "exaone"):
        from orthus.models.registry import vendor_specs

        spec = vendor_specs()[system]
        if not spec.api_key.strip():
            raise RuntimeError(
                f"{system}: ORTHUS_LLM_{system.upper()}_API_KEY not set — check --env-file "
                f"resolves to the main repo .env (got {_REPO_ROOT / '.env'})"
            )
        return VendorClient(
            system,
            spec.base_url,
            spec.api_key,
            spec.model,
            extra_body=spec.extra_body,
            timeout=spec.timeout,
            min_interval=spec.min_interval,
        )

    if system == "gpt-5.3":
        key = os.environ.get("ORTHUS_LLM_API_KEY", "")
        base = os.environ.get("ORTHUS_LLM_BASE_URL", "https://api.openai.com/v1")
        if not key.strip():
            raise RuntimeError("ORTHUS_LLM_API_KEY not set for gpt-5.3")
        # gpt-5.3-chat-latest rejects any explicit temperature but its own default
        # (harness_e2e.py precedent) -> omit the field entirely.
        return VendorClient(system, base, key, "gpt-5.3-chat-latest", timeout=60, temperature=None)

    if system == "gpt-4o":
        key = os.environ.get("ORTHUS_LLM_API_KEY", "")
        base = os.environ.get("ORTHUS_LLM_BASE_URL", "https://api.openai.com/v1")
        if not key.strip():
            raise RuntimeError("ORTHUS_LLM_API_KEY not set for gpt-4o")
        return VendorClient(system, base, key, "gpt-4o", timeout=60)

    if system == "gpt-5.5":
        key = os.environ.get("ORTHUS_LLM_API_KEY", "")
        base = os.environ.get("ORTHUS_LLM_BASE_URL", "https://api.openai.com/v1")
        if not key.strip():
            raise RuntimeError("ORTHUS_LLM_API_KEY not set for gpt-5.5")
        # 2026-07-23 카나리 확정: "gpt-5.5"는 존재(resolves to gpt-5.5-2026-04-23),
        # "gpt-5.5-chat-latest"는 404. gpt-5.3-chat-latest와 동일한 quirk로
        # temperature=0을 거부하고 default(=1)만 허용 -> omit(gpt-5.3 선례 재사용).
        return VendorClient(system, base, key, "gpt-5.5", timeout=60, temperature=None)

    if system == "gpt-5.6":
        key = os.environ.get("ORTHUS_LLM_API_KEY", "")
        base = os.environ.get("ORTHUS_LLM_BASE_URL", "https://api.openai.com/v1")
        if not key.strip():
            raise RuntimeError("ORTHUS_LLM_API_KEY not set for gpt-5.6")
        # 2026-07-24 확인: 계정에 단일 "gpt-5.6"이 없고 코드네임 3종(luna/sol/terra,
        # 메타데이터 구별 불가)만 존재 — 사용자 지정으로 "gpt-5.6-luna"를 대표로 확정.
        # gpt-5.5/gpt-5.3 선례와 동일하게 temperature=0을 거부할 수 있어 omit.
        return VendorClient(
            system, base, key, "gpt-5.6-luna", timeout=60, temperature=None
        )

    if system == "gpt-5.6-sol":
        key = os.environ.get("ORTHUS_LLM_API_KEY", "")
        base = os.environ.get("ORTHUS_LLM_BASE_URL", "https://api.openai.com/v1")
        if not key.strip():
            raise RuntimeError("ORTHUS_LLM_API_KEY not set for gpt-5.6-sol")
        # luna와 동일 계열 quirk 가정(temperature omit) — 카나리로 확인.
        return VendorClient(
            system, base, key, "gpt-5.6-sol", timeout=60, temperature=None
        )

    if system == "glm-5.2":
        key = os.environ.get("ORTHUS_GLM_API_KEY", "")
        if not key.strip():
            raise RuntimeError("ORTHUS_GLM_API_KEY not set for glm-5.2")
        return VendorClient(system, "https://api.z.ai/api/paas/v4/", key, "glm-5.2", timeout=60)

    if system in _DEEPSEEK_MODEL_IDS:
        key = os.environ.get("ORTHUS_LLM_DEEPSEEK_API_KEY", "")
        base = os.environ.get("ORTHUS_LLM_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        if not key.strip():
            raise RuntimeError(f"ORTHUS_LLM_DEEPSEEK_API_KEY not set for {system}")
        model = os.environ.get("ORTHUS_LLM_DEEPSEEK_MODEL_OVERRIDE") or _DEEPSEEK_MODEL_IDS[system]
        return VendorClient(system, base, key, model, timeout=60)

    if system in _CLAUDE_BEDROCK_MODEL_IDS:
        key = os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY") or os.environ.get(
            "ORTHUS_LLM_API_KEY", ""
        )
        if not key.strip():
            raise RuntimeError(
                f"ORTHUS_LLM_BEDROCK_API_KEY (or ORTHUS_LLM_API_KEY) not set for {system}"
            )
        region = os.environ.get("ORTHUS_LLM_BEDROCK_REGION", "us-east-1")
        inference_prefix = os.environ.get("ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX", "us")
        # 2026-07-24 카나리로 확인: claude-opus-4.8은 `temperature` 필드 자체를 거부
        # ("temperature is deprecated for this model") — gpt-5.3/5.5의 quirk와 동일
        # 패턴이라 inferenceConfig에서 완전히 생략(omit)한다.
        temperature = None if system == "claude-opus-4.8" else 0.0
        return BedrockVendorClient(
            system,
            api_key=key,
            region=region,
            model_id=_CLAUDE_BEDROCK_MODEL_IDS[system],
            inference_prefix=inference_prefix,
            temperature=temperature,
            timeout=60.0,
        )

    if system in _DEEPSEEK_BEDROCK_MODEL_IDS:
        # glm-5-bedrock과 동일 패턴: claude-* Bedrock 경로와 같은 자격(같은 account/key)을
        # 재사용하고, 접두만 별도 env로 분리한다. `deepseek.v3.2`는 ON_DEMAND라
        # cross-region inference profile 접두가 필요 없다(카나리 실측) — 기본 빈 문자열.
        key = os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY") or os.environ.get(
            "ORTHUS_LLM_API_KEY", ""
        )
        if not key.strip():
            raise RuntimeError(
                f"ORTHUS_LLM_BEDROCK_API_KEY (or ORTHUS_LLM_API_KEY) not set for {system}"
            )
        region = os.environ.get("ORTHUS_LLM_BEDROCK_REGION", "us-east-1")
        inference_prefix = os.environ.get("ORTHUS_LLM_BEDROCK_DEEPSEEK_INFERENCE_PREFIX", "")
        model = (
            os.environ.get("ORTHUS_LLM_BEDROCK_DEEPSEEK_MODEL_OVERRIDE")
            or _DEEPSEEK_BEDROCK_MODEL_IDS[system]
        )
        return BedrockVendorClient(
            system,
            api_key=key,
            region=region,
            model_id=model,
            inference_prefix=inference_prefix,
            timeout=60.0,
        )

    if system in _GLM_BEDROCK_MODEL_IDS:
        # claude-* Bedrock 경로와 동일 자격(같은 Bedrock account/key)을 재사용한다.
        key = os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY") or os.environ.get(
            "ORTHUS_LLM_API_KEY", ""
        )
        if not key.strip():
            raise RuntimeError(
                f"ORTHUS_LLM_BEDROCK_API_KEY (or ORTHUS_LLM_API_KEY) not set for {system}"
            )
        region = os.environ.get("ORTHUS_LLM_BEDROCK_REGION", "us-east-1")
        # zai.glm-5는 claude 계열과 달리 cross-region inference profile 접두가 필요
        # 없을 수 있다(마켓플레이스/on-demand 모델 가능성) — 카나리 결과로 확정 전까지는
        # ORTHUS_LLM_BEDROCK_GLM_INFERENCE_PREFIX(기본 빈 문자열=접두 없음)로 조정한다.
        # claude 쪽 ORTHUS_LLM_BEDROCK_INFERENCE_PREFIX와 별도 env로 분리해 서로 영향 없음.
        inference_prefix = os.environ.get("ORTHUS_LLM_BEDROCK_GLM_INFERENCE_PREFIX", "")
        return BedrockVendorClient(
            system,
            api_key=key,
            region=region,
            model_id=_GLM_BEDROCK_MODEL_IDS[system],
            inference_prefix=inference_prefix,
            timeout=60.0,
        )

    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- #
# 체크포인트
# --------------------------------------------------------------------------- #
def load_done_pairs(out_path: Path) -> set[tuple[str, str]]:
    """resume 대상 = **실패 없이 완료된** (id, condition)만.

    실패 행(`error` 필드 non-null)도 append되므로, 그것까지 done으로 세면 재실행해도
    실패 문항이 영원히 재시도되지 않는다(타임아웃/429 소진/일시적 5xx가 그대로 결손으로
    굳는다). 같은 저장소의 올바른 선례는 `judge/panel.py::_done_keys` — 깨끗한 행만
    done으로 본다. 재시도분은 같은 (id, condition)으로 append되고 **뒤 행이 앞 행을
    이긴다**(마지막 행 기준). 따라서 한 번 성공하면 다음 실행부터 건너뛰고, 성공 뒤
    다시 실패한 경우에도 최신 상태를 따른다.
    """
    if not out_path.exists():
        return set()
    ok: dict[tuple[str, str], bool] = {}
    with open(out_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ok[(d.get("id"), d.get("condition"))] = not d.get("error")
    return {k for k, clean in ok.items() if clean}


# --------------------------------------------------------------------------- #
# 비용
# --------------------------------------------------------------------------- #
def estimate_cost_usd(usage: dict, price_in: float, price_out: float) -> float:
    pt = usage.get("prompt_tokens") or 0
    ct = usage.get("completion_tokens") or 0
    return (pt / 1_000_000.0) * price_in + (ct / 1_000_000.0) * price_out


# --------------------------------------------------------------------------- #
# 실행
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(args: argparse.Namespace) -> None:
    if args.tasks == "all":
        tasks_wanted = set(ALL_TASKS)
    else:
        tasks_wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        unknown = tasks_wanted - set(ALL_TASKS)
        if unknown:
            raise SystemExit(f"unknown task(s) in --tasks: {sorted(unknown)}; valid={ALL_TASKS}")

    records = build_joined_records(tasks_wanted)

    if args.canary:
        by_task: dict[str, list[dict]] = {}
        for r in records:
            by_task.setdefault(r["task"], []).append(r)
        records = [r for task_rows in by_task.values() for r in task_rows[:2]]
        records.sort(key=lambda r: r["id"])

    work_items = expand_work_items(records)

    if args.limit:
        work_items = work_items[: args.limit]

    print(
        f"== arena_run == system={args.system} tasks={sorted(tasks_wanted)} "
        f"canary={args.canary} dry_run={args.dry_run}"
    )
    print(f"joined records: {len(records)} | work items (bare+glue expanded): {len(work_items)}")

    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.system}.jsonl"

    done_pairs = load_done_pairs(out_path)
    remaining = [it for it in work_items if (it["id"], it["condition"]) not in done_pairs]
    print(f"already checkpointed: {len(done_pairs)} pairs | remaining to run: {len(remaining)}")

    if not remaining:
        print("nothing to do — all requested work items already checkpointed.")
        return

    client = DryRunClient() if args.dry_run else build_client(args.system)
    price_in, price_out = PRICING[args.system]

    stop_event = threading.Event()
    cost_lock = threading.Lock()
    write_lock = threading.Lock()
    total_cost = [0.0]
    completed = [0]
    stopped_for_cost = [False]

    def process(item: dict) -> None:
        if stop_event.is_set():
            return
        t0 = time.monotonic()
        raw_output: str | None = None
        usage: dict = {"prompt_tokens": None, "completion_tokens": None}
        error: str | None = None
        try:
            spec = build_prompt(item["task"], item["condition"], item["input"])
            raw_output, usage = client.complete(spec.system, spec.user, json_only=spec.json_only)
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {str(e)[:300]}"
        latency_ms = int((time.monotonic() - t0) * 1000)

        row = {
            "id": item["id"],
            "task": item["task"],
            "condition": item["condition"],
            "system": args.system,
            "raw_output": raw_output,
            "usage": usage,
            "latency_ms": latency_ms,
            "timestamp": _now_iso(),
            "error": error,
        }
        with write_lock:
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            completed[0] += 1

        if error is None:
            # Cost accounting runs in --dry-run too (DryRunClient still returns a fixed
            # usage dict) so the cap/stop logic itself is unit-verifiable offline —
            # only the *dollar figures* are meaningless in dry-run, not the control flow.
            cost = estimate_cost_usd(usage, price_in, price_out)
            with cost_lock:
                total_cost[0] += cost
                if total_cost[0] >= args.cost_cap_usd and not stop_event.is_set():
                    stop_event.set()
                    stopped_for_cost[0] = True
                    print(
                        f"\n[cost-cap] cumulative estimate ${total_cost[0]:.4f} >= "
                        f"cap ${args.cost_cap_usd:.2f} — stopping new submissions "
                        "(checkpoint already saved, resume-safe)."
                    )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool_exec:
        futures = []
        for item in remaining:
            if stop_event.is_set():
                break
            futures.append(pool_exec.submit(process, item))
        for fut in as_completed(futures):
            fut.result()  # surface unexpected exceptions (process() already catches API errors)

    print(f"\ndone. wrote {completed[0]} rows -> {out_path}")
    cost_label = "mock cost (dry-run, not real $)" if args.dry_run else "estimated cumulative cost this run"
    print(f"{cost_label}: ${total_cost[0]:.4f}")
    remaining_after = len(remaining) - completed[0]
    if remaining_after > 0:
        print(
            f"{remaining_after} work item(s) NOT run this pass "
            f"({'cost cap reached' if stopped_for_cost[0] else 'stopped'}) "
            "— rerun the same command to resume."
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", required=True, choices=SYSTEMS, help="측정 대상 시스템 슬러그")
    ap.add_argument(
        "--tasks", default="all",
        help=f"all 또는 콤마 목록. 유효 태스크: {ALL_TASKS}",
    )
    ap.add_argument("--limit", type=int, default=0, help="work item(조건 확장 후) 상한, 0=무제한")
    ap.add_argument("--canary", action="store_true", help="태스크당 문항 2개만(조건 확장 전)")
    # 2026-07-27: 기본 8.0 -> 100.0 상향(KOREAN_ADVANTAGE_PLAN §6.2). 8.0은 P5 arena 규모에
    # 맞춘 값이라 KO-PARITY 규모(30,400 호출)에서는 레인 중간에 확실히 걸린다. 캡 자체는
    # 유지(GLM 과금사고 이력) — 필요하면 `--cost-cap-usd`로 낮춰 카나리를 돌린다.
    ap.add_argument("--cost-cap-usd", type=float, default=100.0, help="누적 추정 비용 상한(USD)")
    ap.add_argument("--concurrency", type=int, default=6, help="동시 실행 워커 수")
    ap.add_argument(
        "--env-file", default=None,
        help=f"vendor API 키가 있는 .env 경로 (기본: 메인 저장소 루트 추정 = {_REPO_ROOT / '.env'})",
    )
    ap.add_argument("--out-dir", default=None, help="raw jsonl 출력 디렉터리(기본: analysis/raw/arena)")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="실제 API 호출 없이 고정 문자열로 join/체크포인트/재개/비용집계 로직만 검증",
    )
    args = ap.parse_args()

    if not args.dry_run:
        from dotenv import load_dotenv

        env_file = Path(args.env_file) if args.env_file else (_REPO_ROOT / ".env")
        if not env_file.exists():
            print(f"[warn] env file not found: {env_file} (vendor keys must already be in the shell env)")
        else:
            load_dotenv(env_file, override=False)

    run(args)


if __name__ == "__main__":
    main()
