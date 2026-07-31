"""Wiki Q&A: ground the answer in self-authored wiki pages (docs/llm-wiki.md §8).

Replaces the old raw-chunk RAG (`rag.py`). `retrieve` finds top-k wiki chunks
(compiled page/claim bodies); the chat model answers ONLY from those excerpts.
The answer always carries its `WikiSourceRef` grounding (page slug + provenance)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from orthus.audit import audit
from orthus.db import session
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_WIKI_QA, get_chat_model_for
from orthus.schemas.canonical import ChatTurn, WikiAnswer, WikiSourceRef, derive_wiki_links
from orthus.settings import get_settings
from orthus.wiki.author import author_from_qa
from orthus.wiki.gap import detect_gap, maybe_missing_link, record_gap
from orthus.wiki.retrieve import (
    _expand_visible_provenance_slugs,  # shared provenance BFS (graph.py `_escape_like` 선례)
    retrieve,
)

logger = logging.getLogger(__name__)

# --- 넓은 질문 검색 1-hop 확장 (flag `ORTHUS_WIKI_RETRIEVE_EXPAND_ENABLED`, default OFF) ----
#
# "ai 툴 관련 요약해줘" 류 넓은 질문은 개별 구체 페이지(예: hugging-face, klingai)가 어휘
# 불일치로 1차 하이브리드 검색에 안 걸린다. 확장은 1차 hits 상위 seed 페이지에서
# (a) KG entity_neighbors(공유 엔티티로 엮인 다른 페이지 — KG off/미가용이면 조용히 skip,
#     router/graph.py fail-open 패턴) (b) provenance BFS로 **후보 페이지 집합만** 넓힌 뒤
# 2차 retrieve(page_slugs=확장셋)를 돌려 결정론 병합한다. 근거 생성은 계속 compiled wiki
# page 전용이므로 raw-chunk RAG가 아니고(절대 규칙), 추가 LLM 콜은 0이다.
_EXPAND_SEED_COUNT = 3  # 1차 hits 상위 몇 페이지를 확장 seed로 쓸지
_EXPAND_SLUG_CAP = 12  # 확장 slug 총 상한 (정렬 후 절단 — 결정론)
_EXPAND_SCORE_DISCOUNT = 0.85  # 확장 유래 hit 점수 할인 (1차 hit가 항상 우선)
_EXPAND_FINAL_K = 8  # 확장 활성 시 최종 hits 상한 (기존 k=5 → 8)


def _expand_broad_hits(
    user_id: UUID,
    question: str,
    hits: list[WikiSourceRef],
    *,
    k: int,
    scope: str,
    project: str | None,
    since: datetime | None,
    until: datetime | None,
    source: str | None,
) -> list[WikiSourceRef]:
    """1-hop 페이지 확장 + 결정론 병합. 실패는 호출자에서 fail-open (1차 hits 유지).

    1차 hits 순서/점수는 절대 바꾸지 않고, 확장 유래 hit만 할인 점수로 뒤에 붙인다 —
    flag ON이 기존 grounding을 잃게 만들 수 없다. 2차 retrieve는 기존 `page_slugs`
    주입 지점(K4b)을 그대로 써서 `_scope_filter`/provenance/blocked 가드가 전부 유지된다."""
    # 지연 import — 모듈 로드 시 KG 의존을 만들지 않고(flag OFF 기본 무동작),
    # personal-only 배포에서도 qa import가 KG 스택을 끌지 않는다.
    from orthus.kg.client import kg_available, kg_enabled
    from orthus.kg.gate import KgQueryStatus, run_kg_template

    seed_slugs: list[str] = []
    for h in hits[:_EXPAND_SEED_COUNT]:
        if h.page_slug not in seed_slugs:
            seed_slugs.append(h.page_slug)
    primary_slugs = {h.page_slug for h in hits}

    with audit("wiki.retrieve.expand") as span:
        candidates: set[str] = set()
        # (a) KG entity 다리 — company-only 데이터라 graph 분기와 같은 scope 가드.
        # KG off/미가용은 조용히 skip(구성/degradation이지 오류가 아님 — graph.py 패턴).
        kg_used = False
        if scope in ("company", "all") and kg_enabled() and kg_available():
            kg_used = True
            for slug in seed_slugs:
                result = run_kg_template(
                    user_id=user_id,
                    template_name="entity_neighbors",
                    params={"slug": slug},
                    correlation_id=span.correlation_id,
                )
                if result.status is not KgQueryStatus.OK:
                    continue  # per-seed fail-open (예: personal seed slug → slug_not_found)
                for n in result.nodes:
                    if n.label == "WikiPage" and n.materialized and n.slug:
                        candidates.add(n.slug)
        # (b) provenance BFS — retrieve의 기존 가시성 헬퍼 재사용 (scope 가드 내장).
        with session() as s:
            candidates |= _expand_visible_provenance_slugs(s, user_id, set(seed_slugs), scope=scope)
        candidates -= primary_slugs
        expansion = set(sorted(candidates)[:_EXPAND_SLUG_CAP])  # 정렬 후 절단 — 결정론 캡
        span.add_meta(
            n_seed=len(seed_slugs),
            kg_used=kg_used,
            n_candidates=len(candidates),
            n_expansion=len(expansion),
        )
        if not expansion:
            return hits

        # 2차 검색은 확장셋으로 제한 — 랭킹/근거는 기존 단일 compiled-wiki 경로 그대로.
        second = retrieve(
            user_id,
            question,
            k=_EXPAND_FINAL_K,
            scope=scope,
            project=project,
            since=since,
            until=until,
            source=source,
            page_slugs=expansion,
        )
        added: list[WikiSourceRef] = []
        seen = set(primary_slugs)
        for h in sorted(second, key=lambda r: (-round(r.score, 3), r.page_slug)):
            if h.page_slug in seen:
                continue  # 페이지 단위 dedupe (1차 hit 우선)
            seen.add(h.page_slug)
            added.append(h.model_copy(update={"score": round(h.score * _EXPAND_SCORE_DISCOUNT, 6)}))
        span.add_meta(n_added=len(added))
        if not added:
            return hits
        final_k = max(k, _EXPAND_FINAL_K)
        return (hits + added)[:final_k]


# The context block numbers its passages ("[1] (slug) excerpt"), and without explicit
# instruction the model imitates that numbering back at the user and — worse — answers
# with a catalogue of WHICH documents exist instead of WHAT they say (prod, 2026-07-16:
# "AI 영상관련툴에 대한 순위를 정리한 문서가 있으며 ... (문서 [1], [2], [3] 참조)" for
# "ai 툴 관련해서 내용 요약해줘"). Those two rules are therefore stated outright. The
# grounding contract ("ONLY from the passages") is unchanged — AGENTS.md §7.
_SYSTEM = (
    "You are a company wiki assistant. Answer the question using ONLY the provided "
    "context passages. If the context does not contain the answer, say so plainly. "
    "Cite nothing the context does not support. Answer in the question's language.\n"
    "Answer with the SUBSTANCE the passages contain — the actual facts, names, "
    "numbers, and details. Never answer by describing which documents exist or what "
    "a document is about ('X에 대한 문서가 있습니다', 'X에 대한 정보를 제공합니다'); "
    "the user wants the content itself, not a catalogue. When asked to summarize, "
    "summarize the passages' content, not their titles.\n"
    "Do NOT write passage numbers or citation markers in your answer ('[1]', "
    "'(문서 [2] 참조)'); the interface shows the sources separately.\n"
    "If the passages only describe documents without containing their substance, say "
    "that plainly instead of restating the descriptions.\n"
    "Any prior conversation is provided only to resolve references/pronouns (e.g. "
    "'아까 그거'); every fact must come from the numbered context passages, never "
    "from the prior conversation.\n"
    # cite-v2 (prompt-lab Track A, 2026-07-19): 위 마커 금지 한 줄만으로는 Solar가
    # 답변의 57%에서 인용 마커를 냈다. 말미 재강조 + 금지 예시 + 실체 보존 조항이
    # 위반율을 30%로 내리고(paired McNemar 44:6, p=3.2e-8) 판정 품질도 우세(35:23).
    # 근거: experiments/prompt-lab/analysis/qa-round-2.md
    "FINAL RULE — before you output, re-check: your answer must contain "
    "NO bracketed passage numbers and NO reference notes of any form. "
    "Forbidden: '[1]', '[2], [3]', '문서 [2] 참조', '(참고: [1] 발췌 기반)', "
    "'출처: [3]'. If a draft sentence needs a reference, rewrite it to name "
    "the thing itself (project, date, person) instead of the passage number. "
    "The interface already shows sources — any bracket citation in the "
    "answer text is a defect.\n"
    "Removing references must NEVER remove information: keep every fact, "
    "name, number, date, and status the passages support. Answer as fully "
    "as the passages allow — brevity is not a goal; completeness is."
)


# T2 learn contamination guard (deterministic, no LLM — design principle 1).
# Prod evidence (2026-07-16, company node): 179 learned qa-* pages included greetings
# ("안녕?"), and 23 wiki chunks carried a verbatim prior ANSWER with dead numbered
# citations ("... (문서 [1] 및 [2] 참조)"). Those claims are retrieved as grounding on
# the next similar question, so the same meta answer echoes back verbatim — a
# self-contamination loop. The guard below keeps such answers out of the wiki.
#
# The bracket rule requires a non-word char before "[" so that subscript-shaped
# technical prose is NOT mistaken for a citation. That is not hypothetical: the live
# company wiki has 35 legitimate chunks reading "parents[2] path-walking",
# "framings[1] is consistently B", "slugs[0]/slugs[1]" — real knowledge that must
# keep compounding. A citation always follows a space or "(" ("(문서 [1] 및 [2] 참조)").
_ANSWER_CITATION = re.compile(r"문서\s*\[|(?<![\w가-힣])\[\d+\]")
_MIN_LEARN_QUESTION_CHARS = 6


def _should_learn(question: str, answer: str, gap: object | None) -> bool:
    """Deterministic T2 learn gate: only substantive, well-grounded Q&A compounds.

    - `gap` present ⇒ the answer was flagged insufficient/weakly grounded; learning
      it would persist a known-bad answer as a claim.
    - Numbered-citation answers ("[1]", "(문서 [2] 참조)") echo the retrieval
      context's numbering; as a stored claim those references point at nothing.
      Subscript prose ("slugs[0]") is deliberately NOT a citation — see above.
    - Sub-6-char questions are greetings/fragments, not knowledge.
    """
    if gap is not None:
        return False
    if len(question.strip()) < _MIN_LEARN_QUESTION_CHARS:
        return False
    if _ANSWER_CITATION.search(answer):
        return False
    return True


def _notify_phase(on_phase: Callable[..., None] | None, stage: str, **extra: object) -> None:
    """Best-effort progress-phase notification (Tier i agent-work chat bubble).

    `on_phase` is the orchestrate live-stream publisher threaded down from the
    router (only the agent-work chat sets one; /ask search-only passes None). It
    already swallows its own errors, but this extra guard keeps ANY callback from
    ever breaking the answer path — progress display is feedback-only and must
    never affect correctness (same contract as orthus/agentwork/stream.py)."""
    if on_phase is None:
        return
    try:
        on_phase(stage, **extra)
    except Exception:  # noqa: BLE001 — 진행 표시 실패가 답변에 영향을 주면 안 된다
        pass


def _build_user_prompt(
    question: str,
    hits: list[WikiSourceRef],
    history: list[ChatTurn] | None = None,
) -> str:
    joined = "\n\n".join(f"[{i + 1}] ({h.page_slug}) {h.excerpt}" for i, h in enumerate(hits))
    body = f"Context passages:\n{joined}\n\nQuestion: {question}\n\nGrounded answer:"
    if history:
        # Non-authoritative reference block ABOVE the grounding context. History is
        # for resolving references only — never a fact source (AGENTS.md §7). When
        # history is empty/None the prompt stays byte-identical to the no-history form.
        preamble = "이전 대화 (참고용 — 사실의 출처가 아님):\n"
        preamble += "".join(f"{turn.role}: {turn.text}\n" for turn in history)
        preamble += "\n"
        return preamble + body
    return body


def ask(
    user_id: UUID,
    question: str,
    *,
    k: int = 5,
    scope: str = "all",
    project: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    source: str | None = None,
    chat_model: ChatModel | None = None,
    learn: bool = True,
    record_gaps: bool = True,
    context_wiki_slug: str | None = None,
    history: list[ChatTurn] | None = None,
    on_phase: Callable[..., None] | None = None,
) -> WikiAnswer:
    """Answer a question grounded in the wiki the user can see.

    `scope` ('all' | 'company' | 'personal') is forwarded to `retrieve`; the default
    'all' grounds on company + the user's own personal layer, never another user's
    personal content (P2.1). `project` (None = all projects) narrows grounding to a
    single company→project bucket (P2). T2 learning (below) is always PERSONAL.

    `record_gaps=False` detects but does NOT persist data-gap rows — used by the
    decompose fan-out so sub-question gaps don't create duplicates in the backlog.

    `on_phase` (Tier i): optional best-effort progress callback — `searching` before
    retrieval, `grounded` (with `evidence_count`) after, `answering` before the LLM
    call (in answer_from_hits). Never affects the answer."""
    _notify_phase(on_phase, "searching")
    hits = retrieve(
        user_id,
        question,
        k=k,
        scope=scope,
        project=project,
        since=since,
        until=until,
        source=source,
    )
    if hits and get_settings().wiki_retrieve_expand_enabled:
        # 넓은 질문 1-hop 확장 (flag OFF default). 어떤 예외든 1차 hits 그대로 반환 —
        # 확장은 후보 페이지 집합만 넓히는 best-effort이며 답변을 깨뜨릴 수 없다.
        try:
            hits = _expand_broad_hits(
                user_id,
                question,
                hits,
                k=k,
                scope=scope,
                project=project,
                since=since,
                until=until,
                source=source,
            )
        except Exception:  # noqa: BLE001 — fail-open: expansion must never break the answer
            logger.warning("wiki.retrieve.expand failed open; using primary hits", exc_info=True)
    # grounded notify는 확장 후 최종 hits 기준 (flag OFF면 1차 hits 그대로라 무해).
    _notify_phase(on_phase, "grounded", evidence_count=len(hits))
    return answer_from_hits(
        user_id,
        question,
        hits,
        chat_model=chat_model,
        learn=learn,
        record_gaps=record_gaps,
        context_wiki_slug=context_wiki_slug,
        history=history,
        on_phase=on_phase,
    )


def answer_from_hits(
    user_id: UUID,
    question: str,
    hits: list[WikiSourceRef],
    *,
    chat_model: ChatModel | None = None,
    learn: bool = True,
    warnings: list[str] | None = None,
    record_gaps: bool = True,
    context_wiki_slug: str | None = None,
    history: list[ChatTurn] | None = None,
    on_phase: Callable[..., None] | None = None,
) -> WikiAnswer:
    """Generate one grounded answer from pre-selected wiki hits.

    A deterministic gap check (no extra LLM call) flags poorly grounded answers and,
    when `record_gaps`, upserts them into the data-gap backlog as a pure side effect."""
    chat = chat_model or get_chat_model_for(TASK_WIKI_QA)
    no_hits_answer = "관련 위키 내용을 찾지 못했습니다. (wiki에 근거 없음)"
    with audit("wiki.answer") as span:
        if not hits:
            span.add_meta(n_sources=0)
            answer = no_hits_answer
        else:
            # Tier i: no `answering` phase on the empty-hits path — there is no LLM call.
            _notify_phase(on_phase, "answering")
            answer = chat.complete(_SYSTEM, _build_user_prompt(question, hits, history))
            span.add_meta(n_sources=len(hits), model_id=getattr(chat, "model_id", "unknown"))
    result = WikiAnswer(
        question=question,
        answer=answer,
        sources=hits,
        wiki_links=derive_wiki_links(hits),
        warnings=warnings or [],
    )
    # Deterministic insufficiency detection (design principle 1: code decides).
    result.gap = detect_gap(question, hits, answer)
    if result.gap is not None and record_gaps:
        # Side effect; a backlog write must never break the answer (e.g. dedup race).
        # K6 PR3: gate the deterministic KG missing_link upgrade (a graph query) on
        # record_gaps — the upgrade only matters when the gap is persisted, so callers
        # that opt out of the backlog pay no graph/PG cost. Sync-guard, company-scope,
        # fail-open: any failure leaves the original gap and never blocks the answer.
        try:
            # K7.4 — 세션 user_id를 thread(이 콜사이트에 scope 객체 없음; user_id는 상위 ask()/
            # /ask route 세션에서 옴). owner-scope ON이면 owner-inclusive missing_link이 caller
            # 본인 personal 노트도 anchor로 본다. caller는 hit에서 재유도하지 않는다(confused-deputy).
            result.gap = maybe_missing_link(result.gap, hits, user_id=user_id)
            record_gap(
                user_id,
                result.gap,
                source="auto",
                context_wiki_slug=context_wiki_slug,
                source_hits=hits,
            )
        except Exception:  # noqa: BLE001 — backlog is best-effort telemetry
            pass
    # T2 (docs/llm-wiki.md W4): persist the grounded answer back into the wiki as a
    # low-confidence claim so the wiki compounds from usage. Deterministic (no extra
    # LLM call) and a pure side-effect that never alters the returned answer.
    # P2.1: learning is PERSONAL (owner=user_id) — a user's Q&A must not silently
    # mutate the shared company wiki. Empty-grounding answers learn nothing.
    if learn and hits and _should_learn(question, answer, result.gap):
        # `history` is deliberately NOT passed: T2 learning must never absorb prior
        # conversation as evidence — it learns only the grounded question+answer.
        author_from_qa(user_id, question, answer, hits, chat_model=chat, root=None)
    return result
