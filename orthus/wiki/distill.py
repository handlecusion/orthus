"""Distill: corpus document -> WikiSource + list[WikiClaim].

The LLM extracts/compresses (AGENTS.md principle 1); deterministic code validates
the JSON, builds canonical models, and assigns stable slugs. No execution decisions
are delegated to the model."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from json import JSONDecodeError
from uuid import UUID

from sqlalchemy import select

from orthus.audit import audit
from orthus.db import session
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_DISTILL, get_chat_model_for
from orthus.schemas.canonical import ExtractedEntity, WikiClaim, WikiSource
from orthus.tables import documents
from orthus.wiki.slugs import kebab as _kebab
from orthus.wiki.task_hygiene import is_structured_row_source, is_structured_row_tautology
from orthus.wiki.slugs import source_slug_from_title

# entity 추출은 같은 distill JSON 호출에 얹는다(별도 호출 아님 — owner 결정,
# 구현 명세 §9.2). "distillation" 부분문자열은 MockChat matcher 폴백을 막으려
# 반드시 보존한다. claim 예산(_MAX_CLAIMS)을 굶기지 않도록 entity는 {kind,name}
# 콤팩트 리스트 + _MAX_ENTITIES cap으로 토큰 headroom을 둔다.
_SYSTEM = (
    "You are a knowledge distillation engine for an internal company wiki. "
    "Read the document and extract a structured summary plus atomic claims. "
    "Respond with a single JSON object and nothing else. Use the document's "
    "language for prose. Set each claim's page.slug to a concise English "
    "lowercase kebab-case identifier (only a-z, 0-9 and hyphens) naming the "
    "concept the claim belongs to: translate non-English concept names into "
    "English, and reuse the exact same slug for the same concept across "
    "different documents so related claims converge onto one wiki page. Keep "
    "page.title and all other prose in the document's language. "
    "Each claim must be a single, self-contained, verifiable "
    "statement with concrete evidence drawn from the document. Also give each "
    "claim a `headline`: a human-readable one-line title (at most ~120 "
    "characters, in the document's language, no `Q:`/`A:` prefixes) that "
    "summarizes the claim so it reads well as a knowledge-graph node label. "
    "Extract EVERY verifiable claim the document supports — do not stop early, "
    "and do not summarize several facts into one claim. Prefer more atomic claims "
    "over fewer compound ones. "
    "Return at most 20 claims. Return at most 3 open_questions, and only when a human answer "
    "would materially improve durable wiki knowledge. Do not ask about media "
    "content that is not visible in the text, file ownership/upload metadata, "
    "auth/login state, CI run IDs, or generic follow-up tasks. Also list named "
    "entities explicitly mentioned in the document as "
    '{"kind", "name"} objects, where kind is one of person, org, project, or '
    "system and name is the entity's surface form from the text; list at most "
    "16 entities and do not invent any."
)

_JSON_SHAPE = (
    '{"summary": str, "key_concepts": [str], "terminology": [str], '
    '"open_questions": [str], "claims": [{"claim": str, "headline": str, "evidence": str, '
    '"confidence": "high|medium|low", "page": {"slug": "english-kebab-concept-id", "title": str}, '
    '"conflicting": [str]}], '
    '"entities": [{"kind": "person|org|project|system", "name": str}]}'
)

_WS = re.compile(r"\s+")
_CONFIDENCE = {"high", "medium", "low"}
_JSON_RETRIES = 3
# 상한만 있고 하한 지시가 없던 프롬프트(_SYSTEM)가 문서당 클레임을 인위적으로
# 눌러 위키를 얇게 만들고 있었다(T14, docs/model-orchestration.md §13 — gpt-4o-mini
# 조차 25문서 중 14건에서 상한 8에 붙어 잘렸다). 프롬프트에 전수 추출 지시를 넣고
# 상한을 20으로 올려 함께 푼다. 프롬프트 상한과 이 코드 절단은 반드시 같은 값으로
# 유지해야 한다(하나만 풀면 다른 쪽이 자른다) — test_wiki_distill.py가 이를 고정한다.
_MAX_CLAIMS = 20
# entity 추출 필터 상수(구현 명세 §9.2 "저신뢰 추출 skip" — 계산 가능 재정의).
# persist엔 본문이 없으므로 본문이 손에 있는 distill 안에서 필터한다.
_ENTITY_KINDS = ("person", "org", "project", "system")
_MAX_ENTITIES = 16
_ENTITY_NAME_MIN_LEN = 2  # 한 글자 약어/잡음 floor
_ENTITY_MIN_MENTIONS = 2  # 정규화 본문에서 2회 미만 등장하면 저신뢰로 drop


@dataclass(frozen=True)
class DistillResult:
    """distill_document 반환 — 2-tuple→3-tuple positional 사고를 막는 dataclass
    (구현 명세 §9.2). entities는 추출만이며 persist는 serial write 단계가 한다."""

    source: WikiSource
    claims: list[WikiClaim]
    entities: list[ExtractedEntity] = field(default_factory=list)


def _norm_claim(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def _claim_slug(page_slug: str, claim_text: str) -> str:
    h = hashlib.sha256(_norm_claim(claim_text).encode("utf-8")).hexdigest()[:8]
    return f"{page_slug}-{h}"


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # drop opening fence line (``` or ```json) and trailing fence
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _coerce_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("slug", "claim", "title", "text", "description"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _coerce_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _coerce_text(item)
        if text and text not in out:
            out.append(text)
    return out


def _coerce_confidence(value: object) -> str:
    if not isinstance(value, str):
        return "medium"
    confidence = value.strip().lower()
    return confidence if confidence in _CONFIDENCE else "medium"


# KG 노드 라벨용 헤드라인 길이 캡(한 줄). 저장/투영은 이 값을 slug처럼 복사만 한다.
_HEADLINE_MAX = 120
# 앞부분에서 제거할 Q:/A: 노이즈 접두(대소문자·공백 관용). qa 계열 claim은 본문이
# "Q: … A: …" 형식이라 앞을 잘라도 지저분해서 결정론 폴백에서도 걷어낸다.
_QA_PREFIX = re.compile(r"^\s*(?:q|a|question|answer)\s*[:：]\s*", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?。？！]\s")
# 첫 문장 절단의 최소 길이 — 약어(Mr./e.g.)의 마침표+공백에서 조기 절단돼 라벨이
# 무의미한 조각("Mr.")이 되는 것을 막는다. 이 길이를 넘는 첫 종결부호에서만 끊는다.
_MIN_SENTENCE = 12
# 백필 등 단발 헤드라인 생성용 표준 system 프롬프트(distill 인라인 headline 지시와 규칙을
# 한 곳에서 관리 — 프롬프트 드리프트 방지).
HEADLINE_SYSTEM = (
    "You write a short human-readable headline for a single knowledge claim. "
    "Reply with ONE line only, no quotes, no `Q:`/`A:` prefixes, at most "
    f"{_HEADLINE_MAX} characters, in the claim's own language. The headline "
    "must summarize the claim so it reads well as a graph node label."
)


def _one_line_cap(text: str) -> str:
    """개행/연속 공백 collapse → _HEADLINE_MAX 캡(넘치면 말줄임표) → strip.

    LLM headline·결정론 폴백 양쪽이 공유하는 단일 한줄화/캡 규칙(드리프트 방지).
    종결부호 없는 긴 한국어 claim이 단어 중간에서 잘려도 '…'로 잘림을 표시한다."""
    one_line = _WS.sub(" ", text).strip()
    if len(one_line) <= _HEADLINE_MAX:
        return one_line
    return one_line[: _HEADLINE_MAX - 1].rstrip() + "…"


def _headline_fallback(text: str) -> str:
    """결정론 헤드라인 폴백(예외 없음, 순수 문자열 연산).

    Q:/A: 접두 제거 → 첫 문장 → 개행/연속 공백 collapse → _HEADLINE_MAX 캡(말줄임표).
    빈 결과면 원문 앞부분으로 폴백한다."""
    raw = text if isinstance(text, str) else ""
    stripped = _QA_PREFIX.sub("", raw).strip()
    # 첫 문장만 취하되 _MIN_SENTENCE를 넘는 첫 종결부호에서만 끊는다(약어 조기절단 방지).
    for m in _SENTENCE_END.finditer(stripped):
        if m.start() + 1 >= _MIN_SENTENCE:
            stripped = stripped[: m.start() + 1]
            break
    out = _one_line_cap(stripped)
    if out:
        return out
    return _one_line_cap(raw)


def _claim_headline(raw: object, claim_text: str) -> str:
    """LLM headline(raw)을 우선하되, 비면 claim_text에서 결정론 폴백을 만든다.

    LLM headline은 한 줄화 + _HEADLINE_MAX 캡만 하고, 폴백은 Q/A 제거까지 한다."""
    llm = _coerce_text(raw)
    if llm:
        one_line = _one_line_cap(llm)
        if one_line:
            return one_line
    return _headline_fallback(claim_text)


def _norm_for_count(text: str) -> str:
    """등장 횟수 비교용 정규화 — NFKC + casefold + 공백 collapse."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _extract_entities(value: object, markdown: str) -> list[ExtractedEntity]:
    """LLM `entities` 출력을 방어 파싱 + 저신뢰 skip(구현 명세 §9.2).

    drop 규칙: 결측/비-list → []; per-item 비-dict, kind allowlist 밖, name 비-str,
    name 길이 floor 미만, 정규화 본문 등장 < _ENTITY_MIN_MENTIONS. 정규화
    (kind, name) 기준 dedup 후 _MAX_ENTITIES cap. 정규화·충돌·관계는 persist가."""
    if not isinstance(value, list):
        return []
    norm_body = _norm_for_count(markdown)
    out: list[ExtractedEntity] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if len(out) >= _MAX_ENTITIES:
            break
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        name = item.get("name")
        if kind not in _ENTITY_KINDS or not isinstance(name, str):
            continue
        name = name.strip()
        if len(name) < _ENTITY_NAME_MIN_LEN:
            continue
        norm_name = _norm_for_count(name)
        if not norm_name or norm_body.count(norm_name) < _ENTITY_MIN_MENTIONS:
            continue
        key = (kind, norm_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(ExtractedEntity(kind=kind, name=name))
    return out


def _build_user_prompt(title: str, markdown: str) -> str:
    return (
        f"Document title: {title}\n\n"
        f"Document body:\n{markdown}\n\n"
        f"Return JSON exactly matching this shape:\n{_JSON_SHAPE}"
    )


def _complete_distill_json(chat: ChatModel, title: str, markdown: str, doc_id: UUID) -> dict:
    last_error: Exception | None = None
    for _ in range(_JSON_RETRIES):
        raw = chat.complete(_SYSTEM, _build_user_prompt(title, markdown), json_only=True)
        try:
            data = json.loads(_strip_fences(raw))
        except JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(data, dict):
            return data
        last_error = TypeError(f"distill JSON root must be object, got {type(data).__name__}")
    raise ValueError(f"wiki_distill_invalid_json:{doc_id}") from last_error


def distill_document(
    doc_id: UUID, user_id: UUID, *, chat_model: ChatModel | None = None
) -> DistillResult:
    """Extract a WikiSource + atomic WikiClaims (+ entity candidates) from one
    corpus document.

    Claims are deduped by (page-slug, normalized-claim-text) -> stable slug, so
    re-distilling the same document is idempotent. Entities are extraction-only
    (principle 1) — normalization/dedup/conflict/relations are deterministic in
    the serial write stage (`orthus/kg/entities.py::persist_entities`)."""
    chat = chat_model or get_chat_model_for(TASK_DISTILL)
    with audit("wiki.distill") as span:
        with session() as s:
            row = s.execute(
                select(
                    documents.c.title,
                    documents.c.markdown,
                    documents.c.source,
                    documents.c.source_db_name,
                ).where(documents.c.doc_id == doc_id)
            ).first()
        if row is None:
            raise ValueError(f"document not found: {doc_id}")
        title, markdown, doc_source, doc_source_db_name = row

        data = _complete_distill_json(chat, title, markdown, doc_id)

        source_slug = source_slug_from_title(title, doc_id)
        is_structured_row = is_structured_row_source(doc_source, doc_source_db_name)

        claims: list[WikiClaim] = []
        seen: set[str] = set()
        page_slugs: list[str] = []
        today = date.today()
        n_tautology_suppressed = 0

        summary = _coerce_text(data.get("summary"))

        raw_claims = data.get("claims", [])
        if not isinstance(raw_claims, list):
            raw_claims = []
        for c in raw_claims[:_MAX_CLAIMS]:
            if not isinstance(c, dict):
                continue
            page = c.get("page", {}) or {}
            if not isinstance(page, dict):
                page = {}
            page_slug = _kebab(
                _coerce_text(page.get("slug")) or _coerce_text(page.get("title")) or title
            )
            claim_text = _coerce_text(c.get("claim"))
            if not claim_text:
                continue
            # Structured DB rows (Notion DB rows, dashboard logs) generate
            # self-reference / bare-filler-adjective tautology claims from their
            # flattened "**Prop**: Value" render (docs/agent-chat-answer-quality.md
            # residual item). Narrative documents keep the exact same wording as
            # real claims — only structured-row sources go through this check.
            if is_structured_row and is_structured_row_tautology(claim_text):
                n_tautology_suppressed += 1
                continue
            slug = _claim_slug(page_slug, claim_text)
            if slug in seen:
                continue
            seen.add(slug)
            if page_slug not in page_slugs:
                page_slugs.append(page_slug)
            claims.append(
                WikiClaim(
                    slug=slug,
                    claim=claim_text,
                    supporting=[source_slug],
                    conflicting=_coerce_text_list(c.get("conflicting", [])),
                    confidence=_coerce_confidence(c.get("confidence")),
                    last_reviewed=today,
                    related_pages=[page_slug],
                    evidence=_coerce_text(c.get("evidence")),
                    display_title=_claim_headline(c.get("headline"), claim_text),
                )
            )

        # NO fallback claim. Zero extracted claims is the model telling us the document
        # holds no verifiable fact ("Extract EVERY verifiable claim" — _SYSTEM). Promoting
        # the `summary` to a claim in that case does not preserve knowledge; the summary is
        # a description OF the document, so it manufactures exactly the meta page that then
        # answers the user's question. Measured on the live company wiki (2026-07-17): all
        # 23 fallback claims were meta with no exception —
        #   "AI 영상관련툴에 대한 순위를 정리한 문서입니다."   ← the prod bad answer's source
        #   "이 문서는 아틀라스 B2B 전략에 대한 내용을 다루고 있습니다."
        #   "The document outlines the schedule for the week of May 25 …"
        # They are 0.3% of company claims (23/7535), and the source document itself stays in
        # the corpus + WikiSource.summary, so nothing is lost — only the fake claim/page.
        # The doc's real content, when it has any, already becomes claims on the normal path.

        source = WikiSource(
            slug=source_slug,
            title=title,
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=datetime.now(UTC),
            summary=summary,
            key_concepts=_coerce_text_list(data.get("key_concepts", [])),
            key_claims=[c.slug for c in claims],
            terminology=_coerce_text_list(data.get("terminology", [])),
            related_pages=page_slugs,
            open_questions=_coerce_text_list(data.get("open_questions", [])),
        )

        # Structured DB rows (source_db_name set) and dashboard logs generate
        # empty-column fill-in-the-blank open_questions that are not durable
        # knowledge. Suppress them at distill time so they never reach consolidate.
        if is_structured_row:
            source = source.model_copy(update={"open_questions": []})

        entities = _extract_entities(data.get("entities", []), markdown)

        span.add_meta(
            n_claims=len(claims),
            n_entities=len(entities),
            n_tautology_suppressed=n_tautology_suppressed,
            model_id=getattr(chat, "model_id", "unknown"),
            source_slug=source_slug,
        )
    return DistillResult(source=source, claims=claims, entities=entities)
