"""B2 — 계약 이행(Contract Compliance) 출력 생성 러너.

이 스크립트는 **채점하지 않는다**. `golden/b2_contract.json`의 각 문항에 대해
프로덕션이 실제로 쓰는 시스템 프롬프트를 그대로 **import**해서 모델을 호출하고, 모델의
원문 출력을 `raw/b2_<model>.jsonl`(`{"id","raw"}`)로 적는다. 채점은 별도의
`b2_contract.py score`가 이 파일들을 소비한다(§B2 prereg).

프롬프트 출처(전부 import, 재작성 금지)
--------------------------------------
- structured_compile   : orthus.assistant.compile._SYSTEM (+ _render_catalog/_render_grounding)
- router_classify      : orthus.router.route._SYSTEM
- router_intent        : orthus.router.route._INTENT_SYSTEM
- graph_bind           : orthus.router.graph._BIND_SYSTEM
- delegation_extract   : orthus.agentwork.delegation._EXTRACT_SYSTEM_PROMPT
- email_draft          : orthus.agentwork.service._generate_command_email (프롬프트가 함수
                         내부에서 조립되므로, 프로덕션 함수를 그대로 호출하고 그 함수가 쓰는
                         chat model을 캡처링 래퍼로 갈아끼워 raw completion을 가로챈다 —
                         재작성 아님, 실제 message assembly 재사용)
- agentic_tool_call    : orthus.router.agentic.loop._system_prompt + tools.TOOL_SPECS
                         (프로덕션 tool-use 표면; bedrock은 Converse toolConfig, OpenAI-호환
                         모델은 native function-calling으로 같은 TOOL_SPECS/시스템 프롬프트 전달)
- email_draft_payload  : **프로덕션 LLM 프롬프트가 존재하지 않는 유일한 표면.** payload는
                         orthus.agentwork.service가 결정론적으로 조립하고, 계약(parser_ref)은
                         orthus.schemas.canonical.EmailDraftPayload(extra='forbid') 스키마다.
                         그래서 지시문을 import한 Pydantic 스키마(`model_json_schema()`)에서
                         직접 유도한다(내 문장이 아니라 스키마가 SoR). 하단 REPORT 참고.

어댑터 재사용
-------------
- solar/ax/exaone : experiments/fugu-ko/pool.py::build_pool (프로덕션 vendor 배선 = .env)
- gpt-4o-mini     : orthus.models.adapters.openai_compat.OpenAIChat (프로덕션 OpenAI 어댑터)
- bedrock         : orthus.models.adapters.bedrock.BedrockConverseChat (프로덕션 Converse 어댑터)

키는 .env(python-dotenv)에서 읽고 값은 절대 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
GOLDEN_PATH = HERE / "golden" / "b2_contract.json"
RAW_DIR = HERE / "raw"

# .env는 repo-root에 있고 pydantic settings/pool 둘 다 여기 값을 읽는다. 로드 전 import 금지.
load_dotenv(REPO_ROOT / ".env")

# 모든 요청에 넉넉한 상한을 강제해 truncation이 계약 위반으로 오인되지 않게 한다(§3 하네스 규칙).
MAX_TOKENS = 1024

# ---- 프로덕션 프롬프트/헬퍼 import (재작성 금지) ---------------------------------- #
from orthus.agentwork import service as agent_service  # noqa: E402
from orthus.agentwork.delegation import (  # noqa: E402
    _EXTRACT_EXCERPT_MAX,
    _EXTRACT_SYSTEM_PROMPT,
)
from orthus.assistant.compile import (  # noqa: E402
    _SYSTEM as COMPILE_SYSTEM,
    _render_catalog,
    _render_grounding,
)
# 아카이브: 프론티어 저지 arm은 Bedrock 어댑터를 썼다. Solar 단일 공개 빌드에는
# 그 어댑터가 없으므로, 이 스크립트는 실행 시 명확히 안내하고 종료한다.
try:
    from orthus.models.adapters.bedrock import (  # noqa: E402
        BedrockConverseChat,
        _post_converse,
    )
except ImportError:  # pragma: no cover - archival script
    raise SystemExit(
        "archival: 이 스크립트의 프론티어 저지 arm은 Bedrock 어댑터가 필요하다 — "
        "Solar 단일 공개 빌드에는 포함되지 않는다 (experiments/README.md 참조)"
    )
from orthus.models.adapters.openai_compat import OpenAIChat, _post_json  # noqa: E402
from orthus.router.agentic.loop import _system_prompt as _agentic_system_prompt  # noqa: E402
from orthus.router.agentic import tools as agentic_tools  # noqa: E402
from orthus.router.graph import _BIND_SYSTEM  # noqa: E402
from orthus.router.route import _INTENT_SYSTEM, _SYSTEM as ROUTE_SYSTEM  # noqa: E402
from orthus.schemas.canonical import EmailDraftPayload  # noqa: E402
from orthus.settings import get_settings  # noqa: E402

import pool  # noqa: E402  (experiments/fugu-ko/pool.py — PYTHONPATH에 있어야 함)

# agentic_tool_call: 골든의 tool_allowlist(team_members/board/projects/mail_search/kg_relations/
# entity_relations/inbox_summary/data_gaps 포함)는 프로덕션 **bedrock 엔진 + company 노드 + KG 가용**
# 설정에서 advertise되는 전체 도구셋을 겨눈다. loop.py의 append 순서/플래그를 그대로 재현한다:
#   TOOL_SPECS + mail_search + kg_relations/entity_relations + meta(inbox_summary/data_gaps)
#   + company_directory(team_members/board/projects).
# mail_compose(mail_send_enabled 필요)·ask_user(hitl 필요)·wiki_update_candidate는 골든이 겨누지
# 않으므로 프로덕션에서도 이 조합에선 꺼져 있어(off) 제외한다. 3개 base만 주면 25/40이 강제 C6가 된다.
AGENTIC_TOOL_SPECS: list[dict] = [
    *agentic_tools.TOOL_SPECS,
    agentic_tools.MAIL_SEARCH_SPEC,
    agentic_tools.KG_RELATIONS_SPEC,
    agentic_tools.ENTITY_RELATIONS_SPEC,
    agentic_tools.INBOX_SUMMARY_SPEC,
    agentic_tools.DATA_GAPS_SPEC,
    agentic_tools.TEAM_MEMBERS_SPEC,
    agentic_tools.BOARD_SPEC,
    agentic_tools.PROJECTS_SPEC,
]


def agentic_system(today: date) -> str:
    """loop.py의 bedrock+company+KG advertise 조합에 정확히 대응하는 시스템 프롬프트."""
    return _agentic_system_prompt(
        today,
        can_submit_candidate=False,
        mail_search=True,
        mail_compose=False,
        kg_relations=True,
        meta_tools=True,
        company_directory=True,
        ask_user=False,
        ask_user_marker=False,
    )


# structured_compile: 프로덕션 user assembly. 골든 input은 question만 고정하고 catalog/grounding은
# 런타임 검색물이라 없으므로, 프로덕션 헬퍼에 빈 입력을 넣어 "(no tables)/(no wiki grounding)"으로
# 조립한다(REPORT의 fidelity caveat 참고). dialect는 notion_rows structured store의 postgres.
_COMPILE_DIALECT = "postgresql"


# --------------------------------------------------------------------------- #
# 표면별 메시지 조립 — 전부 import한 프로덕션 프롬프트로 만든다.
# --------------------------------------------------------------------------- #
def _compile_messages(inp: dict) -> tuple[str, str]:
    system = COMPILE_SYSTEM.format(dialect=_COMPILE_DIALECT)
    user = (
        f"Question: {inp['question']}\n\n"
        f"Schema:\n{_render_catalog({})}\n\n"
        f"Business/wiki grounding:\n{_render_grounding([])}"
    )
    return system, user


def _route_messages(inp: dict) -> tuple[str, str]:
    return ROUTE_SYSTEM, f"Question: {inp['question']}"


def _intent_messages(inp: dict) -> tuple[str, str]:
    return _INTENT_SYSTEM, f"Question: {inp['question']}"


def _graph_messages(inp: dict) -> tuple[str, str]:
    return _BIND_SYSTEM, f"Question: {inp['question']}"


def _delegation_messages(inp: dict) -> tuple[str, str]:
    excerpt = (inp["text"] or "").strip()[:_EXTRACT_EXCERPT_MAX]
    return _EXTRACT_SYSTEM_PROMPT, excerpt


# email_draft_payload: 프로덕션 LLM 프롬프트가 없다. 계약 SoR인 EmailDraftPayload 스키마
# (import)에서 지시문을 직접 유도한다. 내 문장이 아니라 model_json_schema()가 계약을 말한다.
_EMAIL_PAYLOAD_SCHEMA_JSON = json.dumps(
    EmailDraftPayload.model_json_schema(), ensure_ascii=False, indent=2
)
_EMAIL_PAYLOAD_SYSTEM = (
    "당신은 회사 비서의 이메일 초안 도구(submit_email_draft)가 받는 EmailDraftPayload를 만든다. "
    "아래 JSON 스키마를 정확히 따르는 JSON 객체 하나만 출력한다. 스키마에 정의된 키 외에는 "
    "어떤 키도 추가하지 마라(스키마 extra 금지 — SMTP/발송/provider API 키 등 금지). "
    "필수 키는 recipient_hint, subject_hint, body_template 이다.\n\n"
    "JSON Schema:\n" + _EMAIL_PAYLOAD_SCHEMA_JSON
)


def _email_payload_messages(inp: dict) -> tuple[str, str]:
    user = (
        f"받는 사람: {inp['recipient']}\n"
        f"요청: {inp['instruction']}\n\n"
        "위 요청에 맞는 EmailDraftPayload JSON 객체를 출력하라."
    )
    return _EMAIL_PAYLOAD_SYSTEM, user


# 단순 (system, user, json_only=True) chat 표면들.
_CHAT_BUILDERS: dict[str, Callable[[dict], tuple[str, str]]] = {
    "structured_compile": _compile_messages,
    "router_classify": _route_messages,
    "router_intent": _intent_messages,
    "graph_bind": _graph_messages,
    "delegation_extract": _delegation_messages,
    "email_draft_payload": _email_payload_messages,
}


# --------------------------------------------------------------------------- #
# 모델 엔드포인트 — 프로덕션 어댑터를 감싸 통일 인터페이스 제공.
# --------------------------------------------------------------------------- #
@dataclass
class Endpoint:
    slug: str
    family: str  # "openai" | "bedrock"
    adapter: Any  # 프로덕션 어댑터(WorkerChat / OpenAIChat / BedrockConverseChat)
    # openai-family tool-call용
    base_url: str = ""
    key: str = ""
    model: str = ""
    extra_body: dict = field(default_factory=dict)
    min_interval: float = 0.0
    timeout: float = 60.0
    # gpt-5.x: 벤더가 non-default temperature를 400으로 거부 → tool-call body에서 생략.
    send_temperature: bool = True
    # gpt-5.5(reasoning)는 `max_tokens`를 거부하고 `max_completion_tokens`만 받는데,
    # 작은 budget은 reasoning이 다 먹어 빈 content가 된다 → 캡 자체를 생략(None).
    tool_call_max_tokens: int | None = MAX_TOKENS
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last: float = 0.0

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self.adapter.complete(system, user, json_only=json_only)

    def tool_call(self, system: str, question: str, tools: list[dict]) -> tuple[list[dict], str]:
        """모델에게 tools를 주고 첫 어시스턴트 턴의 (tool_calls, text)를 정규화해 돌려준다.

        tool_calls = [{"name": str, "input": dict}, ...]; 호출이 없으면 빈 리스트.
        예외는 상위로 전파(에러로 기록)."""
        if self.family == "bedrock":
            return self._bedrock_tool_call(system, question, tools)
        return self._openai_tool_call(system, question, tools)

    def _openai_tool_call(
        self, system: str, question: str, tools: list[dict]
    ) -> tuple[list[dict], str]:
        oa_tools = [
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
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            "tools": oa_tools,
            "tool_choice": "auto",
        }
        if self.send_temperature:
            body["temperature"] = 0
        if self.tool_call_max_tokens is not None:
            body["max_tokens"] = self.tool_call_max_tokens
        # extra_body(EXAONE thinking off 등)는 max_tokens를 덮어쓰지 않게 뒤에 병합.
        for k, v in self.extra_body.items():
            body.setdefault(k, v)
        self._throttle()
        data = _post_json(self.base_url, "/chat/completions", self.key, body, self.timeout)
        msg = data["choices"][0]["message"]
        text = msg.get("content") or ""
        calls: list[dict] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or ""
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except (json.JSONDecodeError, TypeError):
                args = {"_raw_arguments": raw_args}
            calls.append({"name": name, "input": args})
        return calls, str(text)

    def _bedrock_tool_call(
        self, system: str, question: str, tools: list[dict]
    ) -> tuple[list[dict], str]:
        a = self.adapter
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": {"json": t["input_schema"]},
                    }
                }
                for t in tools
            ],
            "toolChoice": {"auto": {}},
        }
        body = {
            "messages": [{"role": "user", "content": [{"text": question}]}],
            "system": [{"text": system}],
            "inferenceConfig": {"maxTokens": a._max_tokens, "temperature": a._temperature},
            "toolConfig": tool_config,
        }
        data = _post_converse(a._base, a._model_id, a._key, body, timeout=a._timeout)
        message = (data.get("output") or {}).get("message") or {}
        content = message.get("content") or []
        text_parts = [b["text"] for b in content if isinstance(b, dict) and "text" in b]
        calls = [
            {"name": b["toolUse"].get("name", ""), "input": b["toolUse"].get("input") or {}}
            for b in content
            if isinstance(b, dict) and "toolUse" in b
        ]
        return calls, "\n".join(text_parts).strip()


# --------------------------------------------------------------------------- #
# email_draft: 프로덕션 함수 재사용 + chat model 캡처.
# --------------------------------------------------------------------------- #
_email_tls = threading.local()
_orig_get_chat_model_for = agent_service.get_chat_model_for


def _patched_get_chat_model_for(task):  # noqa: ANN001
    cap = getattr(_email_tls, "capture", None)
    return cap if cap is not None else _orig_get_chat_model_for(task)


agent_service.get_chat_model_for = _patched_get_chat_model_for


class _NullSpan:
    """audit span 대체(no-op). set_output/add_meta만 필요."""

    def set_output(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        pass

    def add_meta(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        pass


@contextmanager
def _null_audit(*_a, **_k):  # noqa: ANN002, ANN003
    yield _NullSpan()


# audit()는 audit_log에 INSERT하는데 이 하네스의 DB 연결은 read-only 트랜잭션이라 __enter__가
# 즉시 raise → _generate_command_email이 except로 빠져 LLM을 부르지 않고 결정론 템플릿을 낸다.
# audit는 순수 텔레메트리이고 프롬프트 조립(system/user)은 audit 블록 밖에서 끝나므로, capture
# 동안 audit를 no-op로 갈아 실제 프로덕션 message assembly 그대로 LLM에 도달시킨다(프롬프트 불변).
agent_service.audit = _null_audit


class _CapturingChat:
    """프로덕션 _generate_command_email이 부르는 chat model 자리에 끼어 raw completion을 잡는다.

    실제 message assembly는 프로덕션 함수가 하고, 이 래퍼는 그 함수가 조립한 (system, user)를
    그대로 받아 inner 어댑터로 호출한 뒤 raw를 저장한다(재작성 없음)."""

    def __init__(self, inner: Endpoint):
        self._inner = inner
        self.model_id = f"capture:{inner.slug}"
        self.raw: str | None = None
        self.error: Exception | None = None

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        try:
            out = self._inner.complete(system, user, json_only=json_only)
        except Exception as exc:  # noqa: BLE001 — 상위에서 error로 기록
            self.error = exc
            raise
        self.raw = out
        return out


def _run_email_draft(ep: Endpoint, inp: dict) -> str:
    cap = _CapturingChat(ep)
    _email_tls.capture = cap
    try:
        # 프로덕션 함수를 그대로 호출 → 실제 system/user assembly 사용. upstream_context=None.
        agent_service._generate_command_email(inp["recipient_hint"], inp["instruction"], None)
    except Exception:  # noqa: BLE001 — 함수 내부에서 대부분 삼켜지지만 방어적으로
        pass
    finally:
        _email_tls.capture = None
    if cap.raw is not None:
        return cap.raw
    if cap.error is not None:
        raise cap.error
    raise RuntimeError("email_draft: no completion captured")


# --------------------------------------------------------------------------- #
# 문항 1개 실행 → raw 문자열.
# --------------------------------------------------------------------------- #
def run_item(ep: Endpoint, item: dict) -> str:
    surface = item["surface"]
    inp = item["input"]
    if surface == "email_draft":
        return _run_email_draft(ep, inp)
    if surface == "agentic_tool_call":
        calls, text = ep.tool_call(
            agentic_system(date.today()), inp["question"], list(AGENTIC_TOOL_SPECS)
        )
        if calls:
            return json.dumps({"tool_calls": calls, "text": text}, ensure_ascii=False)
        # 툴을 안 부르고 산문으로 답한 경우 → 산문 문자열 그대로(scorer 계약 §입력형식)
        return text
    builder = _CHAT_BUILDERS[surface]
    system, user = builder(inp)
    return ep.complete(system, user, json_only=True)


# --------------------------------------------------------------------------- #
# 엔드포인트 빌드.
# --------------------------------------------------------------------------- #
_BEDROCK_MODEL_IDS = {
    "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    # Opus 4.6 runtime ID는 비정형이다: `-v1` 접미사에 date/`:0` 없음(`:0` 붙이면 400),
    # `us.` 프리픽스 포함 — _normalize_model_id의 _KNOWN_INFERENCE_PREFIXES 매치로 그대로 통과.
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    # Opus 4.8: `-v1`조차 없는 bare ID + temperature 지정 거부(gpt-5 계열 quirk).
    "claude-opus-4-8": "us.anthropic.claude-opus-4-8",
}


def bedrock_key() -> str:
    """Bedrock bearer token (ABSK...). Prod reads ORTHUS_LLM_BEDROCK_API_KEY (fallback
    ORTHUS_LLM_API_KEY). Empty string when the key is not configured anywhere."""
    s = get_settings()
    return (
        s.llm_bedrock_api_key
        or s.llm_api_key
        or os.environ.get("ORTHUS_LLM_BEDROCK_API_KEY", "")
        or os.environ.get("ORTHUS_LLM_API_KEY", "")
    ).strip()


def build_endpoint(slug: str) -> Endpoint:
    if slug in _BEDROCK_MODEL_IDS:
        s = get_settings()
        api_key = bedrock_key()
        region = s.llm_bedrock_region or "us-east-1"
        adapter = BedrockConverseChat(
            api_key=api_key,
            region=region,
            model_id=_BEDROCK_MODEL_IDS[slug],
            inference_prefix=s.llm_bedrock_inference_prefix,
            max_tokens=s.llm_bedrock_max_tokens,
            # Opus 4.8은 temperature 지정을 400으로 거부(gpt-5 계열 quirk) → 생략.
            temperature=None if slug == "claude-opus-4-8" else s.llm_bedrock_temperature,
            base_url=s.llm_bedrock_base_url or None,
        )
        return Endpoint(slug=slug, family="bedrock", adapter=adapter)

    if slug == "gpt-5.5":
        s = get_settings()
        base_url = s.llm_base_url or "https://api.openai.com/v1"
        key = (s.llm_api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        # gpt-5.5는 reasoning 모델: temperature는 default 외 전부 400이라 생략(None),
        # 토큰 캡은 `max_completion_tokens`만 받는데 작은 budget이면 reasoning이 다
        # 소진해 빈 content가 돼서 캡 자체를 보내지 않는다(§하네스 규칙의 "truncation을
        # 계약 위반으로 오인하지 않는다" 목적은 캡 생략으로 동일하게 달성).
        adapter = OpenAIChat(base_url, key, "gpt-5.5", temperature=None)
        return Endpoint(
            slug=slug,
            family="openai",
            adapter=adapter,
            base_url=base_url.rstrip("/"),
            key=key,
            model="gpt-5.5",
            extra_body={},
            send_temperature=False,
            tool_call_max_tokens=None,
        )

    if slug in ("deepseek-v4-pro", "glm-5.2"):
        # Reasoning models (gpt-5.5와 같은 규칙): 작은 max_tokens budget은 reasoning이
        # 다 소진해 빈 content가 된다(문서화된 GLM-5.2 비용 인시던트) → 캡 자체를 생략.
        # temperature=0은 양쪽 다 수용(E2E 하네스 arm에서 실측). base_url/env 이름은
        # harness_e2e.py의 `_build_deepseek_chat`/`_build_glm_chat`과 동일.
        if slug == "deepseek-v4-pro":
            key = os.environ.get("ORTHUS_LLM_DEEPSEEK_API_KEY", "").strip()
            base = os.environ.get(
                "ORTHUS_LLM_DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/")
        else:
            key = os.environ.get("ORTHUS_GLM_API_KEY", "").strip()
            base = "https://api.z.ai/api/paas/v4"
        if not key:
            raise ValueError(f"{slug}: vendor API key env required (see harness_e2e.py builders)")
        adapter = OpenAIChat(base, key, slug, timeout=180.0)
        return Endpoint(
            slug=slug,
            family="openai",
            adapter=adapter,
            base_url=base,
            key=key,
            model=slug,
            extra_body={},
            tool_call_max_tokens=None,  # reasoning model: no cap (empty-content trap)
            timeout=180.0,
        )

    if slug == "gpt-4o-mini":
        s = get_settings()
        base_url = s.llm_base_url or "https://api.openai.com/v1"
        # 프로덕션은 ORTHUS_LLM_API_KEY를 읽지만 이 환경엔 OPENAI_API_KEY로 노출돼 있어 폴백한다.
        key = (s.llm_api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        adapter = OpenAIChat(base_url, key, "gpt-4o-mini", extra_body={"max_tokens": MAX_TOKENS})
        return Endpoint(
            slug=slug,
            family="openai",
            adapter=adapter,
            base_url=base_url.rstrip("/"),
            key=key,
            model="gpt-4o-mini",
            extra_body={},
        )

    # solar / ax / exaone — 프로덕션 vendor 배선(pool.build_pool).
    worker = pool.build_pool([slug])[slug]
    # 넉넉한 max_tokens 강제 + EXAONE reasoning 차단 보강(§하네스 규칙). 프로덕션 vendor 스펙은
    # enable_thinking=false만 두므로, budget을 reasoning으로 태우지 않도록 parse_reasoning도 끈다.
    extra = {**worker._spec.extra_body, "max_tokens": MAX_TOKENS}
    if slug == "exaone":
        extra["parse_reasoning"] = False
    worker._spec.extra_body = extra
    return Endpoint(
        slug=slug,
        family="openai",
        adapter=worker,
        base_url=worker._spec.base_url.rstrip("/"),
        key=worker._key,
        model=worker._model,
        extra_body=extra,
        min_interval=worker._spec.min_interval,
        timeout=worker._spec.timeout,
    )


# --------------------------------------------------------------------------- #
# 모델 1종 전체 실행.
# --------------------------------------------------------------------------- #
def preflight(ep: Endpoint) -> str | None:
    """모델 1콜 연결/인증 확인. 성공이면 None, 실패면 사유 문자열(과금·인증 결함 조기 발견)."""
    try:
        ep.complete("You output JSON only.", 'Question: 확인. Respond {"ok": true}', json_only=True)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:180]}"


def run_model(
    ep: Endpoint,
    items: list[dict],
    *,
    workers: int,
    bedrock_counter: list[int],
    bedrock_lock: threading.Lock,
) -> tuple[list[dict], int]:
    slug = ep.slug
    is_bedrock = ep.family == "bedrock"
    results: dict[str, dict] = {}
    errors = 0
    err_lock = threading.Lock()

    def work(item: dict) -> None:
        nonlocal errors
        if is_bedrock:
            with bedrock_lock:
                bedrock_counter[0] += 1
        try:
            raw = run_item(ep, item)
            rec = {"id": item["id"], "raw": raw}
        except Exception as exc:  # noqa: BLE001 — 절대 조용히 드롭하지 않음
            with err_lock:
                errors += 1
            rec = {
                "id": item["id"],
                "raw": "",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
        results[item["id"]] = rec

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, it) for it in items]
        for _ in as_completed(futs):
            pass
    elapsed = time.monotonic() - t0
    rows = [results[it["id"]] for it in items]
    print(
        f"  [{slug}] {len(rows)} items  errors={errors}  {elapsed:.1f}s  ({workers} workers)",
        flush=True,
    )
    return rows, errors


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
def load_items() -> list[dict]:
    return json.loads(GOLDEN_PATH.read_text("utf-8"))["items"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--models",
        default="solar,ax,exaone,gpt-4o-mini,claude-sonnet-4-6,claude-haiku-4-5",
        help="comma slugs to run",
    )
    ap.add_argument("--limit", type=int, default=0, help="cap items per model (0=all, debug)")
    ap.add_argument("--surfaces", default="", help="comma filter surfaces (debug)")
    ap.add_argument("--workers", type=int, default=8, help="openai-family concurrency")
    ap.add_argument("--bedrock-workers", type=int, default=4, help="bedrock concurrency")
    args = ap.parse_args()

    all_items = load_items()
    if args.surfaces:
        keep = set(args.surfaces.split(","))
        all_items = [it for it in all_items if it["surface"] in keep]

    slugs = [s.strip() for s in args.models.split(",") if s.strip()]
    bedrock_counter = [0]
    bedrock_lock = threading.Lock()
    summary: dict[str, dict] = {}

    skipped: dict[str, str] = {}
    for slug in slugs:
        # Bedrock 두 모델은 ABSK bearer token이 있어야 한다. 없으면 조용히 가짜 호출하지 않고 skip.
        if slug in _BEDROCK_MODEL_IDS and not bedrock_key():
            skipped[slug] = (
                "ORTHUS_LLM_BEDROCK_API_KEY not configured (empty in .env, absent in env)"
            )
            print(f"[skip] {slug}: {skipped[slug]}", flush=True)
            continue
        # Haiku는 C1(펜스) 40문항 전용 — 문서화된 펜스 실패 재현 최소셋(그 외 240은 돌리지 않음).
        if slug == "claude-haiku-4-5":
            items = [it for it in all_items if it["violation_type"] == "C1"]
        else:
            items = list(all_items)
        if args.limit:
            items = items[: args.limit]
        is_bedrock = slug in _BEDROCK_MODEL_IDS
        workers = args.bedrock_workers if is_bedrock else args.workers

        try:
            ep = build_endpoint(slug)
        except Exception as exc:  # noqa: BLE001
            skipped[slug] = f"build failed: {type(exc).__name__}: {str(exc)[:150]}"
            print(f"[skip] {slug}: {skipped[slug]}", flush=True)
            continue
        # 과금/인증 결함(예: OpenAI insufficient_quota, Bedrock 403)을 전량 실행 전에 잡아, 280개
        # 영구 에러 파일로 계약 실패를 위장하지 않는다. 성공한 모델만 raw 파일을 남긴다.
        pf = preflight(ep)
        if pf is not None:
            skipped[slug] = f"preflight failed: {pf}"
            print(f"[skip] {slug}: {skipped[slug]}", flush=True)
            continue

        print(f"[run] {slug}: {len(items)} items", flush=True)
        rows, errors = run_model(
            ep,
            items,
            workers=workers,
            bedrock_counter=bedrock_counter,
            bedrock_lock=bedrock_lock,
        )
        out = RAW_DIR / f"b2_{slug}.jsonl"
        write_jsonl(out, rows)
        summary[slug] = {"n": len(rows), "errors": errors, "path": str(out)}
        print(f"  -> {out}", flush=True)

    print("\n== run summary ==")
    for slug, s in summary.items():
        print(f"  {slug:<20} n={s['n']:<4} errors={s['errors']}")
    for slug, reason in skipped.items():
        print(f"  {slug:<20} SKIPPED — {reason}")
    print(f"\nEXACT Bedrock item-level converse invocations: {bedrock_counter[0]}")
    print(
        "  (one per bedrock item; transient 429/5xx re-POSTs inside the adapter are not "
        "separately counted)"
    )


if __name__ == "__main__":
    main()
