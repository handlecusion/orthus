"""K4 — KG 읽기 게이트: `run_kg_template()` (구현 명세 §6.2).

structured 검증 게이트(sqlglot 5-reject)와 동형의 read-only 게이트다. 이
모듈에는 **raw Cypher 입력 경로가 없다** — `templates.py` registry의 사전
컴파일 문자열만 실행하고, 모든 실행(reject 포함)은 `kg_query_runs` +
`audit("kg.retrieve")`에 남는다. 실행은 `client.run_read`(READ 세션 +
transaction timeout) 단일 진입로를 지나며, 이 모듈에 write 세션/neo4j
import는 등장하지 않는다(§2.3 — last_accessed_at SET은 `store.py`가 보유).

결과 매핑 규약(§6.1): 템플릿별 custom 매퍼 없이 공통 매퍼 하나가 driver
record의 Node/Relationship/Path를 재귀 평탄화한다. driver 타입은 duck-typing
으로 식별한다(Node=`labels`, Path=`relationships`, Relationship=`start_node`)
— neo4j import를 client.py 밖으로 내보내지 않는 lazy import 계약(§2.1)의
구현 수단이다.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import insert, select

from orthus.audit.logger import audit
from orthus.audit.redact import redact_pii, redact_pii_text
from orthus.db import session
from orthus.kg import store
from orthus.kg.client import (
    is_timeout_error,
    kg_available,
    kg_enabled,
    kg_owner_scope_enabled,
    kg_read_session,
    run_read,
)
from orthus.kg.schema import Label
from orthus.kg.templates import TEMPLATES
from orthus.kg.visibility import (
    COMPANY_NS,
    caller_has_personal_pages,
    entity_node_key_for,
    owner_inclusive_read_where,
    owner_scope_columns,
    personal_slug_log_value,
    resolve_slug,
)
from orthus.schemas.canonical import (
    KgFramingsSpanMeta,
    KgGraphEdge,
    KgGraphNode,
    KgOverview,
    KgPathFraming,
    KgPathFramings,
    OwnerFootprint,
)
from orthus.settings import get_settings
from orthus.tables import documents, kg_entities, kg_query_runs, structured_rows, wiki_pages


class KgQueryStatus(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    ERROR = "error"


# reject_reason 값(§6.2 + K4 구현 — 회귀 세트가 고정):
#   "kg_disabled" | "kg_unavailable" | "unknown_template" | "deferred_template"
#   | "owner_scope_required" | "invalid_params:<field>" | "slug_not_found:<slug>"
#   | "timeout" | "driver_error:<ExcType>" | "mapping_error:<ExcType>"
#   | "internal_error"
# (deferred_template은 K7.2 §7 — owner-scope 활성 시 owner-variant 없는 deferred 템플릿
#  (entity_mentions, D2)을 소비자 경로에서 fail-closed로 차단한다.)
# (owner_scope_required는 K7.2 — company-framing 템플릿(path_between_company, framing A)은
#  owner-scope 전용이라 flag-OFF에서 부르면 page_id 바인딩이 없어 driver_error가 된다.
#  deferred의 거울상으로 flag-OFF에서 fail-closed reject한다.)
# (mapping_error/internal_error는 fail-open 견고화에서 추가 — 매핑 예외와 driver
#  예외를 분리해 kg_query_runs 진단성을 보존한다. endpoint는 셋 다 supported:false로 수렴.)


class KgTemplateResult(BaseModel):
    status: KgQueryStatus
    reject_reason: str | None = None
    nodes: list[KgGraphNode] = Field(default_factory=list)
    edges: list[KgGraphEdge] = Field(default_factory=list)
    truncated: bool = False
    # L4(2026-06-16) — kg_query_runs에 기록할 params(개인-scope resolve된 slug는 hash).
    # None이면 run_kg_template이 raw params를 기록한다(flag-off/early-reject 경로).
    log_params: dict | None = None
    duration_ms: int | None = None
    # B1 wire-layer — must-be-zero anomaly. 호출자가 "tripwire가 노드를 숨겼을 수 있다"는
    # 신호를 보수적 skip 판정에 쓸 수 있게 노출한다(get_wiki_page_graph의 has_claim_neighbor
    # skip guard). 정상 경로에서는 항상 0이다.
    boundary_tripwire_drops: int = 0


def run_kg_template(
    *,
    user_id: UUID | None,
    template_name: str,
    params: dict[str, Any],
    correlation_id: UUID | None = None,
    limit: int | None = None,
) -> KgTemplateResult:
    """등록 템플릿 1개를 게이트를 거쳐 실행한다 — K4 API/K4b/K5의 유일한 진입로.

    `limit`은 내부 호출자(K4b/K5)용이며 상한은 항상 `settings.kg_query_limit`이
    cap한다 — K4 API는 limit 파라미터를 노출하지 않는다(서버 고정, §6.2 6단계).
    어떤 분기로 끝나든 `kg_query_runs` 기록을 지난다(7단계).

    **이 함수는 예외를 던지지 않는다** — graph 분기 실패는 호출자(K4 endpoint /
    K4b 라우터)에서 200 `supported:false`로 수렴해야 한다는 fail-open 계약
    (kg-model §4 / 구현 명세 §2.6)의 구현이다. 게이트 안의 driver 예외는 status로
    정규화하고, 기록/관측 경로(`_record_run`/last_accessed)는 best-effort이며,
    audit 인프라 자체의 예외(PG enter/exit write 실패 등)까지 최상위에서 잡아
    `ERROR` 결과로 떨어뜨린다 — 어떤 경로로도 endpoint 500이 나지 않는다.
    """
    settings = get_settings()
    started = time.monotonic()
    try:
        with audit("kg.retrieve", correlation_id=correlation_id) as span:
            result = _gate(template_name, params, limit, settings, span, user_id)
            result.duration_ms = int((time.monotonic() - started) * 1000)
            span.add_meta(
                template_name=template_name,
                status=result.status.value,
                reject_reason=result.reject_reason,
                result_count=len(result.nodes),
                truncated=result.truncated,
            )
            _record_run_best_effort(
                template_name=template_name,
                # L4 — 개인-scope slug는 hash된 log_params로 기록(없으면 raw params).
                params=result.log_params if result.log_params is not None else params,
                result=result,
                user_id=user_id,
                correlation_id=span.correlation_id,
                span=span,
            )
            if result.status is KgQueryStatus.OK:
                _touch_last_accessed_best_effort(result.nodes, span)
        return result
    except Exception:  # noqa: BLE001 — fail-open 최종 방어선 (audit 인프라 예외 등)
        return KgTemplateResult(
            status=KgQueryStatus.ERROR,
            reject_reason="internal_error",
            duration_ms=int((time.monotonic() - started) * 1000),
        )


# --- two-framing 오케스트레이션 (K7.4) — 직접 연결(전부 회사) vs 내 메모 경유 --------------
#
# 신규 템플릿 0 — 기존 run_kg_template를 (조건부) 2회 호출한다(registry-completeness CI 유지).
# flag-ON 전용: 호출자 try_graph_answer가 kg_owner_scope_enabled()를 1회 읽고 ON일 때만,
# 2-slug path_between 바인딩에 한해 호출한다(§2 step 0 / §5 (4c) — flag-OFF는 단일 path_between
# v1 경로). framing A = path_between_company(company predicate, flag-ON 전용), framing B =
# path_between owner-variant(visibility_predicate). B가 spine(grounding + .graph 출처)이다.

_FRAMING_A_LABEL = "direct_company"  # framing A — scope-enum 아닌 식별 키(L7/§9 카피는 FE)
_FRAMING_B_LABEL = "via_personal"  # framing B


class PathFramingsOutcome(BaseModel):
    """run_path_framings 성공 결과. `framings`는 RoutedAnswer.path_framings로 싣고, `spine`
    (=framing B, owner-visible path)에서 graph.py가 grounding + `.graph`를 파생한다."""

    framings: KgPathFramings

    @property
    def spine(self) -> KgPathFraming:
        """grounding + `.graph` 출처. spine은 owner-visible path = framing B이고 모든 분기에서
        framings의 **마지막** 원소다(short-circuit: [A, 합성B]; dual: [A, B] 또는 A-error 시 [B]).
        별도 spine_* 필드로 복제하지 않는다 — framings[-1]과 어긋날 여지를 구조적으로 제거한다."""
        return self.framings.framings[-1]


def _framing_from_result(label: str, result: KgTemplateResult) -> KgPathFraming:
    """KgTemplateResult → KgPathFraming. personal_dependent는 **반환 scope**에서 파생한다.
    노드는 `_map_records`가 SoT로 계산한 `is_own_personal`을 재사용하고(raw `.scope` 재분류 대신 —
    boundary 의미가 바뀌어도 wire tripwire와 같은 신호를 따른다), 엣지는 `is_own_personal` 필드가
    없어 `.scope`를 본다. tripwire가 foreign-personal을 이미 drop하므로 'personal'은 caller 본인
    hop뿐이다 — 끝점 scope/템플릿 정체성으로 단정하지 않는다. graph.py는 이 함수를 단일-template
    경로의 spine-shape 매퍼로도 재사용한다(그땐 personal_dependent 미사용)."""
    personal_dependent = any(n.is_own_personal for n in result.nodes) or any(
        e.scope == "personal" for e in result.edges
    )
    return KgPathFraming(
        label=label,
        nodes=result.nodes,
        edges=result.edges,
        path_slugs=[n.slug for n in result.nodes if n.slug],
        personal_dependent=personal_dependent,
        truncated=result.truncated,
    )


def _framings_demote_cause(result: KgTemplateResult) -> str:
    """framing demote(framing_count=0)의 bounded cause 토큰. gate 오류는 reject_reason의 coarse
    카테고리(`:` 앞 — timeout/driver_error/mapping_error 등), OK-but-empty는 'empty_path'. detail/
    slug 미포함이라 span allowlist에 안전하면서 transient 실패 vs genuine empty를 구분한다(§1)."""
    if result.status is not KgQueryStatus.OK:
        return (result.reject_reason or "kg_unavailable").split(":", 1)[0]
    return "empty_path"


def run_path_framings(
    *,
    user_id: UUID | None,
    slug_a: str,
    slug_b: str,
    max_hops: int,
    correlation_id: UUID | None = None,
    limit: int | None = None,
) -> PathFramingsOutcome | None:
    """K7.4 — owner-scope 2-framing 오케스트레이터. **flag-ON 전용**(호출자가 보장).

    반환: 성공 시 `PathFramingsOutcome`(framings + spine=framing B), demote 시 `None`.
    fail-open: 어떤 실패/예외도 None을 돌려주며 raise하지 않는다 — graph.py가 오늘처럼 wiki로
    demote한다(절대 500 금지). framing B가 spine이라 B fail/empty면 None(§2 step 5).

    row 예산(per-attempt, run_kg_template이 reject/error에도 1 row): all-company-no-personal
    short-circuit = 1; cross-scope/personal-bridge dual = 2. short-circuit은 caller가 personal
    page를 1개도 안 가질 때만(`caller_has_personal_pages` False) — 그땐 owner-variant가
    company-only와 구조적으로 같아 framing A 1회로 정직하게 B를 합성한다(§2 step 2).
    """
    params = {"slug_a": slug_a, "slug_b": slug_b, "max_hops": max_hops}
    try:
        with audit("router.graph.framings", correlation_id=correlation_id) as span:
            short_circuit = not caller_has_personal_pages(user_id)
            if short_circuit:
                # framing A(path_between_company) 1회. caller에 personal hop이 없으니 owner-variant
                # B는 구조적으로 A와 동일 → A를 in-Cypher로 실행하고 B를 server-side로 LABEL한다
                # (B2: truncation은 A의 len==limit에서 상속, 중복 2번째 Cypher 왕복만 회피).
                res_a = run_kg_template(
                    user_id=user_id,
                    template_name="path_between_company",
                    params=params,
                    correlation_id=span.correlation_id,
                    limit=limit,
                )
                if res_a.status is not KgQueryStatus.OK or not res_a.nodes:
                    span.add_meta(
                        **KgFramingsSpanMeta(
                            framing_count=0,
                            short_circuit=True,
                            demote_cause=_framings_demote_cause(res_a),
                        ).model_dump()
                    )
                    return None
                framing_a = _framing_from_result(_FRAMING_A_LABEL, res_a)
                # framing B 합성 — 같은 all-company 경로. personal_dependent=False면 same_as_direct는
                # 파생값으로 자동 True(personal 캡션 withheld).
                framing_b = framing_a.model_copy(
                    update={"label": _FRAMING_B_LABEL, "personal_dependent": False}
                )
                framings = [framing_a, framing_b]
            else:
                # dual-path — framing A(company predicate) + framing B(owner-variant, spine).
                res_a = run_kg_template(
                    user_id=user_id,
                    template_name="path_between_company",
                    params=params,
                    correlation_id=span.correlation_id,
                    limit=limit,
                )
                res_b = run_kg_template(
                    user_id=user_id,
                    template_name="path_between",
                    params=params,
                    correlation_id=span.correlation_id,
                    limit=limit,
                )
                if res_b.status is not KgQueryStatus.OK or not res_b.nodes:
                    # B가 spine — B fail/empty면 demote(§2 step 5). A 성공 여부 무관.
                    span.add_meta(
                        **KgFramingsSpanMeta(
                            framing_count=0,
                            short_circuit=False,
                            demote_cause=_framings_demote_cause(res_b),
                        ).model_dump()
                    )
                    return None
                framing_b = _framing_from_result(_FRAMING_B_LABEL, res_b)
                if res_a.status is KgQueryStatus.OK and res_a.nodes:
                    framings = [_framing_from_result(_FRAMING_A_LABEL, res_a), framing_b]
                else:
                    framings = [framing_b]  # A error → B 단독 제시(§2 step 5)
            span.add_meta(
                **KgFramingsSpanMeta(
                    framing_count=len(framings),
                    personal_dependent=framing_b.personal_dependent,
                    same_as_direct=framing_b.same_as_direct,
                    short_circuit=short_circuit,
                ).model_dump()
            )
            return PathFramingsOutcome(framings=KgPathFramings(framings=framings))
    except Exception:  # noqa: BLE001 — fail-open: 어떤 실패도 wiki demote(절대 raise 금지)
        return None


# --- owner footprint (K7.3) — caller 본인 personal 노드/엣지 카운트 ------------------
#
# read 술어(visibility_predicate: `company OR owner=caller`)가 **아니라** own-personal-only
# 술어(`scope='personal' AND owner_id=$caller`)다 — footprint는 "내 personal footprint"라
# company는 무관하다(kg-k7.3-plan §4.1/T2). 집계(scalar count)라 path-template registry나
# `_map_records`(node/edge 매핑)를 타지 않고, run_read(READ 세션+timeout) + audit +
# kg_query_runs **규율만 공유**한다. $caller는 세션에서만 바인딩한다(client 입력 금지 — T1).
_FOOTPRINT_NODES_CYPHER = (
    "MATCH (n) WHERE n.scope = 'personal' AND n.owner_id = $caller "
    "RETURN labels(n)[0] AS label, count(n) AS c"
)
# 방향 패턴 `-[r]->()`로 각 엣지를 1회만 센다(무방향 `-[r]-()`은 2회 중복 카운트).
_FOOTPRINT_EDGES_CYPHER = (
    "MATCH ()-[r]->() WHERE r.scope = 'personal' AND r.owner_id = $caller RETURN count(r) AS c"
)


def owner_footprint(
    caller_id: UUID | None, *, correlation_id: UUID | None = None
) -> OwnerFootprint:
    """caller **본인**의 personal 노드/엣지 카운트 — owner-only read, 새 write/경계 표면 0.

    fail-open: flag-off / owner-scope off / Neo4j 미가용 / null caller / 어떤 예외든
    **빈 footprint**를 돌려준다(예외 미전파 — endpoint 500 방지, kg-model §4). `$caller`는
    호출자(endpoint)가 세션 `AuthenticatedUser.user_id`로만 넘긴다(client 입력 금지 — T1).
    집계는 `_map_records` tripwire 보호 밖이라(scalar) 술어가 by-construction 옳아야 하고,
    전용 경계 테스트가 이를 고정한다(kg-k7.3-plan §5)."""
    empty = OwnerFootprint()
    # B-null-caller — company에는 owner footprint가 없다. owner-scope off면 personal 노드가
    # 애초에 투영되지 않으므로 빈 footprint(불필요한 Neo4j 왕복도 회피).
    if caller_id is None or not kg_enabled() or not kg_owner_scope_enabled():
        return empty
    try:
        with audit("kg.footprint", correlation_id=correlation_id) as span:
            if not kg_available():
                return empty
            caller_s = str(caller_id)
            node_recs = run_read(_FOOTPRINT_NODES_CYPHER, {"caller": caller_s})
            edge_recs = run_read(_FOOTPRINT_EDGES_CYPHER, {"caller": caller_s})
            by_label: dict[str, int] = {}
            total = 0
            for rec in node_recs:
                vals = list(rec.values())
                label, count = vals[0], int(vals[1])
                if label:
                    by_label[label] = count
                total += count
            edge_count = int(list(edge_recs[0].values())[0]) if edge_recs else 0
            # genuine 측정 성공만 status=ready — count==0이어도 "측정했고 0개"라 정직(O1).
            # 위 fail-open empty들(default status=unavailable)과 구분된다.
            fp = OwnerFootprint(
                node_count=total, edge_count=edge_count, by_label=by_label, status="ready"
            )
            span.add_meta(node_count=total, edge_count=edge_count, owner_scope_applied=True)
            _record_footprint_run_best_effort(
                user_id=caller_id, result=fp, correlation_id=span.correlation_id, span=span
            )
            return fp
    except Exception:  # noqa: BLE001 — fail-open: footprint 실패가 endpoint 500이 되지 않게
        return empty


def kg_rebuild_in_progress() -> bool:
    """endpoint glue용(W2 O1/FE) — `:KgMeta.rebuild_in_progress` 1회 lookup.

    footprint/graph 엔드포인트가 full rebuild 중(half-projected `scope=None→company`
    오노출 가능 윈도우)에 보류 상태를 렌더하게 한다. **어떤 예외/미가용/flag-off도
    False**(fail-open: 못 읽으면 정상 경로가 자체 fail-open으로 처리). 드문 윈도우라
    per-request 1회 lookup(:KgMeta는 단일 노드, indexed)이면 충분하다."""
    if not kg_enabled() or not kg_available():
        return False
    try:
        with kg_read_session() as rs:
            meta = store.get_meta(rs)
        return bool(meta is not None and meta.rebuild_in_progress)
    except Exception:  # noqa: BLE001 — 못 읽으면 정상 경로로(fail-open)
        return False


def _record_footprint_run_best_effort(
    *, user_id: UUID, result: OwnerFootprint, correlation_id: UUID, span: Any
) -> None:
    """kg_query_runs에 footprint 실행 1행(best-effort). params는 `{}` — slug 없음, owner_id는
    **절대** 기록하지 않는다(user_id 컬럼만, T3). 기록 실패는 관측 누락일 뿐(fail-open)."""
    try:
        with session() as s:
            s.execute(
                insert(kg_query_runs).values(
                    run_id=uuid4(),
                    template_name="owner_footprint",
                    params_redacted={},
                    status=KgQueryStatus.OK.value,
                    reject_reason=None,
                    duration_ms=None,
                    result_count=result.node_count,
                    user_id=user_id,
                    correlation_id=correlation_id,
                )
            )
            s.commit()
    except Exception as exc:  # noqa: BLE001 — 기록 실패는 관측 누락
        span.add_meta(footprint_record_failed=type(exc).__name__)


# --- company KG overview — 전용 지식그래프 화면 헤더 집계 --------------------------------
#
# footprint(own-personal-only)와 동형의 집계 경로다: read 술어(`company OR owner=caller`)가
# **아니라** company-only 술어(`scope='company'`)이며 `$caller`를 바인딩하지 않는다 — 이 표면은
# company 전체 통계이고 owner-specific 값이 아니다. 타 owner personal 노드/엣지는 술어상
# 애초에 매칭되지 않는다(본인 personal 카운트는 owner_footprint가 준다). scalar count라
# path-template registry/`_map_records`(tripwire)를 타지 않고 run_read + audit + kg_query_runs
# **규율만** 공유한다. `:KgMeta`/`:OutboxApplied` 동기화 메타는 scope 속성이 없어 제외된다.
_OVERVIEW_NODES_CYPHER = (
    "MATCH (n) WHERE n.scope = 'company' RETURN labels(n)[0] AS label, count(n) AS c"
)
# 방향 패턴 `-[r]->()`로 각 엣지를 1회만 센다(무방향은 2회 중복).
_OVERVIEW_EDGES_CYPHER = "MATCH ()-[r]->() WHERE r.scope = 'company' RETURN count(r) AS c"
_OVERVIEW_CONFLICTS_CYPHER = (
    "MATCH ()-[r:CONFLICTS_WITH]->() WHERE r.scope = 'company' "
    "RETURN coalesce(r.status, 'UNRESOLVED') AS status, count(r) AS c"
)

# 집계 결과 process-global TTL 캐시(`entities_present` 동형 — monotonic 단일 슬롯).
#
# **왜 필요한가:** 위 세 쿼리는 라벨 무관 전체 스캔이라 Neo4j page cache가 식으면 실측 노드
# 1.2s + 엣지 1.3s(워밍 시 각각 ~0.1s)로, run_read의 transaction timeout(`kg_query_timeout_ms`
# 기본 2s)에 여유가 40%뿐이다. 콜드 + 동시요청(화면 진입 시 overview·footprint·graph 동시)이면
# 2s를 넘겨 타임아웃 → 아래 blanket except → `unavailable` 배너가 뜬다. TTL 캐시로 스캔 빈도를
# 분당 1회로 낮춰 그 창을 좁힌다(카운트는 rebuild/outbox로 천천히 변해 60s stale 무해).
#
# **성공만 캐시한다** — `unavailable`을 캐시하면 Neo4j가 곧바로 복구돼도 장애를 TTL만큼
# 연장한다(kg_available 음성캐시와 달리 여기선 재시도가 싸지 않을 이유가 없다). 실패는 다음
# 요청이 즉시 재시도한다. flag-off(`disabled`)도 캐시 대상이 아니다(그 경로는 이미 Neo4j 미왕복).
_OVERVIEW_TTL_S = 60.0
_overview_lock = Lock()
_overview_until: float = 0.0
_overview_value: KgOverview | None = None


def invalidate_overview_cache() -> None:
    """TTL 캐시 리셋 — 테스트 격리용(각 테스트가 자기 monkeypatch 조건을 측정하게).
    운영에서는 TTL 만료가 유일한 무효화 경로다."""
    global _overview_until, _overview_value
    with _overview_lock:
        _overview_until = 0.0
        _overview_value = None


def company_kg_overview(*, correlation_id: UUID | None = None) -> KgOverview:
    """company-scope KG 집계(노드/엣지/라벨별/엔티티/모순 카운트) — read-only, 새 write 표면 0.

    fail-open: flag-off는 `disabled`(Neo4j 미왕복), Neo4j 미가용/어떤 예외든 `unavailable`을
    돌려준다(예외 미전파 — endpoint 500 방지, kg-model §4). `$caller`를 바인딩하지 않으므로
    타 owner personal 데이터는 구조적으로 집계에 들어오지 않는다(술어=`scope='company'`).
    genuine 측정 성공만 `ready`(count==0이어도 정직 — O1, footprint와 동형).

    성공 결과는 `_OVERVIEW_TTL_S` 동안 캐시한다(전체 스캔 비용 — 상수 주석 참조)."""
    global _overview_until, _overview_value
    # flag-off는 아예 측정하지 않는다(disabled — Neo4j 왕복 회피). owner_footprint는 empty를
    # 돌리지만 여기선 FE가 "비활성 배너"를 구분 렌더하도록 별도 disabled 상태를 준다.
    if not kg_enabled():
        return KgOverview(status="disabled")
    now = time.monotonic()
    with _overview_lock:
        if _overview_value is not None and now < _overview_until:
            return _overview_value
    empty = KgOverview()  # status="unavailable" — 측정 실패 안전값
    try:
        with audit("kg.overview", correlation_id=correlation_id) as span:
            if not kg_available():
                return empty
            by_label: dict[str, int] = {}
            node_total = 0
            for rec in run_read(_OVERVIEW_NODES_CYPHER, {}):
                vals = list(rec.values())
                label, count = vals[0], int(vals[1])
                if label:
                    by_label[label] = count
                node_total += count
            edge_recs = run_read(_OVERVIEW_EDGES_CYPHER, {})
            edge_count = int(list(edge_recs[0].values())[0]) if edge_recs else 0
            unresolved = 0
            resolved = 0
            for rec in run_read(_OVERVIEW_CONFLICTS_CYPHER, {}):
                vals = list(rec.values())
                status_val, count = str(vals[0]), int(vals[1])
                if status_val == "RESOLVED":
                    resolved += count
                else:
                    unresolved += count
            overview = KgOverview(
                node_count=node_total,
                edge_count=edge_count,
                by_label=by_label,
                entity_count=by_label.get(Label.ENTITY.value, 0),
                unresolved_conflicts=unresolved,
                resolved_conflicts=resolved,
                status="ready",
            )
            # count만 기록(PII 없음 — 이름/slug/owner_id 미포함).
            span.add_meta(
                node_count=node_total,
                edge_count=edge_count,
                unresolved_conflicts=unresolved,
            )
            _record_overview_run_best_effort(
                result=overview, correlation_id=span.correlation_id, span=span
            )
            # genuine 측정 성공만 캐시(실패/disabled는 미캐시 — 상수 주석 참조).
            with _overview_lock:
                _overview_value = overview
                _overview_until = time.monotonic() + _OVERVIEW_TTL_S
            return overview
    except Exception:  # noqa: BLE001 — fail-open: overview 실패가 endpoint 500이 되지 않게
        return empty


def _record_overview_run_best_effort(
    *, result: KgOverview, correlation_id: UUID, span: Any
) -> None:
    """kg_query_runs에 overview 실행 1행(best-effort). params는 `{}`(slug/이름 없음), user_id도
    없음(company 집계라 caller-bound 아님). 기록 실패는 관측 누락일 뿐(fail-open)."""
    try:
        with session() as s:
            s.execute(
                insert(kg_query_runs).values(
                    run_id=uuid4(),
                    template_name="kg_overview",
                    params_redacted={},
                    status=KgQueryStatus.OK.value,
                    reject_reason=None,
                    duration_ms=None,
                    result_count=result.node_count,
                    user_id=None,
                    correlation_id=correlation_id,
                )
            )
            s.commit()
    except Exception as exc:  # noqa: BLE001 — 기록 실패는 관측 누락
        span.add_meta(overview_record_failed=type(exc).__name__)


# --- E1b expand (label,id) 선검증 (D2) ----------------------------------------------

_EXPAND_TEMPLATES = frozenset({"expand_node", "expand_entity"})

# label → (PG table, id 컬럼). owner-scope precheck의 SoT — (label,id)가 caller 가시 scope에
# PG 실재하는지 확인. WikiPage/Claim/Source는 모두 wiki_pages(page_id). Entity는 별도(kg_entities).
_EXPAND_PG_TABLE: dict[str, tuple[Any, str]] = {
    "WikiPage": (wiki_pages, "page_id"),
    "WikiClaim": (wiki_pages, "page_id"),
    "WikiSource": (wiki_pages, "page_id"),
    "Document": (documents, "doc_id"),
    "StructuredFact": (structured_rows, "row_id"),
}


def _expand_precheck(
    template_name: str, parsed: BaseModel, owner_scope: bool, user_id: UUID | None
) -> tuple[dict[str, Any] | None, str | None, dict]:
    """(bind, reject_reason, log_params). reject_reason non-None이면 stage-4 실패(404 재료).

    expand_entity(company_always): parsed.entity_key는 **unprefixed**(endpoint가 namespace strip).
      kg_entities에서 scope='company' 실재 확인. Cypher는 namespaced를 바인딩해야 하므로
      bind["entity_key"]=entity_node_key_for(COMPANY_NS, unprefixed)(Neo4j 노드 키 일치). $caller 미바인딩.
    expand_node(owner 5라벨): _EXPAND_PG_TABLE로 (label,id)가 caller 가시 scope에 실재하는지 PG 확인.
      owner_scope ON = owner_inclusive_read_where(scope, RAW owner, caller)(E1a SoT — company OR owner==caller),
      OFF = company-only. 없으면 404. $id + (ON일 때만) $caller 바인딩. personal이면 log id 해싱(L4).
    """
    if template_name == "expand_entity":
        unprefixed = parsed.entity_key  # endpoint가 namespace 제거해 넘김
        with session() as s:
            row = s.execute(
                select(kg_entities.c.entity_id)
                .where(kg_entities.c.entity_key == unprefixed, kg_entities.c.scope == COMPANY_NS)
                .limit(1)
            ).first()
        log_params: dict = {"entity_key": unprefixed}
        if row is None:
            return None, "entity_or_node_not_found", log_params
        # Cypher 바인딩은 namespaced(Neo4j :Entity 노드 merge 키 = entity_node_key_for). $caller 미바인딩.
        return {"entity_key": entity_node_key_for(COMPANY_NS, unprefixed)}, None, log_params

    # expand_node — owner 라벨
    label = parsed.label
    node_id = parsed.id
    table, id_col = _EXPAND_PG_TABLE[label]
    col = getattr(table.c, id_col)
    log_params = {"label": label, "id": node_id}
    try:
        id_val = UUID(
            node_id
        )  # page_id/doc_id/row_id는 UUID. 비-UUID(placeholder slug/주입 문자열)면 404.
    except (ValueError, AttributeError):
        return None, "entity_or_node_not_found", log_params
    with session() as s:
        if owner_scope:
            scope_col, _owner_expr = owner_scope_columns(table)
            # documents는 owner_scope_columns가 SELECT용 CASE(label="owner_id")를 준다 →
            # read WHERE에는 raw user_id를 써야 한다(E1a visibility.py:53-58 계약). 그 외는 owner_id.
            raw_owner = documents.c.user_id if table is documents else table.c.owner_id
            where = [col == id_val, owner_inclusive_read_where(scope_col, raw_owner, user_id)]
            row = s.execute(select(scope_col).where(*where).limit(1)).first()
        else:
            where = [col == id_val, table.c.scope == COMPANY_NS]
            row = s.execute(select(table.c.scope).where(*where).limit(1)).first()
    if row is None:
        return None, "entity_or_node_not_found", log_params
    resolved_scope = row[0]
    bind: dict[str, Any] = {"id": node_id}
    if owner_scope:
        bind["caller"] = str(user_id) if user_id is not None else None
        if resolved_scope == "personal":
            log_params["id"] = personal_slug_log_value(node_id, user_id)  # L4 — 개인 id 해싱
    return bind, None, log_params


# --- 게이트 단계 1-6 (§6.2 의사코드) ------------------------------------------------


def _execute_and_map(
    template: Any,
    parsed: BaseModel,
    bind: dict[str, Any],
    log_params: dict | None,
    owner_scope: bool,
    limit: int | None,
    settings: Any,
    span: Any,
    user_id: UUID | None,
    *,
    raw_params: dict[str, Any],
) -> KgTemplateResult:
    """stage-4b(log_drop) + stage-5(가용성) + stage-6(실행/매핑/truncated/tripwire).
    slug 경로와 expand 경로가 공유(경계 균일성). 무동작 등가 — 기존 gate 회귀로 고정.

    log_params fallback(raw_params): slug company-fork는 log_params=None을 넘기고 헬퍼가 raw_params를
    fallback으로 본다(기존 stage-4b `dict(params)`와 정확 등가). expand 경로는 precheck가 항상
    log_params를 채우므로 fallback을 안 탄다."""

    def _stamp(result: KgTemplateResult) -> KgTemplateResult:
        result.log_params = log_params
        return result

    if template.log_drop_params:
        base = dict(log_params) if log_params is not None else dict(raw_params)
        for k in template.log_drop_params:
            base.pop(k, None)
        log_params = base

    if not kg_available():
        return _stamp(_rejected("kg_unavailable"))
    requested = limit if (limit is not None and limit > 0) else settings.kg_query_limit
    effective_limit = min(requested, settings.kg_query_limit)
    bind["limit"] = effective_limit
    for bind_key, settings_attr in template.setting_params:
        bind[bind_key] = getattr(settings, settings_attr)
    cypher = template.cypher_for(parsed, owner_scope)
    try:
        records = run_read(cypher, bind)
    except Exception as exc:  # noqa: BLE001 — driver 예외는 status로 정규화(fail-open)
        if is_timeout_error(exc):
            return _stamp(KgTemplateResult(status=KgQueryStatus.TIMEOUT, reject_reason="timeout"))
        return _stamp(
            KgTemplateResult(
                status=KgQueryStatus.ERROR, reject_reason=f"driver_error:{type(exc).__name__}"
            )
        )
    try:
        nodes, edges, dropped = _map_records(records, user_id)
    except Exception as exc:  # noqa: BLE001 — 매핑 예외는 driver와 구분해 정규화
        return _stamp(
            KgTemplateResult(
                status=KgQueryStatus.ERROR, reject_reason=f"mapping_error:{type(exc).__name__}"
            )
        )
    if any(dropped.values()):
        span.add_meta(**{k: v for k, v in dropped.items() if v})
    if dropped.get("boundary_tripwire"):
        _audit_boundary_violation(template.name, user_id, dropped["boundary_tripwire"])
    return _stamp(
        KgTemplateResult(
            status=KgQueryStatus.OK,
            nodes=nodes,
            edges=edges,
            truncated=len(records) == effective_limit,
            boundary_tripwire_drops=dropped.get("boundary_tripwire", 0),
        )
    )


def _gate(
    template_name: str,
    params: dict[str, Any],
    limit: int | None,
    settings: Any,
    span: Any,
    user_id: UUID | None,
) -> KgTemplateResult:
    # 1. flag — fail-closed
    if not kg_enabled():
        return _rejected("kg_disabled")
    # 2. registry lookup
    template = TEMPLATES.get(template_name)
    if template is None:
        return _rejected("unknown_template")
    # owner-scope 유효 flag는 요청당 **1회만** 읽고(코드리뷰 TOCTOU) 이하 모든 분기가 같은
    # 값을 쓴다 — 2b/2c code-guard, slug resolve/bind, cypher 변형 선택(cypher_for에 주입).
    # 게이트가 page_id를 바인딩했는데 cypher_for가 다른 flag로 v1(slug) 변형을 고르는
    # bind/cypher 불일치(unbound-param driver_error)와 모듈 간 분기 중복을 막는다.
    owner_scope = kg_owner_scope_enabled()
    # 2b. deferred-template code-guard (§7) — owner-variant 없는 `deferred` 템플릿을 owner-scope
    #     활성 시 소비자 경로로 서빙하면 false-green/미래 누수 벡터다 → fail-closed 차단.
    #     (K9 이전엔 entity_mentions가 deferred였으나 company_always로 재분류됐다 — company_always는
    #     이 가드에 안 걸리고 stage-4 company-fork로 안전하게 서빙된다. 현재 deferred 템플릿은 없으나
    #     미래 substrate-only 템플릿 대비로 가드는 유지한다.)
    if owner_scope and template.predicate_kind == "deferred":
        return _rejected("deferred_template")
    # 2c. company-framing code-guard (deferred의 거울상) — framing A(path_between_company)는
    #     owner-scope 전용 read 경로다. flag-OFF에서 부르면 cypher가 $page_id_a/$page_id_b를
    #     요구하지만 게이트 flag-OFF 분기는 slug만 바인딩해 unbound-param driver_error가 된다.
    #     dormant landmine을 깔끔한 fail-closed reject로 닫는다(코드리뷰).
    if not owner_scope and template.predicate_kind == "company":
        return _rejected("owner_scope_required")
    # 3. params validate (Literal 타입이 depth/hop 상한 하드코딩의 구현)
    try:
        parsed = template.params_model(**params)
    except ValidationError as exc:
        return _rejected(f"invalid_params:{_first_error_field(exc)}")

    # 3-E1b. expand 계열 (label,id) 선검증 분기 (D2) — 기존 slug_resolution 경로 무수정.
    # 별도 unknown-label 가드는 두지 않는다: ExpandNodeParams.label Literal이 stage-3에서
    # invalid_params:label로 막고(_EXPAND_PG_TABLE 키 == Literal 5종), Entity의 entity_key
    # 실재는 _expand_precheck가 확인한다.
    if template_name in _EXPAND_TEMPLATES:
        e_bind, reject_reason, e_log_params = _expand_precheck(
            template_name, parsed, owner_scope, user_id
        )
        if reject_reason is not None:
            r = _rejected(reject_reason)
            # 성공 경로(_execute_and_map stage-4b)와 동일하게 log_drop_params를 적용한다 —
            # 이 reject 경로는 _execute_and_map을 건너뛰므로 여기서 drop하지 않으면
            # expand_entity의 person-name entity_key가 kg_query_runs에 평문으로 남는다
            # (redact_pii는 한글 이름을 지우지 못해 person-entity redaction carve-out/K7.5
            # erasure 취지를 정면 위반한다 — erase된 노드 더블클릭 확장 not-found가 현실 트리거).
            if template.log_drop_params:
                e_log_params = {
                    k: v for k, v in e_log_params.items() if k not in template.log_drop_params
                }
            r.log_params = e_log_params
            return r
        return _execute_and_map(
            template,
            parsed,
            e_bind,
            e_log_params,
            owner_scope,
            limit,
            settings,
            span,
            user_id,
            raw_params=params,
        )

    # 4. slug resolve/선검증 — 존재하지 않는 시작점의 그래프 스캔 방지 + 404 판정 재료.
    #    flag-OFF: 기존 company-only 선검증(v1, slug 바인딩). flag-ON: owner-inclusive
    #    resolve_slug로 page_id 바인딩(B4 cross-owner slug 충돌 차단) + $caller 바인딩.
    bind: dict[str, Any] = {}
    log_params: dict | None = None

    def _stamp(result: KgTemplateResult) -> KgTemplateResult:
        # stage-4 이후 **모든** 결과(404 reject 포함)에 log_params를 실어 개인 제목이 실패
        # 경로 로그로 새지 않게 한다. 루프 안 404 early-return도 이걸 거쳐야 한다 — 앞 slug가
        # 이미 personal로 resolve돼 hash됐는데 뒷 slug가 404나면, stamp를 건너뛰면 raw params가
        # 기록돼 앞 slug의 개인 제목이 admin-readable 로그로 누출된다(L4 회귀, 코드리뷰).
        result.log_params = log_params
        return result

    if owner_scope and template.predicate_kind != "company_always":
        # company_always(K9 entity_neighbors/entity_mentions)는 owner-scope ON에서도 owner resolve
        # 루프를 타지 않는다(C-A stage-4 분기) — 데이터가 구조상 company-only라 owner-variant Cypher가
        # 없고, owner-fork가 page_id/$caller를 바인딩하면 company-only Cypher가 미참조해 unbound-param
        # driver_error가 된다. 항상 아래 flag-OFF company-fork(slug company 선검증 + $caller 미바인딩)로
        # 보낸다. cypher_for(parsed, owner_scope)도 company_always 함수가 owner_scope를 무시한다.
        log_params = dict(params)
        id_param_by_field = dict(template.owner_bind)
        # multi-slug 템플릿(path_between)이 slug마다 세션을 새로 열지 않도록 단일 세션을
        # 공유해 PG 왕복을 묶는다(코드리뷰 효율). slug_resolution이 비면(entity_mentions)
        # body가 안 돌아 세션은 즉시 닫힌다.
        with session() as resolve_s:
            for field, label in template.slug_resolution:
                slug_val = getattr(parsed, field)
                resolved = resolve_slug(slug_val, user_id, label, s=resolve_s)
                if resolved is None:
                    # B5 — foreign≡absent 동일 404. reject_reason은 caller 입력 그대로(404는
                    # 매칭된 stored note 없음 → 제목 누수 아님, foreign/absent 동일 문자열). 단
                    # **로그**는 _stamp가 실어 둔 log_params(앞서 resolve된 personal slug는
                    # hash)를 쓴다 — reject_reason(raw caller 입력)과 로그(hash)는 별개 표면이다.
                    return _stamp(_rejected(f"slug_not_found:{slug_val}"))
                # Neo4j는 page_id/owner_id를 문자열로 저장하고(projection `str(...)`), driver는
                # UUID 객체를 거부한다 — str로 바인딩해 stored 문자열과 매칭.
                bind[id_param_by_field[field]] = str(resolved.page_id)
                # L4 — 개인-scope resolve된 slug 값은 로그에 hash(개인 제목 admin-노출 차단).
                # caller_id를 salt로 섞어 owner 간 동일-제목 상관 oracle을 막는다(코드리뷰).
                if resolved.scope == "personal" and field in log_params:
                    log_params[field] = personal_slug_log_value(slug_val, user_id)
        # owner predicate $caller — 세션 전용(client 입력 금지). None이면 Cypher null로
        # collapse → company-only(B-null-caller). str로 바인딩(stored owner_id 문자열 매칭).
        bind["caller"] = str(user_id) if user_id is not None else None
    else:
        missing = _first_missing_company_slug(
            [getattr(parsed, field) for field, _label in template.slug_resolution]
        )
        if missing is not None:
            return _rejected(f"slug_not_found:{missing}")
        bind = {field: getattr(parsed, field) for field in template.bind_params}

    # 4b(log_drop) + 5(가용성) + 6(실행/매핑/truncated/tripwire)는 공유 헬퍼로 위임한다 —
    # slug 경로와 E1b expand 경로가 정확히 같은 실행/매핑/경계 규율을 공유(무동작 등가, 기존
    # gate 회귀로 고정). company-fork는 log_params=None을 넘기고 헬퍼가 raw_params(params)를
    # fallback으로 봐 기존 stage-4b `dict(params)`와 정확 등가다. `_stamp` 클로저는 헬퍼가
    # 재현하므로 여기선 더 참조하지 않는다.
    return _execute_and_map(
        template,
        parsed,
        bind,
        log_params,
        owner_scope,
        limit,
        settings,
        span,
        user_id,
        raw_params=params,
    )


def _audit_boundary_violation(template_name: str, caller: UUID | None, count: int) -> None:
    """B1 wire-layer 누수 감지 시 독립 audit 행 — must-be-zero 경보. 절대 foreign
    owner_id/slug/title을 남기지 않는다(observing caller-scope 마커만, §1.2)."""
    try:
        with audit("kg.boundary_violation") as bspan:
            bspan.add_meta(
                template_name=template_name,
                dropped_count=count,
                caller_present=caller is not None,
            )
    except Exception:  # noqa: BLE001 — 경보 실패가 결과 반환을 막지 않는다(fail-open)
        pass


def _rejected(reason: str) -> KgTemplateResult:
    return KgTemplateResult(status=KgQueryStatus.REJECTED, reject_reason=reason)


def _first_error_field(exc: ValidationError) -> str:
    err = exc.errors()[0]
    if err["loc"]:
        return str(err["loc"][-1])
    # model-level validator (예: identical_slugs)는 loc이 비어 msg로 식별한다.
    return str(err["msg"]).removeprefix("Value error, ")


def _first_missing_company_slug(slugs: list[str]) -> str | None:
    with session() as s:
        for slug in slugs:
            row = s.execute(
                select(wiki_pages.c.page_id)
                .where(wiki_pages.c.slug == slug, wiki_pages.c.scope == "company")
                .limit(1)
            ).first()
            if row is None:
                return slug
    return None


# --- 결과 매핑 (공통 매퍼 — §6.1 규약 4종) -------------------------------------------

# id 추출 우선순위 — 라벨 무관 고정(§6.1). K6 라벨 추가 시에도 매퍼 수정 불요.
_ID_PROPS = ("page_id", "doc_id", "row_id", "entity_key", "name", "slug")


def kg_node_dedup_key(label: str, scope: str, node_id: str) -> tuple[str, str, str]:
    """노드 신원 키 — `(label, scope, id)`. `_map_records`(게이트 매핑)와
    `wiki._merge_graph`(패널 union)가 **이 단일 함수**를 공유해 두 곳의 dedup 규약이
    드리프트하지 않게 한다(코드리뷰 #5). scope를 신원에 넣어 같은 slug placeholder의
    company/personal 노드를 둘 다 보존한다."""
    return (label, scope, node_id)


def kg_edge_dedup_key(src: str, rel: str, dst: str, scope: str) -> tuple[str, str, str, str]:
    """엣지 신원 키 — `(src, rel, dst, scope)`. `_map_records`/`_merge_graph` 공유(드리프트
    방지). `status`/`conflict_reason` 등 properties는 **신원이 아니다** — projection MERGE가
    노드쌍·rel·scope당 1 엣지를 보장하므로 properties는 그 1 엣지의 속성이다(코드리뷰 #4)."""
    return (src, rel, dst, scope)


def _map_records(
    records: list[Any], caller: UUID | None
) -> tuple[list[KgGraphNode], list[KgGraphEdge], dict]:
    nodes_by_eid: dict[str, tuple[str, dict[str, Any]]] = {}  # element_id → (label, props)
    rels: list[Any] = []
    # boundary_tripwire: predicate가 옳다면 0이어야 하는 wire-layer must-be-zero(§1.2 B1).
    dropped = {"no_id_nodes": 0, "unmapped_edges": 0, "expired_facts": 0, "boundary_tripwire": 0}
    for record in records:
        for value in record.values():
            _flatten(value, nodes_by_eid, rels)

    now = datetime.now(UTC)
    caller_s = str(caller) if caller is not None else None
    excluded_eids: set[str] = set()
    out_nodes: list[KgGraphNode] = []
    seen_keys: set[tuple[str, str, str]] = set()  # (label, scope, id) — scope-aware dedup
    eid_to_id: dict[str, str] = {}
    for eid, (label, props) in nodes_by_eid.items():
        # 규약 2 — TTL 필터: 만료 StructuredFact는 노드/incident 엣지 모두 제외.
        if label == Label.STRUCTURED_FACT.value and _expired(props.get("valid_until"), now):
            excluded_eids.add(eid)
            dropped["expired_facts"] += 1
            continue
        node_id = _node_id(props)
        if node_id is None:
            # 규약 4 — 방어적 skip: id 후보 속성이 전무한 노드(구조상 비발생).
            excluded_eids.add(eid)
            dropped["no_id_nodes"] += 1
            continue
        scope, is_own = _scope_of(props, caller_s)
        if scope is None:
            # malformed: owner_id 있는데 scope 없음 → fail-closed drop + tripwire.
            excluded_eids.add(eid)
            dropped["boundary_tripwire"] += 1
            continue
        # B1 wire tripwire (defense-in-depth): predicate가 막았어야 할 foreign personal
        # 노드가 새어 왔으면 drop + 경보. owner-variant predicate가 옳으면 절대 미발화.
        if scope == "personal" and not is_own:
            excluded_eids.add(eid)
            dropped["boundary_tripwire"] += 1
            continue
        eid_to_id[eid] = node_id
        key = kg_node_dedup_key(label, scope, node_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out_nodes.append(
            KgGraphNode(
                id=node_id,
                label=label,
                slug=props.get("slug"),
                # K9 — Entity 노드는 title prop이 없고 display_name이 사람이 읽는 이름이다 →
                # title로 매핑(graphChipText/readout이 그대로 쓴다). 다른 라벨은 title 그대로.
                title=props.get("title")
                or (props.get("display_name") if label == Label.ENTITY.value else None),
                # 규약 3 — placeholder 포함: materialized=false + id=slug.
                materialized=bool(props.get("materialized", True)),
                scope=scope,
                is_own_personal=is_own,
                # K9 — Entity 노드의 page-only degree/종류만 wire로(고정 화이트리스트 필드). 다른
                # 라벨은 prop이 없어 None. int 강제(driver Integer → python int).
                mention_count=(
                    int(props["mention_count"])
                    if label == Label.ENTITY.value and props.get("mention_count") is not None
                    else None
                ),
                entity_kind=(props.get("entity_kind") if label == Label.ENTITY.value else None),
            )
        )

    out_edges: list[KgGraphEdge] = []
    # (src, rel, dst, scope) — scope-aware dedup(노드 dedup `(label, scope, id)`와 대칭, 코드리뷰).
    # 같은 slug placeholder가 company/personal 양쪽으로 살아남으면(노드는 scope로 분리 보존)
    # 동일 endpoint-id·rel의 company 엣지와 personal 엣지가 scope 없는 키로는 충돌해 한쪽이
    # 사라지고 그 scope 구분이 손실된다 — scope를 키에 넣어 둘 다 보존한다.
    seen_edges: set[tuple[str, str, str, str]] = set()
    for rel in rels:
        src_eid = rel.start_node.element_id
        dst_eid = rel.end_node.element_id
        # drop된 노드(만료/no-id/boundary)에 인접한 엣지는 모두 제외.
        if src_eid in excluded_eids or dst_eid in excluded_eids:
            continue
        src = eid_to_id.get(src_eid)
        dst = eid_to_id.get(dst_eid)
        if src is None or dst is None:
            dropped["unmapped_edges"] += 1
            continue
        rel_props = dict(rel)
        e_scope, e_own = _scope_of(rel_props, caller_s)
        # 독립 엣지 tripwire(노드와 대칭): malformed(owner_id 있는데 scope 없음 → None)이거나
        # scope=='personal'인데 내 것이 아니면(owner_id 불일치/부재) drop. predicate가 옳으면
        # 둘 다 미발화(must-be-zero).
        if e_scope is None or (e_scope == "personal" and not e_own):
            dropped["boundary_tripwire"] += 1
            continue
        key = kg_edge_dedup_key(src, rel.type, dst, e_scope)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        # owner_id/scope는 typed 필드(scope)와 tripwire에만 쓰고 wire properties에선 제거.
        public_props = {
            k: _native_prop(v) for k, v in rel_props.items() if k not in ("owner_id", "scope")
        }
        out_edges.append(
            KgGraphEdge(src=src, dst=dst, rel=rel.type, scope=e_scope, properties=public_props)
        )
    return out_nodes, out_edges, dropped


def _scope_of(props: dict[str, Any], caller_s: str | None) -> tuple[str | None, bool]:
    """노드/엣지 props에서 (scope, is_own_personal)을 서버 파생 — **노드·엣지 단일 출처**
    (코드리뷰: 노드/엣지 tripwire가 한 글자도 달라지면 안 된다. 이전엔 `_node_scope`/`_edge_scope`
    복제 본문이었고 한쪽만 고치면 tripwire 비대칭이 재발했다). owner_id는 절대 wire로 안 나간다.

    - scope 없음 + owner_id 없음 → 'company'(방어 기본값, 구 v1 노드·엣지/placeholder 호환;
      company 엣지는 projection이 owner_id=None이라 속성이 사라진다).
    - scope 없음 + owner_id 있음 → (None, False) = malformed → 호출자가 drop+tripwire.
    - is_own_personal = scope=='personal' AND owner_id==caller(문자열 비교). owner_id 없는
      personal은 is_own=False라 호출자가 drop(B3 — un-owned personal은 신뢰 불가)."""
    scope = props.get("scope")
    owner_id = props.get("owner_id")
    if scope is None:
        return (None if owner_id is not None else "company"), False
    is_own = scope == "personal" and owner_id is not None and str(owner_id) == caller_s
    return scope, is_own


def _flatten(
    value: Any, nodes_by_eid: dict[str, tuple[str, dict[str, Any]]], rels: list[Any]
) -> None:
    """driver record 값을 재귀 평탄화 — duck-typing 식별 순서가 계약이다.

    Node만 `labels`를, Path만 `relationships`를 갖는다(Relationship은
    `start_node`만). v1 템플릿의 RETURN에 스칼라 집계는 없다 — 그 외 타입은
    무시한다(추가하려면 §6.1 규약을 함께 개정)."""
    if value is None:
        return
    if hasattr(value, "labels"):  # Node
        label = next(iter(value.labels), "")
        nodes_by_eid[value.element_id] = (label, dict(value))
    elif hasattr(value, "relationships"):  # Path — 전체 노드가 속성 포함 hydrate
        for node in value.nodes:
            _flatten(node, nodes_by_eid, rels)
        rels.extend(value.relationships)
    elif hasattr(value, "start_node"):  # Relationship
        rels.append(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _flatten(item, nodes_by_eid, rels)


def _node_id(props: dict[str, Any]) -> str | None:
    for key in _ID_PROPS:
        value = props.get(key)
        if value:
            return str(value)
    return None


def _native_prop(value: Any) -> Any:
    """규약 1 — temporal 변환: driver 고유 타입을 ISO 8601 문자열로.

    Pydantic v2 기본 직렬화에 맡기지 않는다 — driver 타입의 명시 변환은 이
    매퍼가 단일 책임 지점이다(§6.1)."""
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _expired(valid_until: Any, now: datetime) -> bool:
    if valid_until is None:
        return False
    if hasattr(valid_until, "to_native"):
        valid_until = valid_until.to_native()
    if not isinstance(valid_until, datetime):
        return False
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=UTC)
    return valid_until < now


# --- 기록 (7단계) + last_accessed_at (8단계) ----------------------------------------


def _record_run_best_effort(
    *,
    template_name: str,
    params: dict[str, Any],
    result: KgTemplateResult,
    user_id: UUID | None,
    correlation_id: UUID,
    span: Any,
) -> None:
    """run 기록은 best-effort다 — 기록 실패가 이미 산출된 graph 결과 반환을
    막지 않는다(fail-open, 버그 리뷰 #9). DB가 정말 죽으면 audit exit write도
    실패해 최상위 fail-open으로 떨어지지만, 그 전까지 결과는 보존된다."""
    try:
        _record_run(
            template_name=template_name,
            params=params,
            result=result,
            user_id=user_id,
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001 — 기록 실패는 관측 누락일 뿐
        span.add_meta(record_run_failed=type(exc).__name__)


def _record_run(
    *,
    template_name: str,
    params: dict[str, Any],
    result: KgTemplateResult,
    user_id: UUID | None,
    correlation_id: UUID,
) -> None:
    reason = result.reject_reason
    with session() as s:
        s.execute(
            insert(kg_query_runs).values(
                run_id=uuid4(),
                template_name=template_name,
                # 파라미터는 사용자 입력일 수 있다 — redaction 통과 후 저장(절대 규칙).
                params_redacted=redact_pii(params),
                status=result.status.value,
                reject_reason=redact_pii_text(reason) if reason is not None else None,
                duration_ms=result.duration_ms,
                result_count=len(result.nodes) if result.status is KgQueryStatus.OK else None,
                user_id=user_id,
                correlation_id=correlation_id,
            )
        )
        s.commit()


def _touch_last_accessed_best_effort(nodes: list[KgGraphNode], span: Any) -> None:
    """8단계 — 응답에 포함된 WikiPage/WikiClaim 실체 노드의 관측 속성 SET.

    실패해도 질의 결과에 영향 없다(관측 누락만 발생 — kg-model §2 각주)."""
    page_ids = [
        n.id
        for n in nodes
        if n.label in (Label.WIKI_PAGE.value, Label.WIKI_CLAIM.value) and n.materialized
    ]
    try:
        store.touch_last_accessed(page_ids)
    except Exception as exc:  # noqa: BLE001 — best-effort 계약
        span.add_meta(last_accessed_failed=type(exc).__name__)
