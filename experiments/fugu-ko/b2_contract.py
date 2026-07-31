"""B2 — 출력 계약 이행(Contract Compliance) 채점기.

사전선언: `experiments/fugu-ko/analysis/b2-prereg.md` (§3.1 위반 7종, §3.2 지표,
§3.3 주판정, §6 실행 메모). 골든: `golden/b2_contract.json` (7종 x 40 = 280).

이 파일이 하는 일
------------------
1. **두 파서를 항상 나란히 돌린다.**
   - `strict_pass`  = 현행 프로덕션 파서가 계약대로 된 값을 얻는가.
     (`json.loads(raw)` → surface별 키/타입/enum/allowlist/verbatim 검사. 프로덕션이
     조용히 fallback/coercion 하는 자리 — 예: route enum 이탈 → "wiki" 강제,
     delegation mode 이탈 → 기본값 — 는 **계약 미이행**으로 센다. 그 분기를 밟았다는
     것이 곧 모델이 계약을 안 지켰다는 뜻이므로, 그 사실은 `silent_coercion`
     플래그로 별도 기록한다.)
   - `lenient_pass` = **봉투(envelope)만** 고쳐서 통과하는가. 펜스 스트립 + 산문
     껍데기 제거 + JSON 문법 복구까지만 한다. **내용(content)은 절대 고치지 않는다** —
     enum 대소문자 교정, 누락 필드 채우기, allowlist 위반 키 드롭, 손상된 복사 토큰
     복원은 lenient 에서도 하지 않는다. 그래야 `recovery = lenient - strict` 가
     "배선 한 줄로 살아나는 정도"라는 원래 의미를 유지한다.
   - `recovery`는 항상 두 값에서 파생된다(§3.2).
2. **결정론 위반 분류기(LLM 호출 0회).** 규칙은 `_classify()` 한 곳에만 있고
   순서가 고정돼 있다. 어느 규칙에도 안 걸리면 `unclassified` 로 남기고 사유별
   비율을 보고한다(§6 — 조용히 어느 버킷에 넣지 않는다).
3. **주판정(§3.3).** 모델 간 `strict_pass` 분산 vs 모델 간 145문항 정확도 분산의
   분산비를 bootstrap 10,000 / seed 1234 로 CI 추정하고 사전선언된 문안을 찍는다.

구조적 강제
------------
strict 한쪽만 실린 표는 사전선언 §4.1 위반이다. 그래서 모든 집계 값은
`PassPair(strict, lenient)` 로만 존재하고(단일 float 필드 자체가 없다),
직렬화는 `PassPair.as_dict()` 를 거치며, `assert_both_reported()` 가 최종 문서를
훑어 strict/lenient 짝이 깨진 곳이 하나라도 있으면 예외를 던진다.

입력 형식
----------
모델 출력은 JSONL 한 줄에 한 문항:  `{"id": "c1-01", "raw": "<모델 원문>"}`
`agentic_tool_call` surface 는 tool 사용 턴이 있으므로 raw 에 하네스가 정규화한
`{"tool_calls": [{"name": ..., "input": {...}}], "text": "..."}` JSON 을 넣는다
(툴을 안 부르고 산문으로 답했으면 그냥 그 산문 문자열).

사용
-----
    python experiments/fugu-ko/b2_contract.py selfcheck
    python experiments/fugu-ko/b2_contract.py score \\
        --outputs solar=raw/b2_solar.jsonl --outputs exaone=raw/b2_exaone.jsonl \\
        --accuracy analysis/raw/b2_accuracy.json --out analysis/raw/b2_summary.json

`--accuracy` 는 기존 145문항 E2E 정확도(§3.3에서 재사용) `{"solar": 0.62, ...}`.

테스트 위치: 이 저장소의 `tests/` 는 실험 스크립트를 import 하는 패턴이 없다
(`tests/unit/test_mail_delegation_prefilter.py` 처럼 골든 **데이터**만 읽는다).
그래서 분류기 회귀는 `tests/` 대신 이 파일의 `selfcheck` 서브커맨드에 픽스처로 둔다.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "golden" / "b2_contract.json"

# --------------------------------------------------------------------------- #
# Surface contracts — SoR for the structural half of the contract. The golden
# file carries only `surface` + `input` (+ per-item copy_tokens / tool_allowlist)
# so 280 rows can never drift from the production schema.
#
# Every enum/keyset below is copied from the production module named in
# `prompt_ref` / `parser_ref` on each golden item.
# --------------------------------------------------------------------------- #
ROUTE_ENUM = ("structured", "wiki", "graph")
# orthus/router/route.py::COMMAND_FAMILIES + the 3 read routes = the 7-way (8-label) enum.
COMMAND_FAMILIES = (
    "connector_sync",
    "email_send",
    "document_draft",
    "personal_board_cleanup",
    "central_wiki_task_cleanup",
)
INTENT_ENUM = ROUTE_ENUM + COMMAND_FAMILIES
GRAPH_INTENT_ENUM = ("relation", "conflict", "provenance", "entity")
DELEGATION_MODES = ("code", "knowledge")
DELEGATION_RUNNERS = ("claude", "codex", "hermes")
# orthus/schemas/canonical.py::EmailDraftPayload — model_config extra="forbid" (P3.4d).
EMAIL_DRAFT_ALLOWED_KEYS = frozenset(
    {
        "recipient_hint",
        "subject_hint",
        "body_template",
        "intent",
        "source",
        "status",
        "llm_drafted",
        "used_for_outcome",
    }
)
EMAIL_DRAFT_PINNED = {
    "intent": "draft_for_review",
    "source": "assistant_command_template",
    "status": "draft",
    "used_for_outcome": False,
}

VIOLATION_TYPES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")
VIOLATION_LABEL = {
    "C1": "markdown fence wrapping",
    "C2": "JSON schema deviation",
    "C3": "enum escape",
    "C4": "field-allowlist violation",
    "C5": "empty response / prose contamination",
    "C6": "tool-call instruction ignored",
    "C7": "value/name copy corruption",
}


class Defect(NamedTuple):
    """One contract violation observed in a parsed output object."""

    code: str  # C2 | C3 | C4 | C7
    detail: str


@dataclass(frozen=True)
class SurfaceContract:
    """Structural contract of one production prompt surface."""

    name: str
    required: dict[str, str]  # key -> type token
    enum_field: str | None = None
    enum_values: tuple[str, ...] = ()
    allowed_keys: frozenset[str] | None = None  # None = production tolerates extras
    pinned: dict[str, Any] = field(default_factory=dict)
    tool_call: bool = False
    copy_fields: tuple[str, ...] = ()  # where verbatim copy tokens must land
    extra_check: Callable[[dict], list[Defect]] | None = None


def _type_ok(value: Any, token: str) -> bool:
    if token == "string":
        return isinstance(value, str)
    if token == "string_nonempty":
        return isinstance(value, str) and bool(value.strip())
    if token == "bool":
        return isinstance(value, bool)
    if token == "list_str_nonempty":
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(s, str) and s.strip() for s in value)
        )
    if token == "any":
        return True
    raise ValueError(f"unknown type token: {token}")


def _delegation_extra(obj: dict) -> list[Defect]:
    """delegation_extract: conditional schema + two coerced enums.

    `orthus/agentwork/delegation.py::extract_delegation_intent` requires
    `is_delegation is True` + a non-empty `instruction` before it returns anything;
    `mode`/`runner` outside the enum are silently coerced to the conservative
    defaults, which is exactly the C3 case the prereg wants counted, not hidden.
    """
    defects: list[Defect] = []
    if obj.get("is_delegation") is True:
        if not _type_ok(obj.get("instruction"), "string_nonempty"):
            defects.append(Defect("C2", "is_delegation=true but instruction missing/empty"))
        if not isinstance(obj.get("assignee", ""), str):
            defects.append(Defect("C2", "assignee is not a string"))
    for key, allowed in (("mode", DELEGATION_MODES), ("runner", DELEGATION_RUNNERS)):
        val = obj.get(key)
        if val in (None, ""):
            continue  # prompt explicitly allows leaving these blank
        if not isinstance(val, str):
            defects.append(Defect("C2", f"{key} is not a string: {val!r}"))
        elif val.strip().lower() not in allowed:
            defects.append(Defect("C3", f"{key}={val!r} outside {allowed}"))
    return defects


def _email_payload_extra(obj: dict) -> list[Defect]:
    """EmailDraftPayload literal fields are pinned by the schema (P3.4d)."""
    defects: list[Defect] = []
    for key, want in EMAIL_DRAFT_PINNED.items():
        if key in obj and obj[key] != want:
            defects.append(Defect("C2", f"{key}={obj[key]!r} != pinned {want!r}"))
    return defects


SURFACE_CONTRACTS: dict[str, SurfaceContract] = {
    "router_classify": SurfaceContract(
        name="router_classify",
        required={"route": "string_nonempty"},
        enum_field="route",
        enum_values=ROUTE_ENUM,
    ),
    "router_intent": SurfaceContract(
        name="router_intent",
        required={"intent": "string_nonempty"},
        enum_field="intent",
        enum_values=INTENT_ENUM,
    ),
    "structured_compile": SurfaceContract(
        name="structured_compile",
        required={"sql": "string_nonempty"},
        copy_fields=("sql",),
    ),
    "graph_bind": SurfaceContract(
        name="graph_bind",
        required={"intent": "string_nonempty", "subjects": "list_str_nonempty"},
        enum_field="intent",
        enum_values=GRAPH_INTENT_ENUM,
        copy_fields=("subjects",),
    ),
    "delegation_extract": SurfaceContract(
        name="delegation_extract",
        required={"is_delegation": "bool"},
        copy_fields=("assignee",),
        extra_check=_delegation_extra,
    ),
    "email_draft": SurfaceContract(
        name="email_draft",
        required={"subject": "string_nonempty", "body": "string_nonempty"},
        copy_fields=("subject", "body"),
    ),
    "email_draft_payload": SurfaceContract(
        name="email_draft_payload",
        required={
            "recipient_hint": "string_nonempty",
            "subject_hint": "string_nonempty",
            "body_template": "string_nonempty",
        },
        allowed_keys=EMAIL_DRAFT_ALLOWED_KEYS,
        extra_check=_email_payload_extra,
    ),
    "agentic_tool_call": SurfaceContract(
        name="agentic_tool_call",
        required={},
        tool_call=True,
    ),
}

# The production parsers that silently swallow a contract miss instead of raising.
SILENT_COERCION_SURFACES = {
    "router_classify": "invalid route -> 'wiki'",
    "router_intent": "invalid intent -> 'wiki'",
    "delegation_extract": "invalid mode/runner -> knowledge/codex",
    "email_draft": "unparseable -> deterministic template",
}


# --------------------------------------------------------------------------- #
# Envelope handling — strict parse, and the lenient repair ladder.
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(
    r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)?```",
    re.DOTALL,
)
_UNCLOSED_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(?P<body>.*)\Z", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SMART_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'"}


def has_fence(raw: str) -> bool:
    return "```" in (raw or "")


def strip_fences(raw: str) -> str:
    """Return the first fenced block's body, else the input unchanged."""
    text = raw or ""
    m = _FENCE_RE.search(text)
    if m is not None:
        return m.group("body")
    m = _UNCLOSED_FENCE_RE.search(text)
    if m is not None:
        return m.group("body")
    return text


def extract_json_span(text: str) -> str | None:
    """Balanced-brace extraction of the first top-level JSON object/array."""
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def repair_json_syntax(text: str) -> str:
    out = text
    for bad, good in _SMART_QUOTES.items():
        out = out.replace(bad, good)
    out = _TRAILING_COMMA_RE.sub(r"\1", out)
    out = re.sub(r"\bTrue\b", "true", out)
    out = re.sub(r"\bFalse\b", "false", out)
    out = re.sub(r"\bNone\b", "null", out)
    return out


def strict_parse(raw: str) -> Any | None:
    """Exactly what production does: `json.loads` on the raw completion."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


class Recovery(NamedTuple):
    obj: Any | None
    repairs: tuple[str, ...]


def lenient_parse(raw: str) -> Recovery:
    """Envelope-only repair ladder. Never edits parsed CONTENT."""
    text = raw if isinstance(raw, str) else ""
    obj = strict_parse(text)
    if obj is not None:
        return Recovery(obj, ())

    repairs: list[str] = []
    if has_fence(text):
        text = strip_fences(text)
        repairs.append("fence_strip")
        obj = strict_parse(text)
        if obj is not None:
            return Recovery(obj, tuple(repairs))

    span = extract_json_span(text)
    if span is not None and span.strip() != text.strip():
        repairs.append("prose_trim")
        obj = strict_parse(span)
        if obj is not None:
            return Recovery(obj, tuple(repairs))
        text = span
    elif span is not None:
        text = span

    repaired = repair_json_syntax(text)
    if repaired != text:
        obj = strict_parse(repaired)
        if obj is not None:
            repairs.append("syntax_repair")
            return Recovery(obj, tuple(repairs))
    return Recovery(None, tuple(repairs))


# --------------------------------------------------------------------------- #
# Tool-call detection (agentic surface)
# --------------------------------------------------------------------------- #
def extract_tool_calls(raw: str) -> list[dict] | None:
    """Return the normalized tool calls in a harness-recorded turn, else None."""
    obj = strict_parse(raw)
    if obj is None:
        obj = lenient_parse(raw).obj
    if not isinstance(obj, dict):
        return None
    calls = obj.get("tool_calls")
    if isinstance(calls, list):
        return [c for c in calls if isinstance(c, dict)]
    # single-call shape
    if isinstance(obj.get("tool_call"), dict):
        return [obj["tool_call"]]
    if isinstance(obj.get("name"), str) and ("input" in obj or "arguments" in obj):
        return [obj]
    return None


# --------------------------------------------------------------------------- #
# Contract checks over a parsed object
# --------------------------------------------------------------------------- #
def _copy_targets(obj: dict, contract: SurfaceContract, item: dict) -> str:
    fields = item.get("copy_field")
    if isinstance(fields, str) and fields:
        keys = [k for k in fields.replace("+", " ").split() if k]
    else:
        keys = list(contract.copy_fields)
    chunks: list[str] = []
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str):
            chunks.append(val)
        elif isinstance(val, list):
            chunks.extend(str(v) for v in val)
        elif val is not None:
            chunks.append(str(val))
    return "\n".join(chunks)


def check_contract(obj: Any, item: dict) -> list[Defect]:
    """All non-envelope contract defects of a parsed output. Order-independent."""
    contract = SURFACE_CONTRACTS[item["surface"]]
    if contract.tool_call:
        return []  # tool surfaces are judged by evaluate(), not by object shape
    if not isinstance(obj, dict):
        return [Defect("C2", f"top level is {type(obj).__name__}, expected object")]

    defects: list[Defect] = []
    if contract.allowed_keys is not None:
        extra = sorted(set(obj) - contract.allowed_keys)
        if extra:
            defects.append(Defect("C4", f"keys outside allowlist: {extra}"))
    for key, token in contract.required.items():
        if key not in obj:
            defects.append(Defect("C2", f"missing required key {key!r}"))
        elif not _type_ok(obj[key], token):
            defects.append(Defect("C2", f"{key} has wrong type/empty: {obj[key]!r}"))
    if contract.enum_field and contract.enum_field in obj:
        val = obj[contract.enum_field]
        if isinstance(val, str) and val not in contract.enum_values:
            defects.append(
                Defect("C3", f"{contract.enum_field}={val!r} outside {contract.enum_values}")
            )
    if contract.extra_check is not None:
        defects.extend(contract.extra_check(obj))

    tokens = item.get("copy_tokens") or []
    if tokens:
        haystack = _copy_targets(obj, contract, item)
        missing = [t for t in tokens if t not in haystack]
        if missing:
            defects.append(Defect("C7", f"copy tokens not reproduced verbatim: {missing}"))
    return defects


# --------------------------------------------------------------------------- #
# Per-item evaluation + the deterministic violation classifier
# --------------------------------------------------------------------------- #
#
# CLASSIFIER RULES (ordered; first match wins; zero LLM calls)
#
#   R0  strict_pass                                    -> clean
#   R1  raw is empty / whitespace only                 -> C5
#   R2  tool surface and no tool call in the output    -> C6
#   R3  tool surface, tool call present, name not in
#       the item's tool_allowlist                      -> C6
#   R4  fence present AND the fence body alone
#       satisfies the whole contract                   -> C1
#   R5  nothing JSON-shaped recoverable at all         -> C5 if the text is prose
#                                                         on a JSON surface,
#                                                         else unclassified
#   R6  recovered object has keys outside an
#       allowlisted surface                            -> C4
#   R7  recovered object has an out-of-enum value      -> C3
#   R8  recovered object misses a required key or has
#       a wrong type                                   -> C2
#   R9  recovered object drops/alters a copy token     -> C7
#   R10 recovered only via prose_trim (contract OK)    -> C5
#   R11 recovered only via syntax_repair (contract OK) -> unclassified
#                                                         (json_syntax_repair —
#                                                          not one of the 7 types)
#   R12 anything else                                  -> unclassified
#
# `fence_present` is recorded on EVERY row independently of the class, so a
# fenced-and-also-schema-broken output still shows up in the fence rate even
# though R6-R9 claim its class.
# --------------------------------------------------------------------------- #
_HANGUL_OR_LATIN = re.compile(r"[가-힣A-Za-z]")


def evaluate(item: dict, raw: str) -> dict:
    """Score one output. Always returns strict AND lenient (never one alone)."""
    surface = item["surface"]
    contract = SURFACE_CONTRACTS[surface]
    text = raw if isinstance(raw, str) else ""

    if contract.tool_call:
        calls = extract_tool_calls(text)
        allow = item.get("tool_allowlist") or []
        names = [c.get("name") for c in (calls or [])]
        ok = bool(calls) and (not allow or any(n in allow for n in names))
        # A tool surface has no envelope to repair: calling the tool or not is
        # the whole contract, so strict and lenient are equal BY CONSTRUCTION —
        # and are still reported as a pair (§4.1).
        result = {
            "id": item["id"],
            "surface": surface,
            "probe_type": item["violation_type"],
            "strict_pass": ok,
            "lenient_pass": ok,
            "fence_present": has_fence(text),
            "repairs": [],
            "silent_coercion": False,
            "defects": [] if ok else [["C6", f"tool_calls={names or 'none'} allow={allow}"]],
        }
        result["violation"], result["unclassified_reason"] = _classify(item, text, result, None)
        return result

    strict_obj = strict_parse(text)
    strict_defects = check_contract(strict_obj, item) if strict_obj is not None else None
    strict_pass = strict_obj is not None and not strict_defects

    recovery = lenient_parse(text)
    lenient_defects = check_contract(recovery.obj, item) if recovery.obj is not None else None
    lenient_pass = recovery.obj is not None and not lenient_defects

    defects = lenient_defects if lenient_defects is not None else (strict_defects or [])
    silent = (not strict_pass) and surface in SILENT_COERCION_SURFACES
    result = {
        "id": item["id"],
        "surface": surface,
        "probe_type": item["violation_type"],
        "strict_pass": strict_pass,
        "lenient_pass": lenient_pass,
        "fence_present": has_fence(text),
        "repairs": list(recovery.repairs),
        "silent_coercion": silent,
        "defects": [[d.code, d.detail] for d in defects],
    }
    result["violation"], result["unclassified_reason"] = _classify(item, text, result, recovery)
    return result


def _classify(
    item: dict, raw: str, result: dict, recovery: Recovery | None
) -> tuple[str, str | None]:
    contract = SURFACE_CONTRACTS[item["surface"]]

    if result["strict_pass"]:  # R0
        return "clean", None
    if not (raw or "").strip():  # R1
        return "C5", None
    if contract.tool_call:  # R2 / R3
        return "C6", None

    if has_fence(raw):  # R4
        body = strip_fences(raw)
        obj = strict_parse(body)
        if obj is not None and not check_contract(obj, item):
            return "C1", None

    assert recovery is not None
    if recovery.obj is None:  # R5
        if _HANGUL_OR_LATIN.search(raw) and "{" not in raw:
            return "C5", None
        return "unclassified", "unrecoverable_output"

    codes = {d[0] for d in result["defects"]}
    for code in ("C4", "C3", "C2", "C7"):  # R6 - R9
        if code in codes:
            return code, None
    if "prose_trim" in recovery.repairs:  # R10
        return "C5", None
    if "syntax_repair" in recovery.repairs:  # R11
        return "unclassified", "json_syntax_repair"
    if "fence_strip" in recovery.repairs:
        # fence body needed a further repair to parse (e.g. fence + trailing prose)
        return "C1", None
    return "unclassified", "no_rule_matched"  # R12


# --------------------------------------------------------------------------- #
# Aggregation — strict/lenient are structurally inseparable.
# --------------------------------------------------------------------------- #
class PassPair(NamedTuple):
    """A rate that CANNOT be reported without its counterpart (prereg §4.1)."""

    strict: float
    lenient: float
    n: int

    @property
    def recovery(self) -> float:
        return self.lenient - self.strict

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "strict_pass": round(self.strict, 4),
            "lenient_pass": round(self.lenient, 4),
            "recovery": round(self.recovery, 4),
        }

    def render(self) -> str:
        return (
            f"strict {self.strict * 100:5.1f}%  lenient {self.lenient * 100:5.1f}%  "
            f"recovery {self.recovery * 100:+5.1f}pp  (n={self.n})"
        )


def pass_pair(rows: list[dict]) -> PassPair:
    n = len(rows)
    if n == 0:
        return PassPair(0.0, 0.0, 0)
    return PassPair(
        sum(1 for r in rows if r["strict_pass"]) / n,
        sum(1 for r in rows if r["lenient_pass"]) / n,
        n,
    )


def summarize_model(slug: str, rows: list[dict]) -> dict:
    by_type: dict[str, dict] = {}
    for vt in VIOLATION_TYPES:
        sub = [r for r in rows if r["probe_type"] == vt]
        by_type[vt] = {"label": VIOLATION_LABEL[vt], **pass_pair(sub).as_dict()}
    by_surface = {
        s: pass_pair([r for r in rows if r["surface"] == s]).as_dict()
        for s in sorted({r["surface"] for r in rows})
    }
    observed = Counter(r["violation"] for r in rows)
    unclassified = [r for r in rows if r["violation"] == "unclassified"]
    return {
        "model": slug,
        "overall": pass_pair(rows).as_dict(),
        "by_probe_type": by_type,
        "by_surface": by_surface,
        "observed_violations": {
            k: observed.get(k, 0) for k in ("clean", *VIOLATION_TYPES, "unclassified")
        },
        "unclassified_rate": round(len(unclassified) / len(rows), 4) if rows else 0.0,
        "unclassified_reasons": dict(Counter(r["unclassified_reason"] for r in unclassified)),
        "fence_rate": round(sum(1 for r in rows if r["fence_present"]) / len(rows), 4)
        if rows
        else 0.0,
        "silent_coercion_rate": round(sum(1 for r in rows if r["silent_coercion"]) / len(rows), 4)
        if rows
        else 0.0,
    }


def assert_both_reported(doc: Any, path: str = "$") -> None:
    """Fail loudly if any rate object lost its counterpart (prereg §4.1)."""
    if isinstance(doc, dict):
        has_strict = "strict_pass" in doc
        has_lenient = "lenient_pass" in doc
        if has_strict != has_lenient:
            raise AssertionError(
                f"prereg §4.1 violation at {path}: strict/lenient must be reported together "
                f"(strict_pass={has_strict}, lenient_pass={has_lenient})"
            )
        if has_strict and "recovery" not in doc and "id" not in doc:
            raise AssertionError(f"prereg §3.2 violation at {path}: recovery missing")
        for k, v in doc.items():
            assert_both_reported(v, f"{path}.{k}")
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            assert_both_reported(v, f"{path}[{i}]")


# --------------------------------------------------------------------------- #
# Main judgment (prereg §3.3): Var(strict_pass) vs Var(accuracy), bootstrap.
# --------------------------------------------------------------------------- #
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 1234


def variance_ratio_judgment(
    contract_rates: dict[str, float],
    accuracy_rates: dict[str, float],
    *,
    n_boot: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
    label: str = "strict_pass",
) -> dict:
    """Var_contract / Var_accuracy with a percentile bootstrap CI over MODELS.

    The resampling unit is the MODEL (the models are the sample whose spread the
    prereg asks about), and each resample draws PAIRED indices so a model's
    contract rate and its 145-item accuracy always travel together.
    """
    models = sorted(set(contract_rates) & set(accuracy_rates))
    if len(models) < 3:
        return {
            "label": label,
            "models": models,
            "n_models": len(models),
            "verdict": "insufficient_models",
            "note": "variance over <3 models is not estimable; run more models.",
        }
    c = [contract_rates[m] for m in models]
    a = [accuracy_rates[m] for m in models]
    var_c = statistics.variance(c)
    var_a = statistics.variance(a)
    point = (var_c / var_a) if var_a > 0 else None

    rng = random.Random(seed)
    ratios: list[float] = []
    degenerate = 0
    idx = range(len(models))
    for _ in range(n_boot):
        pick = [rng.choice(idx) for _ in idx]
        bc = [c[i] for i in pick]
        ba = [a[i] for i in pick]
        try:
            vc = statistics.variance(bc)
            va = statistics.variance(ba)
        except statistics.StatisticsError:  # pragma: no cover - len<2 impossible here
            degenerate += 1
            continue
        if va <= 0:
            degenerate += 1
            continue
        ratios.append(vc / va)

    ci_lo = ci_hi = None
    if ratios:
        ratios.sort()
        ci_lo = ratios[int(0.025 * (len(ratios) - 1))]
        ci_hi = ratios[int(0.975 * (len(ratios) - 1))]

    if ci_lo is None:
        verdict, statement = "undetermined", "부트스트랩 표본이 전부 퇴화해 CI를 낼 수 없다."
    elif ci_lo > 1:
        verdict = "contract_dominates"
        statement = "프로덕션에서는 계약 이행이 추론력보다 큰 변별 요인이다"
    elif ci_hi < 1:
        verdict = "accuracy_dominates"
        statement = "정확도가 더 큰 변별 요인이다 (주장과 반대 — 그대로 보고)"
    else:
        verdict = "comparable"
        statement = "두 축의 변별력이 비슷하다 — 주장 철회"

    return {
        "label": label,
        "models": models,
        "n_models": len(models),
        "var_contract": var_c,
        "var_accuracy": var_a,
        "variance_ratio": point,
        "bootstrap": {
            "n_resamples": n_boot,
            "seed": seed,
            "unit": "model (paired)",
            "effective_resamples": len(ratios),
            "degenerate_resamples": degenerate,
            "ci95": [ci_lo, ci_hi],
        },
        "verdict": verdict,
        "statement": statement,
    }


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    doc = json.loads(path.read_text("utf-8"))
    items = doc["items"]
    for it in items:
        if it["surface"] not in SURFACE_CONTRACTS:
            raise ValueError(f"{it['id']}: unknown surface {it['surface']!r}")
        if it["violation_type"] not in VIOLATION_TYPES:
            raise ValueError(f"{it['id']}: unknown violation_type {it['violation_type']!r}")
    return items


def load_outputs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        raw = rec.get("raw")
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False) if raw is not None else ""
        out[rec["id"]] = raw
    return out


def score_model(items: list[dict], outputs: dict[str, str]) -> list[dict]:
    return [evaluate(it, outputs.get(it["id"], "")) for it in items]


def build_report(
    items: list[dict],
    per_model_rows: dict[str, list[dict]],
    accuracy: dict[str, float],
) -> dict:
    summaries = {slug: summarize_model(slug, rows) for slug, rows in per_model_rows.items()}
    strict_rates = {slug: s["overall"]["strict_pass"] for slug, s in summaries.items()}
    lenient_rates = {slug: s["overall"]["lenient_pass"] for slug, s in summaries.items()}
    doc = {
        "benchmark": "B2 contract compliance",
        "prereg": "experiments/fugu-ko/analysis/b2-prereg.md",
        "n_items": len(items),
        "models": sorted(per_model_rows),
        "e2e_accuracy_input": accuracy,
        "per_model": summaries,
        "main_judgment": variance_ratio_judgment(strict_rates, accuracy, label="strict_pass"),
        "secondary_judgment": variance_ratio_judgment(
            lenient_rates, accuracy, label="lenient_pass"
        ),
        "biggest_recovery": sorted(
            ({"model": slug, **summaries[slug]["overall"]} for slug in summaries),
            key=lambda d: d["recovery"],
            reverse=True,
        )[:3],
    }
    assert_both_reported(doc)
    return doc


def print_report(doc: dict) -> None:
    print(f"== B2 contract compliance ==  n_items={doc['n_items']}  models={doc['models']}")
    print(f"{'model':<24}{'strict':>9}{'lenient':>10}{'recovery':>10}{'unclass':>9}{'fence':>8}")
    for slug in doc["models"]:
        s = doc["per_model"][slug]
        o = s["overall"]
        print(
            f"{slug:<24}{o['strict_pass'] * 100:8.1f}%{o['lenient_pass'] * 100:9.1f}%"
            f"{o['recovery'] * 100:+9.1f}p{s['unclassified_rate'] * 100:8.1f}%"
            f"{s['fence_rate'] * 100:7.1f}%"
        )
    print("\n-- per violation type (strict / lenient) --")
    header = "type  " + "".join(f"{m[:14]:>18}" for m in doc["models"])
    print(header)
    for vt in VIOLATION_TYPES:
        cells = ""
        for slug in doc["models"]:
            t = doc["per_model"][slug]["by_probe_type"][vt]
            cells += f"{t['strict_pass'] * 100:8.1f}/{t['lenient_pass'] * 100:<9.1f}"
        print(f"{vt:<6}{cells}")
    for key in ("main_judgment", "secondary_judgment"):
        j = doc[key]
        print(f"\n-- {key} ({j['label']}) --")
        if j.get("verdict") == "insufficient_models":
            print(f"  {j['note']}")
            continue
        ci = j["bootstrap"]["ci95"]
        print(
            f"  Var_contract={j['var_contract']:.6f}  Var_accuracy={j['var_accuracy']:.6f}  "
            f"ratio={j['variance_ratio']}"
        )
        print(
            f"  bootstrap n={j['bootstrap']['n_resamples']} seed={j['bootstrap']['seed']} "
            f"CI95=[{ci[0]}, {ci[1]}] degenerate={j['bootstrap']['degenerate_resamples']}"
        )
        print(f"  verdict={j['verdict']}: {j['statement']}")


# --------------------------------------------------------------------------- #
# selfcheck fixtures — the classifier regression set (no pytest pattern exists
# for experiment-local modules in this repo; see module docstring).
# --------------------------------------------------------------------------- #
def _fixtures(items: list[dict]) -> list[tuple[str, str, str, bool, bool]]:
    """(item_id, raw, expected_violation, expect_strict, expect_lenient)."""
    by_surface: dict[str, dict] = {}
    for it in items:
        by_surface.setdefault(it["surface"], it)

    def pick(surface: str, need_copy: bool = False, vt: str | None = None) -> dict:
        for it in items:
            if it["surface"] != surface:
                continue
            if need_copy and not it.get("copy_tokens"):
                continue
            if vt and it["violation_type"] != vt:
                continue
            return it
        raise KeyError(f"no golden item for surface={surface} copy={need_copy}")

    sc = pick("structured_compile")
    rc = pick("router_classify")
    ri = pick("router_intent")
    gb = pick("graph_bind")
    de = pick("delegation_extract")
    ed = pick("email_draft")
    ep = pick("email_draft_payload")
    tc = pick("agentic_tool_call")
    sc_copy = pick("structured_compile", need_copy=True)
    gb_copy = pick("graph_bind", need_copy=True)
    sql = "SELECT count(*) FROM notion_rows WHERE db_name = '협업업무표'"
    good_payload = {
        "recipient_hint": "김대표",
        "subject_hint": "정기 회의 일정 안내",
        "body_template": "안녕하세요, 다음 주 정기 회의 일정을 안내드립니다.",
    }
    copy_tok = sc_copy["copy_tokens"][0]

    return [
        # -- clean --------------------------------------------------------- #
        (sc["id"], json.dumps({"sql": sql}, ensure_ascii=False), "clean", True, True),
        (rc["id"], '{"route": "wiki"}', "clean", True, True),
        (ri["id"], '{"intent": "connector_sync"}', "clean", True, True),
        (
            gb["id"],
            json.dumps({"intent": "relation", "subjects": ["nova", "orbit"]}, ensure_ascii=False),
            "clean",
            True,
            True,
        ),
        (
            de["id"],
            json.dumps(
                {
                    "is_delegation": True,
                    "assignee": "김철수",
                    "mode": "code",
                    "runner": "codex",
                    "instruction": "배포 스크립트 타임아웃 수정",
                },
                ensure_ascii=False,
            ),
            "clean",
            True,
            True,
        ),
        (
            ed["id"],
            json.dumps({"subject": "회의 일정 안내", "body": "안녕하세요."}, ensure_ascii=False),
            "clean",
            True,
            True,
        ),
        (ep["id"], json.dumps(good_payload, ensure_ascii=False), "clean", True, True),
        (
            tc["id"],
            json.dumps(
                {"tool_calls": [{"name": tc["tool_allowlist"][0], "input": {}}], "text": ""},
                ensure_ascii=False,
            ),
            "clean",
            True,
            True,
        ),
        # -- C1 markdown fence --------------------------------------------- #
        (
            sc["id"],
            "```json\n" + json.dumps({"sql": sql}, ensure_ascii=False) + "\n```",
            "C1",
            False,
            True,
        ),
        (rc["id"], '```json\n{"route": "structured"}\n```', "C1", False, True),
        (ri["id"], '```\n{"intent": "wiki"}\n```', "C1", False, True),
        (
            gb["id"],
            '```json\n{"intent": "conflict", "subjects": ["배포 공지"]}\n```',
            "C1",
            False,
            True,
        ),
        # unterminated fence (the Haiku shape when the response is truncated)
        (rc["id"], '```json\n{"route": "graph"}', "C1", False, True),
        # -- C2 schema deviation ------------------------------------------- #
        (gb["id"], '{"intent": "relation"}', "C2", False, False),  # subjects missing
        (gb["id"], '{"intent": "relation", "subjects": "nova, orbit"}', "C2", False, False),
        (ed["id"], '{"subject": "회의 일정 안내"}', "C2", False, False),  # body missing
        (sc["id"], '{"query": "SELECT 1"}', "C2", False, False),  # wrong key name
        (de["id"], '{"is_delegation": "true", "instruction": "확인"}', "C2", False, False),
        (
            de["id"],
            '{"is_delegation": true, "assignee": "김철수", "instruction": ""}',
            "C2",
            False,
            False,
        ),
        # -- C3 enum escape ------------------------------------------------- #
        (rc["id"], '{"route": "database"}', "C3", False, False),
        (rc["id"], '{"route": "Wiki"}', "C3", False, False),  # casing is NOT repaired
        (ri["id"], '{"intent": "send_email"}', "C3", False, False),
        (gb["id"], '{"intent": "related", "subjects": ["nova"]}', "C3", False, False),
        (
            de["id"],
            json.dumps(
                {
                    "is_delegation": True,
                    "assignee": "김철수",
                    "mode": "research",
                    "runner": "codex",
                    "instruction": "조사",
                },
                ensure_ascii=False,
            ),
            "C3",
            False,
            False,
        ),
        # -- C4 allowlist violation (P3.4d) --------------------------------- #
        (
            ep["id"],
            json.dumps({**good_payload, "smtp_host": "smtp.gmail.com"}, ensure_ascii=False),
            "C4",
            False,
            False,
        ),
        (
            ep["id"],
            json.dumps({**good_payload, "send_now": True}, ensure_ascii=False),
            "C4",
            False,
            False,
        ),
        (
            ep["id"],
            json.dumps({**good_payload, "provider_api_key": "sk-xxx"}, ensure_ascii=False),
            "C4",
            False,
            False,
        ),
        # fenced AND extra key -> the substantive violation wins, fence still flagged
        (
            ep["id"],
            "```json\n" + json.dumps({**good_payload, "cc": "a@b.c"}, ensure_ascii=False) + "\n```",
            "C4",
            False,
            False,
        ),
        # -- C5 empty / prose contamination --------------------------------- #
        (sc["id"], "", "C5", False, False),
        (rc["id"], "   \n ", "C5", False, False),
        (
            sc["id"],
            "네, 아래와 같습니다.\n" + json.dumps({"sql": sql}, ensure_ascii=False),
            "C5",
            False,
            True,
        ),
        (
            rc["id"],
            '이 질문은 지식형 질문으로 보입니다. {"route": "wiki"} 로 라우팅하면 됩니다.',
            "C5",
            False,
            True,
        ),
        (ed["id"], "제목: 회의 일정 안내\n본문: 안녕하세요.", "C5", False, False),
        # -- C6 tool-call ignored -------------------------------------------- #
        (tc["id"], "이번 주 팀 일정은 화요일 회의와 목요일 배포가 있습니다.", "C6", False, False),
        (tc["id"], json.dumps({"text": "제가 알기로는 …"}, ensure_ascii=False), "C6", False, False),
        (
            tc["id"],
            json.dumps({"tool_calls": [{"name": "web_search", "input": {}}]}, ensure_ascii=False),
            "C6",
            False,
            False,
        ),
        # -- C7 copy corruption ---------------------------------------------- #
        (
            sc_copy["id"],
            json.dumps(
                {"sql": f"SELECT count(*) FROM notion_rows WHERE db_name = '{copy_tok}'"},
                ensure_ascii=False,
            ),
            "clean",
            True,
            True,
        ),
        (
            sc_copy["id"],
            json.dumps(
                {
                    "sql": "SELECT count(*) FROM notion_rows WHERE db_name = '"
                    + copy_tok.replace(" ", "").replace("✅", "")
                    + "'"
                },
                ensure_ascii=False,
            ),
            "C7",
            False,
            False,
        ),
        (
            gb_copy["id"],
            json.dumps(
                {
                    "intent": "relation",
                    "subjects": [t.replace(" ", "") for t in gb_copy["copy_tokens"]],
                },
                ensure_ascii=False,
            ),
            "C7",
            False,
            False,
        ),
        # -- unclassified (reported, never silently bucketed) ---------------- #
        (rc["id"], '{"route": "wiki",}', "unclassified", False, True),  # trailing comma
        (sc["id"], "!!!", "unclassified", False, False),
    ]


def selfcheck() -> int:
    items = load_golden()
    by_id = {it["id"]: it for it in items}
    counts = Counter(it["violation_type"] for it in items)
    print(f"golden: {len(items)} items  {dict(sorted(counts.items()))}")
    if len(items) != 280 or set(counts.values()) != {40}:
        print("FAIL: golden must be 7 types x 40 = 280")
        return 1

    failures = 0
    print(f"\n{'item':<10}{'expect':<14}{'got':<14}{'strict':<8}{'lenient':<9}{'fence':<7}{'ok'}")
    for item_id, raw, want, want_strict, want_lenient in _fixtures(items):
        res = evaluate(by_id[item_id], raw)
        ok = (
            res["violation"] == want
            and res["strict_pass"] is want_strict
            and res["lenient_pass"] is want_lenient
        )
        failures += 0 if ok else 1
        print(
            f"{item_id:<10}{want:<14}{res['violation']:<14}"
            f"{str(res['strict_pass']):<8}{str(res['lenient_pass']):<9}"
            f"{str(res['fence_present']):<7}{'PASS' if ok else 'FAIL'}"
        )
        if not ok:
            print(f"           detail: {res['defects']} repairs={res['repairs']}")

    # PassPair / report-shape invariants (prereg §4.1 must be structural).
    pair = PassPair(0.5, 0.8, 10)
    assert abs(pair.recovery - 0.3) < 1e-9
    assert set(pair.as_dict()) == {"n", "strict_pass", "lenient_pass", "recovery"}
    try:
        assert_both_reported({"overall": {"strict_pass": 0.5, "recovery": 0.1}})
    except AssertionError:
        pass
    else:
        failures += 1
        print("FAIL: assert_both_reported accepted a strict-only table")

    # main judgment shape on synthetic rates
    j = variance_ratio_judgment(
        {"a": 0.90, "b": 0.42, "c": 0.88, "d": 0.30},
        {"a": 0.71, "b": 0.70, "c": 0.69, "d": 0.72},
    )
    assert j["bootstrap"]["n_resamples"] == 10_000 and j["bootstrap"]["seed"] == 1234
    j2 = variance_ratio_judgment(
        {"a": 0.90, "b": 0.42, "c": 0.88, "d": 0.30},
        {"a": 0.71, "b": 0.70, "c": 0.69, "d": 0.72},
    )
    if j != j2:
        failures += 1
        print("FAIL: bootstrap is not reproducible under the pinned seed")
    print(
        f"\nbootstrap determinism: OK  (ratio={j['variance_ratio']:.2f} "
        f"CI95={[round(x, 2) for x in j['bootstrap']['ci95']]} verdict={j['verdict']})"
    )
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("score", help="score model outputs against the golden contract set")
    sc.add_argument(
        "--outputs",
        action="append",
        required=True,
        metavar="SLUG=PATH",
        help="model outputs JSONL ({id, raw}); repeatable",
    )
    sc.add_argument("--accuracy", required=True, help="JSON {model: 145-item E2E accuracy}")
    sc.add_argument("--golden", default=str(GOLDEN_PATH))
    sc.add_argument("--out", default="", help="write the full report JSON here")
    sc.add_argument("--rows-out", default="", help="write per-item rows JSONL here")

    sub.add_parser("selfcheck", help="run the deterministic classifier fixtures")
    args = ap.parse_args()

    if args.cmd == "selfcheck":
        raise SystemExit(selfcheck())

    items = load_golden(Path(args.golden))
    accuracy = json.loads(Path(args.accuracy).read_text("utf-8"))
    per_model: dict[str, list[dict]] = {}
    for spec in args.outputs:
        slug, _, path = spec.partition("=")
        if not path:
            raise SystemExit(f"--outputs expects SLUG=PATH (got {spec!r})")
        per_model[slug] = score_model(items, load_outputs(Path(path)))

    doc = build_report(items, per_model, accuracy)
    print_report(doc)
    if args.out:
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), "utf-8")
        print(f"\nreport -> {args.out}")
    if args.rows_out:
        with open(args.rows_out, "w", encoding="utf-8") as f:
            for slug, rows in per_model.items():
                for r in rows:
                    f.write(json.dumps({"model": slug, **r}, ensure_ascii=False) + "\n")
        print(f"rows   -> {args.rows_out}")


if __name__ == "__main__":
    main()
