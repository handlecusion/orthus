"""Answer-gap detection + data-gap backlog (docs/architecture-v2.md §7).

When the wiki path returns a poorly grounded answer, `detect_gap` flags it with a
deterministic reason — NO extra LLM call, so /ask latency is unchanged (design
principle 1: deterministic code decides, LLM only compiles). The flag is attached
to the answer and the gap is upserted into the `data_gaps` backlog so a data owner
can see what knowledge is missing.

The richer 'add these fields here' suggestion is LLM-generated, but only on demand
(`generate_suggestion`, triggered by the /gaps/feedback button), never inline on the
hot answer path.

PII: the question is redacted before it is persisted (hard rule — same as query_runs)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import insert, or_, select, update

from orthus.audit import audit
from orthus.audit.redact import redact_pii_text
from orthus.db import session
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_GAP_SUGGEST, get_chat_model_for
from orthus.schemas.canonical import (
    DataGap,
    GapReason,
    GapSuggestionSection,
    WikiGap,
    WikiSourceRef,
)
from orthus.settings import get_settings
from orthus.tables import data_gaps
from orthus.wiki.slug import clean_wiki_slug

# A top hit below this cosine score means retrieval found nothing closely related.
WEAK_SCORE_THRESHOLD = 0.55

# Grounding-absence markers. The wiki QA system prompt instructs the model to say
# "the context does not contain the answer" plainly; these substrings catch that,
# distinguishing a real "we have no data" answer from a substantive one. Each marker
# already implies absence (없/못/cannot), so false positives on good answers are rare.
_INSUFFICIENT_MARKERS = (
    "정보 없",
    "정보가 없",
    "정보는 없",
    "내용 없",
    "내용이 없",
    "찾지 못",
    "찾을 수 없",
    "확인되지 않",
    "확인할 수 없",
    "알 수 없",
    "나와 있지 않",
    "언급이 없",
    "언급되어 있지 않",
    "포함되어 있지 않",
    "정의가 없",
    "컨텍스트에 없",
    "컨텍스트에는 없",
    "근거 없",
    "근거가 없",
    "does not contain",
    "doesn't contain",
    "no information",
    "cannot find",
    "can't find",
    "not provided in",
    "not mentioned",
    "no relevant",
    "unable to find",
)

# Absence phrasings the flat table above misses. Measured leak (fugu-ko E2 anchor probe,
# 62 observed refusals): the table catches "포함되어 있지 않" but not "…정보는 제공되지
# 않았습니다" / "…정보가 명시되어 있지 않습니다" — the English "not provided in" is registered
# while its Korean mirrors were never added. Refusal phrasing differs BY MODEL, so a table
# that only knows one dialect silently penalizes whichever model speaks another.
#
# These cannot be added as plain substrings. A bare "제공되지 않" / "명시되지 않" also fires on
# a substantive answer's caveat tail ("추가 세부 사항은 제공되지 않음", "구체적인 툴 이름은
# 명시되지 않았습니다") and even on a plain product FACT ("별도 자막 관리자 창 기능은 제공하지
# 않습니다") — and a false gap is expensive: the answer is dropped from the answer cache AND
# from decompose synthesis. So the absence verb is bound to an information noun, which is
# exactly the "the model declared the CONTEXT lacks the answer" shape the table already
# encodes for 포함되어 있지 않 / 나와 있지 않. Answers whose absence clause is about a
# domain noun (액션/이름/유형…) stay grounded.
_INFO_NOUN = r"(?:정보|내용|자료|근거|설명|언급|기록|데이터)"
_ABSENCE_VERB = (
    r"(?:제공되지|제공하지|명시되어\s*있지|명시되지|기재되어\s*있지|"
    r"드러나지|나타나\s*있지|서술되어\s*있지)"
)
_INSUFFICIENT_PATTERNS = (
    # "…정보는 (현재) 제공되지 않았습니다" · "…내용이 명시되어 있지 않습니다"
    # (short intra-sentence gap so an adverb/qualifier between noun and verb still matches).
    re.compile(rf"{_INFO_NOUN}[은는이가을를도]?\s*(?:[^.。\n]{{0,12}}\s*)?{_ABSENCE_VERB}\s*않"),
    # Explicit refusal: "따라서 해당 질문에 대한 답변을 제공할 수 없습니다" · "답변할 수 없습니다".
    re.compile(r"답변(?:을|를)?\s*(?:제공)?할\s*수\s*없"),
)

_WS = re.compile(r"\s+")


def normalize_question(question: str) -> str:
    """Dedup key: lowercase, collapse whitespace, strip trailing punctuation."""
    return _WS.sub(" ", question.strip().lower()).strip(" ?？.!~")


def detect_gap(question: str, hits: list[WikiSourceRef], answer_text: str) -> WikiGap | None:
    """Deterministic insufficiency check over (hits, answer). None == answer is fine.

    Priority: no data > the model declared the context lacks the answer > retrieval
    was weak. Returns the first that applies."""
    top = max((h.score for h in hits), default=None)
    if not hits:
        reason: GapReason = "no_data"
    elif _looks_insufficient(answer_text):
        reason = "insufficient_grounding"
    elif top is not None and top < WEAK_SCORE_THRESHOLD:
        reason = "weak_retrieval"
    else:
        return None
    return WikiGap(
        detected=True,
        reason=reason,
        missing_topic=question,
        top_score=top,
        message=_build_message(question, reason),
    )


def _looks_insufficient(answer_text: str) -> bool:
    lowered = (answer_text or "").lower()
    if any(marker in lowered for marker in _INSUFFICIENT_MARKERS):
        return True
    return any(pattern.search(lowered) for pattern in _INSUFFICIENT_PATTERNS)


def _build_message(question: str, reason: GapReason) -> str:
    """Deterministic 'add data here' guidance (no LLM). Scope-aware target."""
    where = "개인 Notion 또는 /editor 문서" if _is_personal() else "회사 Notion 또는 /editor 문서"
    head = {
        "no_data": f'"{question}"에 대한 자료가 아직 없습니다.',
        "weak_retrieval": f'"{question}"에 관련된 정리된 자료가 부족합니다.',
        "insufficient_grounding": f'"{question}"의 단편 정보만 있고 정리된 설명이 없습니다.',
        "missing_link": f'"{question}"에 관련된 자료는 있으나 서로 연결되어 있지 않습니다.',
    }[reason]
    return (
        f"{head} {where}에 핵심 정보를 정리해 추가하고 sync(또는 /editor 저장)하면 "
        "다음 질문부터 답에 반영됩니다."
    )


# K7.4 — cross-scope(내 personal 1 + company 1) missing_link 라이브 카피. L7로 **중립/slug-free**
# (회사 페이지 제목 미포함 → slug→title lookup 없음 → 비-redacted title이 메시지에 애초에 안 들어가
# title-leak 표면 0). 1인칭 "내 메모"로 소유를 분명히 한다(raw 영어 'personal' 금지). **이 슬라이스는
# 연결 액션을 ship하지 않으므로 라이브 카피는 서술형만 쓴다** — 예전 "연결해 둘까요?" 같은 약속형/질문형
# CTA는 실제 연결 버튼이 없어 dead-end가 되므로 금지한다. CTA 동사는 실제 연결/promote 버튼이 나가는
# K7.4c FE와 **함께** 도입한다(§4 non-authoritative 워딩 규약). owner에게 "어느 회사 페이지인지" named
# legibility도 owner-only K7.4c FE가 제공한다(백엔드 메시지엔 제목 안 실음). 이 문자열은 라이브
# RoutedAnswer 본문에만 싣고 어떤 audit meta에도 verbatim 기록하지 않는다(data_gaps에 message 컬럼
# 없음 — durable 표면 0).
_CROSS_SCOPE_MISSING_LINK_MESSAGE = "내 메모와 회사 자료가 아직 서로 연결되어 있지 않습니다."


# --- missing_link upgrade (K6 PR3 — deterministic KG signal, 0 LLM calls) -------
#
# §9.3 동기-가드: insufficient_grounding 답변 직후, retrieval이 ≥2개의 materialized
# company wiki page로 resolve되고 KG가 가용하면, 상위 2개 page 사이에 path_between
# (max_hops=4)을 K4 게이트로 질의한다 — 경로가 없으면 "자료는 있는데 서로 연결돼
# 있지 않다"는 신호이므로 gap reason을 missing_link로 승격한다. company scope 한정.
# path_between은 지식 rel(SUPPORTS/CONFLICTS_WITH/BACKLINK/DERIVED_FROM/EXTRACTED_FROM)
# 만 타고 IN_PROJECT 허브는 제외하므로(templates.py), 같은 프로젝트라는 이유만으로는
# "연결"로 보지 않는다 — 같은 프로젝트의 비연결 page 쌍도 정상 감지된다.
#
# 가용성 판정은 게이트(run_kg_template)가 단일 지점에서 한다 — 여기서 따로 핑하지
# 않는다(이중 verify_connectivity 방지). KG-down 시 connect-timeout 반복은
# client.kg_available의 음성 캐시(§2.6)가 흡수한다. flag off는 게이트가 즉시
# kg_disabled로 reject하지만 그 경우에도 kg_query_runs row를 쓰므로, off 기본 상태의
# 핫패스 PG write를 피하려고 kg_enabled() 단축을 먼저 둔다.
# fail-open: KG off/미가용/reject는 전부 원래 gap 그대로 반환(답변 차단 없음). 게이트는
# 예외를 던지지 않으므로(gate.py 계약) 여기서 broad except로 가리지 않는다 —
# 로컬 로직 버그는 테스트에서 드러나야 한다(호출자 qa.py가 best-effort로 감싼다).


def _top_company_page_slugs(
    hits: list[WikiSourceRef], *, n: int = 2, owner_scope: bool = False
) -> list[tuple[str, str]]:
    """First `n` distinct hit page slugs with scope tag (hits arrive score-ordered).

    Returns `[(slug, 'company'|'personal'), ...]` — company/personal 분류를 한 곳에 집중해
    상위 호출자(maybe_missing_link/record_gap)가 cross-scope 여부와 영속 scope를 이 반환에서
    파생한다(별도 ad-hoc scope-tag 중복 방지).

    K7.4 owner-inclusive: `owner_scope` ON이면 caller 본인 personal hit도 candidate로 받는다
    (company-only `source_scope=='company'` 필터 완화). **안전 단일 전제:** `hits`는 상위
    retrieve가 central owner-scope로 만든 집합이라 회사 hit + caller 본인 personal hit만 들어
    있고 foreign-personal은 애초에 없다(P8.1) → 남는 personal hit의 scope-tag는 retrieve 경계가
    "caller 본인 소유"를 보장한다(여기서 owner_id를 따로 재검증하지 않는다). 이 전제가 깨지면
    (미래 retrieve가 foreign-personal을 섞으면) leak이 되므로 §6 "타 owner hit이 top-2에 안 들어옴"
    회귀가 retrieve 경계 변화를 잡는다.

    **byte-identity(필수):** `owner_scope` OFF이거나 caller가 personal 노트를 0개 가져 retrieve가
    personal hit을 안 돌려주면, 결과는 today company-only 경로와 동일 candidate set이다(같은 slug,
    같은 순서) — 단지 반환 타입이 `(slug, 'company')` tuple일 뿐. 노트 보유 여부가 candidate
    shape/비용을 바꾸지 않는다(fingerprint 방지 + ops 고볼륨 경로 무변동)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for h in hits:
        if h.source_scope == "company":
            tag = "company"
        elif h.source_scope == "personal" and owner_scope:
            tag = "personal"  # retrieve 경계가 caller-own 보장(foreign-personal은 hits에 없음)
        else:
            continue  # flag-off personal / 기타 → 제외(flag-off는 today와 byte-identical)
        slug = clean_wiki_slug(h.page_slug)
        if slug is None or slug in seen:
            continue
        seen.add(slug)
        out.append((slug, tag))
        if len(out) >= n:
            break
    return out


def maybe_missing_link(
    gap: WikiGap, hits: list[WikiSourceRef], *, user_id: UUID | None = None
) -> WikiGap:
    """Upgrade an insufficient_grounding gap to missing_link when the top-2 anchor pages
    are disconnected in the KG. Returns the gap unchanged on any miss.

    K7.4 owner extension: `user_id`는 **세션 user_id**(qa.py가 thread)에서만 온다 — hit에서
    재유도 금지(hit의 owner_id는 콘텐츠 owner라 confused-deputy). owner-scope ON이면 top-2가
    owner-inclusive(회사 + caller 본인 personal)가 되고 `run_kg_template`이 owner-variant
    path_between으로 caller hop을 포함한 경로를 본다. **canonical 순서:** (1) reason 가드 →
    (2) `_is_personal()` node-kind early-return(**불변** — central은 이미 False, personal node는
    Neo4j 없음) → (3) `kg_enabled()` 단축 → (4) company-anchor 가드(personal-personal은 gate 왕복
    없이 차단) → (5) user_id-threaded gate.

    Best-effort by contract: the K4 gate never raises, so this does not wrap a broad
    except — local logic bugs should surface in tests; the answer-path caller (qa.py)
    swaps any unexpected failure for the original gap."""
    if gap.reason != "insufficient_grounding":
        return gap
    if _is_personal():
        # node-kind 가드(불변) — v1 그래프는 central company-only이고 personal node에는 Neo4j가
        # 없다(kg-model §1). central(company node)에선 이미 False라 통과한다 — owner-scope 확장에
        # 이 가드 완화/변경은 불필요하다(완화하면 personal node가 없는 Neo4j를 치는 회귀만 생김).
        return gap
    from orthus.kg.client import kg_enabled, kg_owner_scope_enabled

    if not kg_enabled():
        return gap  # off 기본 상태의 핫패스 gate/kg_query_runs write 회피
    owner_scope = kg_owner_scope_enabled()
    # _top_company_page_slugs는 in-memory `hits`에서 동작(추가 PG 비용 0). owner_scope ON이면
    # caller 본인 personal hit도 anchor로 받는다(노트-0 caller면 retrieve가 personal hit을 안
    # 줘 company-only와 byte-identical).
    anchors = _top_company_page_slugs(hits, n=2, owner_scope=owner_scope)
    if len(anchors) < 2:
        return gap
    # 핵심 anti-flood 가드(결정론, §4): top-2에 company-scope 페이지가 최소 1개일 때만 발화.
    # personal-personal 쌍은 **Neo4j 왕복 없이 즉시 차단** — D2에서 entity가 company-only라 두
    # personal 노트 사이 path_between은 구조적으로 empty → 가드 없으면 모든 personal 페이지에서
    # missing_link 발화 + 표현마다 새 row = 무한 백로그 노이즈가 된다.
    if not any(scope == "company" for _, scope in anchors):
        return gap
    cross_scope = any(scope == "personal" for _, scope in anchors)
    # K4 게이트 경유 — raw Cypher 입력 경로 없음, 가용성/예외도 status로 수렴(fail-open).
    # user_id 무조건 forward 안전: owner-scope OFF에서 gate는 user_id를 무시하고 company-only v1
    # 경로를 돈다(path_between은 predicate_kind!='company'라 reject도 없음) → company-company
    # missing_link byte-identical. owner-scope ON에서만 owner-variant가 caller hop을 본다.
    from orthus.kg.gate import KgQueryStatus, run_kg_template

    result = run_kg_template(
        user_id=user_id,
        template_name="path_between",
        params={"slug_a": anchors[0][0], "slug_b": anchors[1][0], "max_hops": 4},
    )
    if result.status is not KgQueryStatus.OK:
        return gap  # unavailable/reject/timeout/error → 원래 reason 유지
    if result.nodes:
        # 경로 존재 → missing_link 아님. owner-variant면 caller hop으로 이어진 경우도 여기서
        # suppress된다(missing_link는 owner-가시 경로조차 없을 때만 발화).
        return gap
    # OK + 두 materialized anchor page 사이 경로 없음 → missing_link. cross-scope(내 메모 1 +
    # 회사 1)는 중립/slug-free 카피(L7), company-company는 기존 generic 문구.
    message = (
        _CROSS_SCOPE_MISSING_LINK_MESSAGE
        if cross_scope
        else _build_message(gap.missing_topic, "missing_link")
    )
    return gap.model_copy(update={"reason": "missing_link", "message": message})


# --- backlog persistence --------------------------------------------------------


def record_gap(
    user_id: UUID,
    gap: WikiGap,
    *,
    source: str = "auto",
    context_wiki_slug: str | None = None,
    source_hits: list[WikiSourceRef] | None = None,
) -> UUID | None:
    """Upsert the gap into `data_gaps`, deduped per normalized question. Returns the
    row id, or None if the question normalizes to empty. A pure side effect: callers
    on the answer path swallow failures so an answer is never blocked by backlog I/O."""
    scope = "personal" if _is_personal() else "company"
    owner_id = user_id if scope == "personal" else None
    # L6(K7.4) — central(company node)의 cross-scope missing_link은 **owner-scoped**로 영속한다.
    # 현행 기본값(scope='company'/owner_id=None)을 그대로 쓰면 owner가 그 주제 관련 사적 노트를
    # 가졌다는 사실이 전 user/admin에 노출된다(존재/주제 oracle, K7 owner-only 경계 위반). 신호는
    # caller override가 아니라 **write-site에서 scope-tag된 anchor로 직접 파생**한다(fail-closed —
    # cross-scope 신호 누락/오배선이 company-scope row를 만들 수 없게). 발화 쌍 anchor 중 하나라도
    # caller 본인 personal이면 owner-scoped 강제. caller None은 cross-scope 불가(B-null-caller)라
    # 분기 자체가 안 탄다. company-company missing_link은 현행 company-scope 유지.
    if scope == "company" and gap.reason == "missing_link" and user_id is not None and source_hits:
        from orthus.kg.client import kg_owner_scope_enabled

        if kg_owner_scope_enabled():
            anchors = _top_company_page_slugs(source_hits, n=2, owner_scope=True)
            if any(tag == "personal" for _, tag in anchors):
                scope = "personal"
                owner_id = user_id
    norm = normalize_question(gap.missing_topic)
    if not norm:
        return None
    redacted_q = redact_pii_text(gap.missing_topic)
    now = datetime.now(UTC)
    settings = get_settings()
    resolved_context_slug = resolve_gap_context_wiki_slug(
        gap,
        explicit=context_wiki_slug,
        source_hits=source_hits or [],
    )
    with session() as s:
        existing = s.execute(
            select(
                data_gaps.c.gap_id,
                data_gaps.c.hit_count,
                data_gaps.c.context_wiki_slug,
            ).where(
                data_gaps.c.scope == scope,
                _owner_predicate(owner_id),
                data_gaps.c.question_norm == norm,
            )
        ).first()
        if existing is not None:
            # reason은 최신 관측을 따른다(latest-wins): KG가 내려간 동안의 재질문은
            # disconnect를 확인할 수 없으니 insufficient_grounding이 정직하고, 페이지가
            # 다시 연결되면 path 발견으로 자연히 missing_link에서 내려온다. 둘 다 self-correct.
            update_values = {
                "hit_count": existing.hit_count + 1,
                "last_seen_at": now,
                "updated_at": now,
                "reason": gap.reason,
                "top_score": gap.top_score,
                "question": redacted_q,
            }
            if existing.context_wiki_slug is None and resolved_context_slug is not None:
                update_values["context_wiki_slug"] = resolved_context_slug
            s.execute(
                update(data_gaps)
                .where(data_gaps.c.gap_id == existing.gap_id)
                .values(**update_values)
            )
            s.commit()
            return existing.gap_id
        gap_id = uuid4()
        s.execute(
            insert(data_gaps).values(
                gap_id=gap_id,
                scope=scope,
                owner_id=owner_id,
                node_id=settings.node_id,
                question_norm=norm,
                question=redacted_q,
                reason=gap.reason,
                top_score=gap.top_score,
                context_wiki_slug=resolved_context_slug,
                suggested_fields=[],
                suggestion_status="pending",
                hit_count=1,
                status="open",
                source=source,
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
        )
        s.commit()
        return gap_id


def record_feedback(
    user_id: UUID, question: str, *, context_wiki_slug: str | None = None
) -> UUID | None:
    """User pressed '이 답변 부족해요'. Record the question as a user-declared gap so
    the backlog and an LLM suggestion can be produced even without retrieval hits."""
    gap = WikiGap(
        detected=True,
        reason="insufficient_grounding",
        missing_topic=question,
        top_score=None,
        message=_build_message(question, "insufficient_grounding"),
    )
    return record_gap(user_id, gap, source="feedback", context_wiki_slug=context_wiki_slug)


def list_gaps(user_id: UUID, *, status: str | None = None, limit: int = 100) -> list[DataGap]:
    """Backlog rows visible to the caller's node scope, busiest first."""
    where = [_owner_read_predicate(user_id)]
    if status is not None:
        where.append(data_gaps.c.status == status)
    with session() as s:
        rows = s.execute(
            select(data_gaps)
            .where(*where)
            .order_by(
                data_gaps.c.status, data_gaps.c.hit_count.desc(), data_gaps.c.last_seen_at.desc()
            )
            .limit(limit)
        ).all()
    return [_to_data_gap(r) for r in rows]


def list_gaps_for_wiki_page(
    user_id: UUID,
    context_wiki_slug: str,
    *,
    status: str = "open",
    limit: int = 20,
) -> list[DataGap]:
    """Open backlog rows linked to a node-local wiki page slug."""
    slug = clean_wiki_slug(context_wiki_slug)
    if slug is None:
        return []
    with session() as s:
        rows = s.execute(
            select(data_gaps)
            .where(
                _owner_read_predicate(user_id),
                data_gaps.c.context_wiki_slug == slug,
                data_gaps.c.status == status,
            )
            .order_by(data_gaps.c.hit_count.desc(), data_gaps.c.last_seen_at.desc())
            .limit(limit)
        ).all()
    return [_to_data_gap(r) for r in rows]


def set_gap_status(user_id: UUID, gap_id: UUID, status: str) -> DataGap | None:
    """Resolve / dismiss / reopen a backlog row in the caller's scope."""
    now = datetime.now(UTC)
    with session() as s:
        updated = s.execute(
            update(data_gaps)
            .where(
                data_gaps.c.gap_id == gap_id,
                _owner_read_predicate(user_id),
            )
            .values(status=status, updated_at=now)
            .returning(data_gaps)
        ).first()
        s.commit()
    return _to_data_gap(updated) if updated is not None else None


def get_gap(user_id: UUID, gap_id: UUID) -> DataGap | None:
    with session() as s:
        row = s.execute(
            select(data_gaps).where(
                data_gaps.c.gap_id == gap_id,
                _owner_read_predicate(user_id),
            )
        ).first()
    return _to_data_gap(row) if row is not None else None


# --- LLM field suggestion (on demand only) --------------------------------------

_SUGGEST_SYSTEM = (
    "You help a company/personal knowledge base close data gaps. A user asked a "
    "question the wiki could not answer well. Propose the MISSING information that "
    "should be written down to answer it next time. Return JSON ONLY:\n"
    '{"target": "<short where-to-put-it, e.g. 회사 Notion \'회사 개요\' 페이지>", '
    '"connector": "<connector slug to sync, e.g. notion>", '
    '"sections": [{"title": "<group>", "items": ["<concrete field/fact to add>", ...]}]}\n'
    "Keep it concrete and minimal (2-4 sections, 2-5 items each). Answer in the "
    "question's language. Do not invent facts; only name what should be documented."
)


def generate_suggestion(
    user_id: UUID, gap_id: UUID, *, chat_model: ChatModel | None = None
) -> DataGap | None:
    """Fill a backlog row's LLM field suggestion. One chat call; on parse failure it
    falls back to a deterministic suggestion and still marks the row ready."""
    gap = get_gap(user_id, gap_id)
    if gap is None:
        return None
    # Solar: 0/12 off-spec section counts, against 4/12 for both A.X and gpt-4o-mini.
    # A malformed suggestion is not a soft failure here — `_parse_suggestion` drops to
    # a deterministic stub, so the operator gets boilerplate instead of an answer.
    chat = chat_model or get_chat_model_for(TASK_GAP_SUGGEST)
    with audit("wiki.gap_suggest") as span:
        raw = chat.complete(
            _SUGGEST_SYSTEM,
            f"Question: {gap.question}\nReason it failed: {gap.reason}",
            json_only=True,
        )
        span.add_meta(gap_id=str(gap_id), reason=gap.reason)
    target, connector, sections = _parse_suggestion(raw)
    now = datetime.now(UTC)
    with session() as s:
        updated = s.execute(
            update(data_gaps)
            .where(data_gaps.c.gap_id == gap_id)
            .values(
                suggested_target=target,
                suggested_connector=connector,
                suggested_fields=[sec.model_dump() for sec in sections],
                suggestion_status="ready",
                updated_at=now,
            )
            .returning(data_gaps)
        ).first()
        s.commit()
    return _to_data_gap(updated) if updated is not None else None


def _parse_suggestion(
    raw: str,
) -> tuple[str | None, str | None, list[GapSuggestionSection]]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        # Deterministic fallback so the row is still actionable.
        target = (
            "개인 Notion 또는 /editor 문서" if _is_personal() else "회사 Notion 또는 /editor 문서"
        )
        return (
            target,
            "notion",
            [
                GapSuggestionSection(
                    title="추가할 정보",
                    items=["질문에 답할 핵심 사실/정의를 정리한 문서를 작성하세요."],
                )
            ],
        )
    target = data.get("target") if isinstance(data.get("target"), str) else None
    connector = data.get("connector") if isinstance(data.get("connector"), str) else None
    sections: list[GapSuggestionSection] = []
    for sec in data.get("sections", []) if isinstance(data.get("sections"), list) else []:
        if not isinstance(sec, dict):
            continue
        title = sec.get("title")
        items = sec.get("items")
        if not isinstance(title, str):
            continue
        clean_items = [i for i in items if isinstance(i, str)] if isinstance(items, list) else []
        sections.append(GapSuggestionSection(title=title, items=clean_items))
    return target, connector, sections


# --- helpers --------------------------------------------------------------------


def _is_personal() -> bool:
    return get_settings().node_kind == "personal"


def _owner_predicate(owner_id: UUID | None):
    if owner_id is None:
        return data_gaps.c.owner_id.is_(None)
    return data_gaps.c.owner_id == owner_id


def _owner_read_predicate(user_id: UUID):
    """Fail-closed scope+owner visibility for data-gap read paths (P8.1).

    Personal node: only the caller's own personal rows. Company (central) node:
    shared company-scope rows (owner_id NULL) plus the caller's own personal-scope
    rows; never another user's personal rows. This mirrors the wiki retrieve /
    structured `company OR own-personal` visibility so owner-scoped personal gaps
    surface for their owner on the central node. Permanent privacy boundary, not
    gated on owner_scope_enabled (docs/p8-central-consolidation.md §3/§5-A).
    By-id read/resolve paths (`get_gap`, `set_gap_status`) also use this predicate
    so a row is only fetched/mutated by its owner; the dedup-upsert in `record_gap`
    keeps the exact-match `_owner_predicate` for the caller's own scope insert key."""
    own_personal = (data_gaps.c.scope == "personal") & (data_gaps.c.owner_id == user_id)
    if _is_personal():
        return own_personal
    company = (data_gaps.c.scope == "company") & data_gaps.c.owner_id.is_(None)
    return or_(company, own_personal)


def resolve_gap_context_wiki_slug(
    gap: WikiGap,
    *,
    explicit: str | None = None,
    source_hits: list[WikiSourceRef] | None = None,
) -> str | None:
    explicit_slug = clean_wiki_slug(explicit)
    if explicit_slug is not None:
        return explicit_slug
    if gap.reason not in {"weak_retrieval", "insufficient_grounding", "missing_link"}:
        return None
    first_hit = (source_hits or [None])[0]
    if first_hit is None:
        return None
    return clean_wiki_slug(first_hit.page_slug)


def _to_data_gap(row) -> DataGap:
    sections = [
        GapSuggestionSection(**sec) for sec in (row.suggested_fields or []) if isinstance(sec, dict)
    ]
    return DataGap(
        gap_id=row.gap_id,
        scope=row.scope,
        reason=row.reason,
        question=row.question,
        top_score=row.top_score,
        suggested_target=row.suggested_target,
        suggested_connector=row.suggested_connector,
        context_wiki_slug=row.context_wiki_slug,
        suggested_fields=sections,
        suggestion_status=row.suggestion_status,
        hit_count=row.hit_count,
        status=row.status,
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_seen_at=row.last_seen_at,
    )


__all__ = [
    "WEAK_SCORE_THRESHOLD",
    "detect_gap",
    "maybe_missing_link",
    "normalize_question",
    "record_gap",
    "record_feedback",
    "list_gaps",
    "list_gaps_for_wiki_page",
    "set_gap_status",
    "get_gap",
    "generate_suggestion",
    "resolve_gap_context_wiki_slug",
]
