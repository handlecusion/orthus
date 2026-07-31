"""K4b — `/ask` graph 분기 (docs/kg-model.md §4, docs/kg-implementation-spec.md §7).

라우터가 관계형 질문을 `graph`로 분류했을 때만 진입한다. 역할 분담은 다른 orthus
경로와 동일하게 보수적이다(원칙 1): LLM은 **의도 enum + 명사구 추출만** 하고,
템플릿 선택·subject→company page slug resolve·게이트 실행·grounding은 전부 결정론
코드다. LLM은 템플릿 이름도 Cypher도 만들지 않는다.

이 모듈의 어떤 실패도 예외를 던지지 않는다 — bind miss / kg 미가용 / 게이트 reject /
grounding 비어있음은 모두 `GraphOutcome(answer=None)`으로 수렴해 호출자
(`router.answer`)가 wiki 분기로 demote한다(fail-open, kg-impl §2.6/§7.2). 답변 본문은
graph가 고른 page들로 제한된 기존 wiki grounding(`answer_from_hits`)에서만 생성되므로
불변식 5(compiled wiki page 전용)가 구조적으로 보존된다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, or_, select

from orthus.audit import audit
from orthus.audit.redact import redact_pii
from orthus.db import session
from orthus.kg.client import kg_available, kg_enabled, kg_owner_scope_enabled
from orthus.kg.entities import normalize_name_norm
from orthus.kg.gate import (
    KgQueryStatus,
    _framing_from_result,
    run_kg_template,
    run_path_framings,
)
from orthus.kg.visibility import owner_inclusive_read_where
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_GRAPH_BIND, get_chat_model_for
from orthus.schemas.canonical import KgGraphAnswer, KgPathFramings, RoutedAnswer
from orthus.settings import get_settings
from orthus.tables import kg_entities, wiki_pages
from orthus.wiki.qa import answer_from_hits
from orthus.wiki.retrieve import _escape_like, retrieve  # _escape_like: shared LIKE escaper

logger = logging.getLogger(__name__)

# K8.3 (D6) — conflict gate-reject 경고 토큰. ask/page.tsx의 .includes() 검사와 짝을 이룬다.
# 한쪽이 바뀌면 배너가 조용히 사라지므로 단일 상수로 관리한다.
_CONFLICT_VIEW_UNAVAILABLE = "conflict_view_unavailable"

# v1 graph is company-scope-only (kg-model §4). personal-only scope is excluded; `all`
# is allowed but only ever reaches here on a company node (federation handles personal).
_GRAPH_SCOPES = {"company", "all"}

# Grounding breadth cap. A dense neighbors(depth=2) or long path_between can resolve many
# groundable pages; retrieve sizes its scans off k (vector=k*4, lexical=k*40), so an
# uncapped k would scan far more than the small top-k a grounded answer needs. The page
# set is already restricted by page_slugs, so a modest cap keeps the scan bounded.
_GROUNDING_K_CAP = 12

# The substring "graph-query intent" is a stable MockChat needle for tests — it appears
# only in this bind prompt, never in the classify _SYSTEM ("query router"). The LLM does
# extraction ONLY (원칙 1): intent enum + noun phrases, no template, no Cypher.
_BIND_SYSTEM = (
    "You extract graph-query intent and subjects for a company knowledge graph. "
    "Given a question about how things relate, return JSON only: "
    '{"intent": "relation|conflict|provenance|entity", "subjects": ["noun phrase", ...]}. '
    "Use intent=relation for 'how are A and B related/connected' (a relationship BETWEEN two "
    "things), intent=conflict for 'what conflicts with X', intent=provenance for 'where did X "
    "come from / what is the evidence for X', intent=entity for a question centered on ONE "
    "named thing (a person, project, product, team, or tool) asking which pages mention or "
    "share it — 'which pages mention X', 'where is X talked about', 'pages tied together by X'. "
    "Prefer entity over relation when there is a single named subject and the question is about "
    "the pages connected THROUGH that one thing (not a relationship between two distinct "
    "subjects). subjects are the concrete named things in the question (at most 3). "
    "Output only the JSON object with keys intent and subjects, nothing else."
)

_INTENTS = ("relation", "conflict", "provenance", "entity")


@dataclass
class GraphBinding:
    """Result of deterministic param binding — what template to run with what params.

    The resolved slugs live inside `params`; there is no separate slug list (it would be
    redundant state that could drift from `params`)."""

    template: str
    intent: str
    params: dict


@dataclass
class GraphOutcome:
    """Control-flow carrier between `try_graph_answer` and `router.answer` (never
    serialized). `answer is None` ⇒ demote to wiki; `fallback_warnings` are threaded
    into the demoted wiki answer's warnings as telemetry (kg-impl §7.2).

    `demote_reason` carries the PRECISE demote token (skipped|kg_disabled|bind_miss|
    no_groundable_pages|empty_grounding|gate_reject|kg_unavailable) — the same value set
    as `router.graph` span meta — so `router.answer` can record it without a span join."""

    answer: RoutedAnswer | None
    fallback_warnings: list[str] = field(default_factory=list)
    demote_reason: str | None = None


def try_graph_answer(
    user_id: UUID,
    question: str,
    *,
    scope: str = "all",
    project: str | None = None,
    chat_model: ChatModel | None = None,
    context_wiki_slug: str | None = None,
) -> GraphOutcome:
    """Attempt a graph-grounded answer; never raises. Returns a `mode="graph"`
    `RoutedAnswer` on success, else `GraphOutcome(answer=None)` to demote to wiki.

    The whole body — LLM bind, PG slug-resolve, gate, and restricted grounding — is
    inside one blanket guard so a Neo4j/PG/LLM failure anywhere fails open."""
    try:
        with audit("router.graph") as span:
            settings = get_settings()
            # SOLE structural guard. `router.answer` dispatches here on mode=='graph'
            # WITHOUT re-checking node_kind/scope, so this is the only thing enforcing the
            # company-scope-only / non-federation invariant (v1 KG has company rows only;
            # personal nodes have no Neo4j). Do not remove it expecting the caller to cover it.
            if settings.node_kind != "company" or scope not in _GRAPH_SCOPES:
                span.add_meta(graph="skipped")
                return GraphOutcome(answer=None, demote_reason="skipped")
            if not kg_enabled():
                # KG off is config, not degradation — demote silently, no warning.
                span.add_meta(graph="kg_disabled")
                return GraphOutcome(answer=None, demote_reason="kg_disabled")

            # K7.4 — owner-scope 유효 flag는 요청당 **1회만** 읽고(TOCTOU-safe) candidate
            # owner-inclusion과 two-framing dispatch가 같은 값을 쓴다(§2 step 0 / §5 (4c)).
            # gate의 per-template 재독은 게이트가 재검증하므로 독립적이고 허용된다.
            owner_scope = kg_owner_scope_enabled()

            # K8.3 (D6) — bind BEFORE the availability check so a degraded-KG demote knows the
            # intent. The pre-bind guard used to live above this, but it could only emit a
            # generic "kg_unavailable" (intent unknown), so a conflict question during a real
            # Neo4j outage — the dominant degradation — demoted to a plain-wiki answer that
            # reads as "no conflicts". bind is LLM+PG only (never touches Neo4j), so running it
            # first is safe; the only cost is one extra bind per graph question while KG is down
            # (rare, transient). Normal operation is unchanged (bind always ran post-availability).
            # K8.3 (D6) — `intent_out`로 LLM이 뽑은 intent를 따로 받아둔다. binding이 None
            # (intent는 conflict인데 subjects가 claim·page 어느 것으로도 resolve 안 됨)이어도
            # outage demote에서 honest 토큰을 붙일 수 있게 — bind 성공 여부와 intent를 분리한다.
            bound_intent: list[str] = []
            binding = bind_graph_params(
                question,
                caller_id=user_id,
                owner_scope=owner_scope,
                context_wiki_slug=context_wiki_slug,
                chat_model=chat_model,
                intent_out=bound_intent,
            )
            if not kg_available():
                # Flag on but Neo4j unreachable — degraded path, surface as telemetry. A
                # conflict intent additionally carries the honest signal (D6) so the demoted
                # plain-wiki answer isn't misread as "no conflicts here / all clear"; the
                # gate-reject arm below emits the same token post-bind. Availability outranks
                # bind_miss: during an outage the outage is the honest reason regardless of
                # whether the subjects resolved — so we check the detected intent (bound_intent)
                # even when binding is None (subject未해결 conflict 질문도 토큰을 받는다).
                fallback_warnings = ["kg_unavailable"]
                is_conflict_intent = (
                    binding.intent == "conflict"
                    if binding is not None
                    else "conflict" in bound_intent
                )
                if is_conflict_intent:
                    fallback_warnings.append(_CONFLICT_VIEW_UNAVAILABLE)
                span.add_meta(graph="kg_unavailable")
                return GraphOutcome(
                    answer=None,
                    fallback_warnings=fallback_warnings,
                    demote_reason="kg_unavailable",
                )
            if binding is None:
                span.add_meta(graph="bind_miss")
                return GraphOutcome(answer=None, demote_reason="bind_miss")

            # two-framing은 owner-scope ON + 2-slug path_between 바인딩에만. 그 외(flag-OFF,
            # 1-slug neighbors, claim intent)는 기존 단일 run_kg_template → .graph 경로
            # (path_framings=None). path_between owner-variant(framing B)가 그 쿼리와 동일하므로
            # 추가 호출 없이 교체한다 — framing B를 spine으로 grounding + .graph를 파생해 3x 왕복
            # (.graph 1 + A + B)을 피한다(§5 (4d)).
            path_framings: KgPathFramings | None = None
            two_framing = owner_scope and binding.template == "path_between"
            if two_framing:
                pf = run_path_framings(
                    user_id=user_id,
                    slug_a=binding.params["slug_a"],
                    slug_b=binding.params["slug_b"],
                    max_hops=binding.params["max_hops"],
                    correlation_id=span.correlation_id,
                )
                if pf is None:
                    # framing B(spine) fail/empty/error → demote(§2 step 5). 정밀 cause는
                    # run_path_framings 자신의 `router.graph.framings` span의 demote_cause로 기록된다
                    # (transient gate 오류 vs genuine empty 구분, §1) — top-span은 단일 사유로 둔다.
                    span.add_meta(graph="framings_demote")
                    return GraphOutcome(answer=None, demote_reason="framings_demote")
                spine = pf.spine
                path_framings = pf.framings
            else:
                result = run_kg_template(
                    user_id=user_id,
                    template_name=binding.template,
                    params=binding.params,
                    correlation_id=span.correlation_id,
                )
                if result.status is not KgQueryStatus.OK:
                    # Genuine gate failure after a real attempt (e.g. resolve→exec row race,
                    # Neo4j down, timeout). Demote + surface the reason as telemetry.
                    reason = result.reject_reason or "kg_unavailable"
                    span.add_meta(graph="gate_reject", reject_reason=reason)
                    # Keep the PRECISE reject token on demote_reason (e.g. "gate_reject:timeout",
                    # "gate_reject:driver_error:…") so router.answer's top-span fallback_reason
                    # preserves gate-failure granularity — a generic "gate_reject" would force
                    # the exact span join this thread-up was meant to avoid.
                    # K8.3 (D6) — a conflict query that BOUND but failed at the gate must surface
                    # an honest signal so the demoted plain-wiki answer isn't misread as "no
                    # conflicts here / all clear". Intent is known post-bind (unlike the pre-bind
                    # kg_unavailable guard, which already warns generically for every graph intent).
                    warnings = [reason]
                    if binding.intent == "conflict":
                        warnings.append(_CONFLICT_VIEW_UNAVAILABLE)
                    return GraphOutcome(
                        answer=None,
                        fallback_warnings=warnings,
                        demote_reason=f"gate_reject:{reason}",
                    )
                # 단일 run_kg_template 결과를 spine shape로 매핑 — _framing_from_result를 재사용해
                # KgTemplateResult→nodes/edges/path_slugs/truncated 투영을 한 곳에 모은다(이전엔
                # framing 빌드·spine 추출·KgGraphAnswer 3곳에 복붙). label/personal_dependent는
                # 이 비-framing 경로에선 미사용.
                spine = _framing_from_result(binding.template, result)

            page_slugs = _grounding_slugs(spine.nodes)
            if not page_slugs:
                # Path found but no materialized WikiPage/WikiClaim to ground on — demote
                # to plain wiki (never emit mode="graph" with zero compiled-page sources).
                span.add_meta(graph="no_groundable_pages")
                return GraphOutcome(answer=None, demote_reason="no_groundable_pages")

            # `page_slugs` restricts retrieve's candidate set to the path's pages; the
            # distance-ordered vector scan then returns those pages' chunks (non-empty
            # whenever the pages carry indexed chunks). Empty only for chunk-less pages —
            # in which case there is genuinely nothing to ground on and demote is correct
            # (불변식 5: no mode="graph" with zero sources).
            hits = retrieve(
                user_id,
                question,
                k=min(max(5, len(page_slugs)), _GROUNDING_K_CAP),
                scope="company",
                project=project,
                page_slugs=set(page_slugs),
            )
            if not hits:
                span.add_meta(graph="empty_grounding")
                return GraphOutcome(answer=None, demote_reason="empty_grounding")

            # Body grounds on the graph-selected pages through the EXISTING wiki path.
            # learn/record_gaps off: the graph success path found a connection, so it must
            # not compound a T2 claim or record a (missing_link) gap — those belong to the
            # wiki demote path (normal ask()) where the relation was NOT found (kg-impl §7.3).
            wiki_answer = answer_from_hits(
                user_id,
                question,
                hits,
                chat_model=chat_model,
                learn=False,
                record_gaps=False,
                context_wiki_slug=context_wiki_slug,
            )
            graph = KgGraphAnswer(
                template=binding.template,
                intent=binding.intent,
                # spine_* — two-framing이면 framing B(owner-visible path)에서, 아니면 단일
                # run_kg_template 결과에서. .graph는 K4/K5와 byte-shared shape이므로 owner-scope
                # 개념(framing/personal_dependent)을 싣지 않는다 — 그건 RoutedAnswer.path_framings
                # 전용이다(L2).
                nodes=spine.nodes,
                edges=spine.edges,
                # RAW path/neighbor node ordering — includes non-groundable nodes
                # (provenance_chain Document/WikiSource slugs, placeholder
                # materialized=False slugs). Intentionally distinct from the grounded
                # source set (materialized WikiPage/WikiClaim only, via _grounding_slugs):
                # a K5 consumer must NOT treat these as clickable compiled-page targets.
                path_slugs=spine.path_slugs,
                params_redacted=redact_pii(dict(binding.params)),
                truncated=spine.truncated,
            )
            span.add_meta(graph="ok", template=binding.template, n_sources=len(hits))
            return GraphOutcome(
                answer=RoutedAnswer(
                    question=question,
                    mode="graph",
                    wiki=wiki_answer,
                    graph=graph,
                    # K7.4 — owner-scope two-framing은 graph-success 반환에만(demote는 wiki
                    # 반환이므로 None). flag-OFF/neighbors/claim은 None(위 path_framings 초기값).
                    path_framings=path_framings,
                    warnings=wiki_answer.warnings,
                )
            )
    except Exception:  # noqa: BLE001 — fail-open: any failure demotes to wiki
        # Record the failure so a deterministic always-fails bug (schema drift, a renamed
        # field, etc.) is detectable rather than silently masquerading as "no relation
        # found". Without this the whole branch could be 100% dead with no signal.
        logger.warning("router.graph failed open; demoting to wiki", exc_info=True)
        return GraphOutcome(answer=None, demote_reason="internal_error")


def bind_graph_params(
    question: str,
    *,
    caller_id: UUID | None = None,
    owner_scope: bool = False,
    context_wiki_slug: str | None = None,
    chat_model: ChatModel | None = None,
    intent_out: list[str] | None = None,
) -> GraphBinding | None:
    """LLM extract (intent + ≤3 subjects) → deterministic resolve + template select.

    Returns None (→ wiki fallback) on any unparseable/over-length/ambiguous result. The
    LLM never names a template or a slug; code maps intent→template and resolves each
    subject to exactly one page slug.

    K7.4: `owner_scope`(호출자가 1회 읽어 내려줌, TOCTOU-safe) + `caller_id`로 relation page
    candidate를 owner-inclusive로 resolve한다(claim/context는 company-only 유지). candidate는
    advisory — 경계는 gate resolve_slug다.

    K8.3 (D6): `intent_out`가 주어지면 LLM이 추출한 유효 intent를 그 리스트에 append한다 —
    subjects가 resolve 안 돼 None을 반환하더라도 호출자가 intent(예: conflict)를 알 수 있게.
    outage demote에서 honest 토큰을 붙이는 데 쓴다(반환 contract는 불변)."""
    chat = chat_model or get_chat_model_for(TASK_GRAPH_BIND)
    with audit("router.graph.bind") as span:
        raw = chat.complete(_BIND_SYSTEM, f"Question: {question}", json_only=True)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        intent = data.get("intent")
        if intent not in _INTENTS:
            return None
        # D6 — 유효 intent를 호출자에 흘려보낸다(binding이 None으로 끝나도). outage demote의
        # honest 토큰 판정용. JSON parse 실패/invalid intent면 기록 안 함(intent 불명).
        if intent_out is not None:
            intent_out.append(intent)
        subjects = data.get("subjects")
        if not isinstance(subjects, list):
            return None
        subjects = [s for s in subjects if isinstance(s, str) and s.strip()][:3]

        # K9.3a — entity intent는 slug-centric resolver와 별개 경로다(B3 — entity-rooted). 명사구를
        # name_norm으로 정규화(persist와 동일한 normalize_name_norm: redact+NFKC+공백collapse+casefold,
        # L5 드리프트 방지)해 company kg_entities에 존재하면 entity_mentions(name_norm)로 바인딩한다.
        # page slug resolve를 타지 않고, name_norm은 Direct PII라 span meta에 절대 싣지 않는다(U6 —
        # 게이트 로그도 log_drop_params로 drop). 미존재면 None → wiki demote(3중 가드 동일).
        if intent == "entity":
            name_norm = _resolve_entity_name(subjects)
            span.add_meta(
                intent="entity",
                n_subjects=len(subjects),
                resolved=name_norm is not None,
                template="entity_mentions" if name_norm is not None else None,
            )
            if name_norm is None:
                return None
            return GraphBinding("entity_mentions", "entity", {"name_norm": name_norm})

        # The target start-node kind is fixed by the intent's template: relation runs over
        # :WikiPage (path_between/neighbors), but conflict/provenance run over :WikiClaim
        # (conflicts_of/provenance_chain MATCH (c:WikiClaim {slug})). Resolving every intent
        # to kind='page' silently mis-binds those two — a page slug can never match a claim
        # start node, so the gate returns empty and the branch always demotes. Resolve to
        # the kind the chosen template actually starts from.
        target_kind = "claim" if intent in ("conflict", "provenance") else "page"

        # One session resolves every subject (and the context slug) — no per-subject
        # session churn on the /ask hot path. `n_unresolved` counts NAMED subjects that
        # failed to resolve (≠ dedup): it lets _bind tell "two phrasings of the same page"
        # (benign → neighbors) apart from "two distinct subjects, one not found" (→ demote).
        resolved: list[str] = []
        n_unresolved = 0
        with session() as s:
            for subj in subjects:
                slug = _resolve_subject(
                    s, subj, kind=target_kind, caller_id=caller_id, owner_scope=owner_scope
                )
                if slug is None:
                    n_unresolved += 1
                elif slug not in resolved:
                    resolved.append(slug)
            # context_wiki_slug (P4.2 prefill) is APPENDED, never prepended: explicit
            # subjects take the path endpoints, and the context only fills a missing
            # endpoint for subject-omitted questions ("이 페이지와 B는 무슨 관계?"). It is a
            # wiki PAGE slug, so it only ever completes a relation (page) binding — for
            # claim-rooted intents _slug_exists(kind='claim') rejects it, as intended.
            if (
                context_wiki_slug
                and context_wiki_slug not in resolved
                and _slug_exists(s, context_wiki_slug, kind=target_kind)
            ):
                resolved.append(context_wiki_slug)

        binding = _bind(intent, resolved, n_unresolved=n_unresolved)
        # K8.3 (B3) — conflict intent page fallback. 토픽/페이지 표현("nova 회고에서 모순")은
        # claim slug로 resolve되지 않아 위 binding이 None(bind_miss)이 된다. claim이 하나도 안
        # 잡혔을 때만(claim 우선 — 기존 conflicts_of 경로 무회귀) 같은 주어를 PAGE로 re-resolve해
        # 그 페이지 claim들의 모순(page_conflicts)을 노출한다. 같은 주어가 claim·page 둘 다로
        # resolve돼도 claim이 먼저 잡혔으면 여기 오지 않으므로 claim 우선이 보장된다. page 후보는
        # 1차 claim/page resolve와 **대칭**으로 owner-inclusive다(코드리뷰 #3 — `caller_id`/
        # `owner_scope` 전달). owner-scope ON이면 호출자 본인 personal 페이지의 모순도 토픽
        # 표현으로 닿는다(K8.6이 personal conflict status를 권위화한 것과 정합). 경계는 게이트
        # `page_conflicts` owner-variant(visibility_predicate) + `_map_records` tripwire가 강제한다
        # — 비-owner 세션엔 personal 후보가 resolve돼도 게이트가 노드/엣지를 drop한다.
        if binding is None and intent == "conflict":
            page_resolved: list[str] = []
            with session() as s:
                for subj in subjects:
                    slug = _resolve_subject(
                        s, subj, kind="page", caller_id=caller_id, owner_scope=owner_scope
                    )
                    if slug and slug not in page_resolved:
                        page_resolved.append(slug)
                if (
                    context_wiki_slug
                    and context_wiki_slug not in page_resolved
                    and _slug_exists(s, context_wiki_slug, kind="page")
                ):
                    page_resolved.append(context_wiki_slug)
            if page_resolved:
                if len(page_resolved) > 1:
                    span.add_meta(
                        conflict_page_candidates=len(page_resolved),
                        conflict_page_used="first",
                    )
                binding = GraphBinding("page_conflicts", intent, {"slug": page_resolved[0]})
        span.add_meta(
            intent=intent,
            n_subjects=len(subjects),
            n_resolved=len(resolved),
            n_unresolved=n_unresolved,
            template=binding.template if binding else None,
        )
        return binding


def _bind(intent: str, slugs: list[str], *, n_unresolved: int) -> GraphBinding | None:
    """intent + resolved slugs → template + params (code-only, no LLM).

    `n_unresolved` is the count of NAMED subjects that failed to resolve — it gates the
    neighbors branch so we never silently answer neighbors-of-the-survivor for a relation
    question where a named subject was not found."""
    if intent == "relation":
        if len(slugs) >= 2:
            return GraphBinding(
                "path_between",
                intent,
                {"slug_a": slugs[0], "slug_b": slugs[1], "max_hops": 4},
            )
        # Neighbors fires for a single distinct page ONLY when every named subject
        # resolved (n_unresolved == 0): that covers a genuine one-subject question AND
        # two phrasings that deduped to the SAME page (both benign — there is no A↔B pair
        # to bridge, so neighbors-of-that-page is the right answer). If a named subject
        # was NOT found (n_unresolved > 0 — even when a context slug filled the slot), the
        # asked relation can't be formed, so demote to wiki rather than answer a different
        # question with no signal.
        if len(slugs) == 1 and n_unresolved == 0:
            return GraphBinding("neighbors", intent, {"slug": slugs[0], "depth": 2})
        return None
    if intent == "conflict" and slugs:
        return GraphBinding("conflicts_of", intent, {"slug": slugs[0]})
    if intent == "provenance" and slugs:
        return GraphBinding("provenance_chain", intent, {"slug": slugs[0]})
    return None


def _resolve_entity_name(subjects: list[str]) -> str | None:
    """K9.3a — 명사구 목록 → 첫 번째로 company `kg_entities`에 실재하는 `name_norm`.

    정규화는 persist 경로(`entities.persist_entities`)와 **같은** `normalize_name_norm`을 써
    드리프트가 없다(L5). person 포함 모든 kind를 허용한다 — 직접 질의한 anchor가 인물이어도
    제외하면 빈 답이 되기 때문이다(§0 자동-티저 person 억제는 page 패널 한정, /ask 직접 질의는
    별개). 존재 확인만 하고 슬러그 resolve를 타지 않으므로 owner-scope/page_id 경로와 무관하다
    (entity는 company-only projection — gate가 company-fork 강제)."""
    with session() as s:
        for subj in subjects:
            norm = normalize_name_norm(subj)
            if not norm:
                continue
            exists = s.execute(
                select(kg_entities.c.entity_id)
                .where(kg_entities.c.name_norm == norm, kg_entities.c.scope == "company")
                .limit(1)
            ).scalar_one_or_none()
            if exists is not None:
                return norm
    return None


def _grounding_slugs(nodes) -> list[str]:
    """Keep only materialized WikiPage/WikiClaim slugs from the path — the groundable
    compiled pages. Document/StructuredFact/placeholder nodes are path-explanation only
    (kg-impl §7.3). Claims are wiki_pages rows with their own chunks, so they ground
    directly (kind='claim' is a valid grounding kind)."""
    out: list[str] = []
    for n in nodes:
        if n.label in ("WikiPage", "WikiClaim") and n.materialized and n.slug:
            if n.slug not in out:
                out.append(n.slug)
    return out


# --- company slug resolution (single shared session — caller owns the session) --------
#
# These are deliberately kind-AWARE (the caller passes the kind the chosen graph template
# starts from: 'page' for relation, 'claim' for conflict/provenance). That is why they are
# NOT the gate's kind-agnostic `_first_missing_company_slug` — the routing layer must pick
# the right start-node namespace, while the gate only checks company membership. Scope is
# NOT uniformly company: `_slug_exists` (context_wiki_slug) is company-only, but
# `_resolve_subject` is owner-INCLUSIVE for page candidates when owner-scope is on (K7.4,
# via `_relation_candidate_filter`). These candidates are ADVISORY — the authoritative
# owner/company boundary is the gate's `resolve_slug` (re-resolves under the owner predicate),
# so a buggy candidate can never return another owner's row (see the per-tier note below).
#
# Resolution is one round trip per subject: fetch all rows matching any tier (exact slug →
# exact title → title prefix), ordered so exact matches survive the cap, then apply tier
# priority in Python. More than one match in the winning tier is ambiguous and yields None.
#
# _RESOLVE_FETCH_CAP bounds the scan. Its interaction with each tier:
#   - exact-slug / exact-title: the ORDER BY floats these matches to the front, so they are
#     never dropped by the LIMIT — those tiers see every match and the ambiguity guard holds.
#   - title-prefix: NOT pulled forward by the ORDER BY, so the cap CAN truncate prefix
#     matches. This still cannot mis-resolve: slug is unique within (slug, scope, owner_id)
#     via uq_wiki_pages_slug_scope_owner (NULLS NOT DISTINCT). K7.4: relation page candidates
#     are owner-INCLUSIVE (company + caller's own personal), so one slug can appear twice
#     (≤1 company + ≤1 caller-personal), but dict.fromkeys dedups by slug → uniq still counts
#     DISTINCT slugs. >_RESOLVE_FETCH_CAP prefix matches therefore still yield >=2 distinct
#     slugs → len(uniq)!=1 → None (never the wrong single page). A company page and the
#     caller's OWN personal note with DIFFERENT slugs in the winning tier → len(uniq)!=1 →
#     None demote (L5 — no 2nd precedence in the fuzzy layer; precedence lives only in the
#     gate's resolve_slug CASE). Candidates are ADVISORY — the gate's resolve_slug re-resolves
#     every slug under the owner predicate, so a buggy candidate can never return another
#     owner's note.
_RESOLVE_FETCH_CAP = 50


def _company_filter(kind: str):
    """company-only candidate filter (kind-aware). Used by _slug_exists + claim resolution.
    K7.4 keeps these company-only (decision a) — only relation PAGE candidate gen is
    owner-inclusive, via _relation_candidate_filter."""
    return (wiki_pages.c.scope == "company", wiki_pages.c.kind == kind)


def _relation_candidate_filter(kind: str, caller_id: UUID | None, owner_scope: bool):
    """K7.4 — candidate filter for `_resolve_subject`. Owner-inclusive ONLY for relation
    page candidates (kind='page' + owner-scope ON): `company OR (personal AND owner==$caller)`
    from the single shared visibility helper (NOT a hand-rolled OR; NOT the write predicate).
    claim candidates (conflict/provenance) and flag-OFF stay company-only — this slice does
    not opt those surfaces into owner-inclusion (§5 (4a))."""
    if kind == "page" and owner_scope and caller_id is not None:
        return (
            owner_inclusive_read_where(wiki_pages.c.scope, wiki_pages.c.owner_id, caller_id),
            wiki_pages.c.kind == kind,
        )
    return _company_filter(kind)


def _resolve_subject(
    s, subject: str, *, kind: str, caller_id: UUID | None = None, owner_scope: bool = False
) -> str | None:
    """Resolve a noun phrase to exactly one slug of `kind` (page|claim), trying tiers in
    order: exact slug → exact title (case-insensitive) → title prefix. Returns the first
    tier with a unique match; zero/ambiguous at every tier → None.

    K7.4: relation page candidates are owner-inclusive (caller's own personal rows join the
    candidate set via _relation_candidate_filter); claim candidates stay company-only. The
    fuzzy layer keeps `len(uniq)!=1 → None` (L5) — it does NOT encode a personal-first
    precedence; the authoritative boundary is the gate's resolve_slug (advisory candidates).

    Cap interaction (see _RESOLVE_FETCH_CAP comment): the ORDER BY protects exact-slug and
    exact-title from the LIMIT; the title-prefix tier cannot mis-resolve because slug is
    unique within (slug, scope, owner_id) so any over-cap prefix set carries >=2 distinct
    slugs → None."""
    subj = subject.strip()
    if not subj:
        return None
    lower = subj.lower()
    rows = s.execute(
        select(wiki_pages.c.slug, wiki_pages.c.title)
        .where(
            *_relation_candidate_filter(kind, caller_id, owner_scope),
            or_(
                wiki_pages.c.slug == subj,
                func.lower(wiki_pages.c.title) == lower,
                wiki_pages.c.title.ilike(f"{_escape_like(subj)}%", escape="\\"),
            ),
        )
        # Exact-slug then exact-title first, so they are never dropped by the LIMIT cap.
        .order_by(
            (wiki_pages.c.slug == subj).desc(),
            (func.lower(wiki_pages.c.title) == lower).desc(),
        )
        .limit(_RESOLVE_FETCH_CAP)
    ).all()
    exact_slug = [r.slug for r in rows if r.slug == subj]
    exact_title = [r.slug for r in rows if r.title is not None and r.title.lower() == lower]
    prefix = [r.slug for r in rows if r.title is not None and r.title.lower().startswith(lower)]
    for tier in (exact_slug, exact_title, prefix):
        uniq = list(dict.fromkeys(tier))
        if len(uniq) == 1:
            return uniq[0]
    return None


def _slug_exists(s, slug: str, *, kind: str) -> bool:
    """True if `slug` is a company wiki row of `kind` (context_wiki_slug validation)."""
    return (
        s.execute(
            select(wiki_pages.c.slug)
            .where(*_company_filter(kind), wiki_pages.c.slug == slug)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )
