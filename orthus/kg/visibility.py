"""K7 — owner-scope visibility helpers (single source, kg-k7-plan §1.1).

이 모듈은 owner 경계 계약의 단일 출처다. K7.1은 **projection-side(write)** 헬퍼를
싣는다: 키 정체성(`ns_for`/`ns_key`/`entity_node_key`)과 경계 술어
(`owner_inclusive_sql` = SELECT 입력 술어, `doc_visible_to` = provenance 네임스페이스
가드). K7.2가 같은 모듈에 **read-side** 헬퍼(`visibility_predicate`/`resolve_slug`)를
더한다 — 경계 계약(B1–B6)이 한 곳에 모이고 미래 sharing이 가법적 확장이 된다.

write 술어(`owner_inclusive_sql`: `owner IS NOT NULL`)와 read 술어(K7.2:
`owner = $caller`)는 의미가 다르다 — 같은 모듈에 두어 한쪽만 바뀌고 다른 쪽이
방치되는 drift를 한눈에 막는다(코드리뷰 #10).

유효 owner-scope 게이트는 `orthus.kg.client.kg_owner_scope_enabled()`(D3 hard-guard)
이며, projection-side 헬퍼는 **순수**다(flag 미독). read-side는 `visibility_predicate`
(순수 — Cypher fragment 문자열만 반환)와 `resolve_slug`(PG read — start node 정체성
resolve)다. flag 분기는 호출자(projection SELECT / gate template 선택)가 한다.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Table, case, or_, select

from orthus.db import session
from orthus.tables import documents, structured_rows, wiki_pages

# company row의 네임스페이스 토큰. personal row는 owner UUID 문자열을 쓴다.
COMPANY_NS = "company"

# read resolve가 label→PG `wiki_pages.kind`로 디스패치하는 매핑(단일 테이블, kind 컬럼).
# Document/StructuredFact는 별도 테이블이고 현재 read 템플릿이 그 라벨을 start node로
# resolve하지 않으므로 미포함(미래 템플릿이 추가될 때 확장 — kg-k7.2-plan §3 Step 1).
_LABEL_TO_KIND: dict[str, str] = {"WikiPage": "page", "WikiClaim": "claim", "WikiSource": "source"}


def owner_scope_columns(table: Table) -> tuple[Any, Any]:
    """테이블별 (scope_col, owner_col_expr)의 단일 진실 원천(D1, kg-model §3).

    KG projection의 owner 경계는 테이블마다 owner를 다른 컬럼에서 읽는다:
      - wiki_pages / structured_rows: 전용 `owner_id`(nullable).
      - documents: `owner_id` 컬럼이 없다 → owner = user_id, 단 user_id가 NOT NULL이라
        company 문서도 채워져 'company ⇒ owner IS NULL' 불변이 깨진다 →
        scope='personal'일 때만 user_id, company면 NULL로 CASE(.label("owner_id")).

    이 매핑을 여기 한 곳에 모아 projection SELECT 컬럼 / (E1b) expand precheck가 소비한다.
    미등록 테이블은 fail-closed KeyError(조용한 None/company-only 수렴 금지 — 미래 소비자가
    경계를 놓치지 않게).

    **SELECT vs WHERE 이중성(중요):** documents가 반환하는 owner_col_expr는 **SELECT 컬럼용
    CASE(label)** 이다. projection WHERE 술어는 그대로 raw `documents.c.user_id`를
    owner_inclusive_sql에 넘겨야 flag-off 스냅샷과 byte-identical하다(CASE를 WHERE에 넣으면
    `IS NOT NULL` 술어 형태가 달라진다). E1b read precheck는 WHERE에
    owner_inclusive_read_where(scope, raw user_id/owner_id, caller)를 쓴다 — write 술어
    owner_inclusive_sql(`owner IS NOT NULL`)을 read에 재사용하지 말 것(전 owner personal 노출).

    이 헬퍼는 flag-off(COMPANY) 경로에는 개입하지 않는다 — 호출자가 COMPANY 분기에서
    owner 컬럼을 아예 만들지 않는 현행 구조를 그대로 둔다(byte-identity 보존).
    """
    if table is wiki_pages:
        return wiki_pages.c.scope, wiki_pages.c.owner_id
    if table is structured_rows:
        return structured_rows.c.scope, structured_rows.c.owner_id
    if table is documents:
        owner_expr = case((documents.c.scope == "personal", documents.c.user_id), else_=None).label(
            "owner_id"
        )
        return documents.c.scope, owner_expr
    raise KeyError(f"owner_scope_columns: unmapped table {getattr(table, 'name', table)!r}")


def owner_inclusive_sql(scope_col: Any, owner_col: Any) -> Any:
    """`company OR (personal AND owner IS NOT NULL)` — owner-inclusive SELECT WHERE 단편.

    projection 입력(write 경계) 술어의 **단일 출처**다. 여섯 `_*_select` 헬퍼가 전부
    이걸 거치므로 owner-inclusion 규칙이 한 곳에서만 바뀐다(코드리뷰 #10).

    owner 부재 personal row는 제외한다(방어): owner_id NULL인 personal row가
    `ns_for`에서 company로 수렴해 placeholder/노드 경계를 흐리는 것을 SELECT 단계에서
    먼저 막는다. company row는 owner 컬럼과 무관하게 전부 포함.

    주의: read 경계(K7.2)는 `owner = $caller`라 의미가 다르다 — 이 함수를 read에
    재사용하면 안 된다(여기 두는 이유는 두 술어를 한 모듈에서 대조하기 위함). read 경계는
    `owner_inclusive_read_where`를 쓴다(아래)."""
    return (scope_col == COMPANY_NS) | ((scope_col == "personal") & (owner_col.isnot(None)))


def owner_inclusive_read_where(scope_col: Any, owner_col: Any, caller_id: UUID | None) -> Any:
    """`company OR (personal AND owner == $caller)` — owner-inclusive **read** WHERE 단편(K7.4).

    read 경계 술어의 **단일 출처**다. `resolve_slug`(이 모듈)와 graph.py candidate 생성이
    모두 이걸 거치므로 owner-read 규칙이 한 곳에서만 바뀐다 — 미래 P7.3/sharing이 술어 하나
    (예: shared-collaborator OR)만 확장하면 전 경로가 상속한다('six encodings' drift 차단).

    caller==None(B-null-caller)은 company-only로 축약한다 — SQLAlchemy `owner == None`은
    `IS NULL`로 컴파일돼 owner_id NULL인 personal row를 잘못 매칭하므로, None 경로는 owner
    비교를 **아예 만들지 않고** company만 본다(Cypher `owner_id = $caller` null 바인딩이
    company-only로 축약되는 것과 동치).

    write 술어 `owner_inclusive_sql`(위, `owner IS NOT NULL`)을 **재사용하지 않는다** — 의미가
    다르고(write=전 personal, read=caller 본인만), byte-identical CI 가드는 Cypher fragment만
    감시하므로 router-side SQL이 write 술어로 오염되면 전 owner의 personal row가 candidate에
    들어와 graph.py title-disclosure 표면이 된다(§3 하드닝)."""
    if caller_id is None:
        return scope_col == COMPANY_NS
    return or_(scope_col == COMPANY_NS, owner_col == caller_id)


def doc_visible_to(
    src_scope: str, src_owner: UUID | None, d_scope: str, d_owner: UUID | None
) -> bool:
    """provenance(EXTRACTED_FROM)용 owner-namespace 가드(B4) — slug `resolve_dst`의
    doc_id 판이다. source/fact는 자기 네임스페이스(company ∪ 자기 owner) 안의 doc에만
    연결할 수 있다. `doc_meta`는 전 owner 평면 맵이라(전 personal doc 포함), 이 가드가
    없으면 personal-A source가 personal-B doc로 cross-owner 엣지를 만들 수 있다
    (`_edge_scope_owner`의 '둘 다 personal이면 같은 owner' 전제가 깨짐). company doc은
    누구에게나 visible. `resolve_dst`(slug)와 이 함수(doc_id)가 같은 B4 규칙을 공유하므로
    미래 sharing 변경 시 둘이 같은 모듈에서 함께 보인다(코드리뷰 #10)."""
    if d_scope == COMPANY_NS:
        return True
    return d_owner is not None and d_owner == src_owner


def ns_for(scope: str, owner_id: UUID | str | None) -> str:
    """row의 네임스페이스 토큰 — placeholder 정체성과 entity 접두의 단일 출처.

    company(또는 owner 부재) → `'company'`, personal → owner UUID 문자열.
    서로 다른 owner의 같은 slug가 한 노드로 합쳐지지 않게 하는 키 분리의 기반(B4).
    """
    if scope == COMPANY_NS or owner_id is None:
        return COMPANY_NS
    return str(owner_id)


def ns_key(scope: str, owner_id: UUID | str | None, slug: str) -> str:
    """placeholder :WikiPage의 MERGE 정체성 — `'<namespace>:<slug>'`.

    v1은 bare slug를 MERGE 키로 썼다. v2(K7.1)는 이 ns_key로 재키잉한다 — 같은
    slug의 company/personal-A/personal-B dangling placeholder가 **서로 다른**
    노드가 된다(B4 cross-owner 존재 오라클 차단). v1 slug-only placeholder는 v2
    rebuild가 purge한다(store.prune, page_id NULL AND ns_slug NULL).
    """
    return f"{ns_for(scope, owner_id)}:{slug}"


def entity_node_key_for(scope: str, entity_key: str) -> str:
    """PG `kg_entities.entity_key`(`'kind:name_norm'`)에 scope 접두를 붙인 :Entity의
    Neo4j MERGE 키 — `'<scope-prefix>:kind:name_norm'`. **단일 변환 지점**이다:
    projection 노드/엣지 끝점과 `store.delete_entity_nodes`가 모두 이걸 거쳐야
    erasure가 접두 노드를 놓치지 않는다.

    K7은 접두 SLOT만 예약한다 — D2 아래 entity는 company-only라 값이 항상
    `'company:...'`이고 **merge/정체성 의미는 0**이다(cross-owner entity dedup 금지,
    personal-entity 슬라이스 ADR로 미룸). PG 컬럼은 `kind:name_norm`로 유지된다
    (projection-only 접두 — PG 데이터 마이그레이션 없음)."""
    prefix = ns_for(scope, None) if scope == COMPANY_NS else scope
    return f"{prefix}:{entity_key}"


def entity_node_key(scope: str, kind: str, name_norm: str) -> str:
    """(kind, name_norm)에서 직접 만드는 변형 — entities.py PG 키와 동형의 suffix
    `'kind:name_norm'`를 `entity_node_key_for`로 접두한다(단일 빌더 경유)."""
    return entity_node_key_for(scope, f"{kind}:{name_norm}")


def entity_key_from_node_id(node_id: str) -> str:
    """namespaced :Entity node id(company:kind:name_norm) → unprefixed kg_entities.entity_key.
    company_always라 접두는 항상 'company:'. entity_node_key_for의 역변환(단일 출처). personal
    entity 미지원이라 다른 접두는 없다(K7 접두 SLOT은 값이 항상 company:)."""
    return node_id.removeprefix(f"{COMPANY_NS}:")


# --- read-side (K7.2) — owner predicate + start-node resolve --------------------


def visibility_predicate(var: str, caller_param: str = "caller") -> str:
    """owner-variant 템플릿이 bound 노드/엣지에 합성하는 **단일** Cypher WHERE fragment.

    `(<var>.scope = 'company' OR <var>.owner_id = $<caller>)` — `(var, caller)`만 받고
    **role 인자는 절대 받지 않는다**(B-admin: admin/role은 추가 가시성 0). 값은 driver
    바인딩($caller)으로만 전달 — f-string은 식별자(var/param 이름)만 보간한다.

    sharing 확장점(미래): 술어는 owner-only binary의 일반형
    `... OR <var>.id IN $visible_ids`의 owner case다. 다음 hard-prerequisite 문장은
    docs-check/grep가 고정한다 — slot을 켜려면 proof harness가 먼저 와야 한다:
    Binding `$visible_ids` to a non-empty set requires the dynamic-`$visible_ids` proof harness to land first; until then `$visible_ids` is never bound and `shared-collaborator` grants no visibility.
    """  # noqa: E501
    return f"({var}.scope = 'company' OR {var}.owner_id = ${caller_param})"


class ResolvedSlug(BaseModel):
    """`resolve_slug` 결과 — page_id(시작노드 바인딩)와 scope(L4 로깅 결정)."""

    page_id: UUID
    scope: Literal["company", "personal"]


def resolve_slug(
    slug: str, caller_id: UUID | None, label: str = "WikiPage", *, s: Any = None
) -> ResolvedSlug | None:
    """read-path slug→(page_id, scope) resolver (L2 — PG, fail-open: Neo4j 비의존).

    owner-inclusive predicate로 PG `wiki_pages`를 단일 쿼리 조회한다. caller-personal이
    있으면 그것을, 없으면 company를, 둘 다 없으면 `None`(→404, B5)을 돌려준다.
    foreign-only와 truly-absent는 **같은 쿼리**로 `None`이 되어 구분 불가(B5 비-오라클).

    `s`(선택): 열린 세션을 넘기면 그 안에서 실행한다 — multi-slug 템플릿(path_between)이
    slug마다 세션을 새로 열어 PG 왕복을 중복하지 않게, 호출자가 단일 세션을 공유한다
    (코드리뷰 효율). None이면 자체 세션을 연다(단일-slug 호출/테스트 편의).

    null caller(B-null-caller): caller가 None이면 술어를 company-only로 축약한다.
    SQLAlchemy `col == None`은 `IS NULL`로 컴파일되어 owner_id NULL인 personal row를
    잘못 매칭하므로, None 경로는 owner 비교를 **아예 만들지 않고** company만 본다
    (= Cypher `owner_id = $caller`에 null 바인딩이 company-only로 축약되는 것과 동치).

    LIMIT 1 결정론 근거: `uq_wiki_pages_slug_scope_owner`(slug,scope,owner_id, NULLS NOT
    DISTINCT)가 slug당 company ≤1 + owner당 personal ≤1을 보장. kind 필터는 tiebreak가
    아니라 정합성 단언(claim slug가 label=WikiPage로 resolve되지 않게). 미지원 label
    (Document/StructuredFact 등)은 None.
    """
    kind = _LABEL_TO_KIND.get(label)
    if kind is None:
        return None
    # owner-inclusive read 술어는 단일 공유 헬퍼에서(owner_inclusive_read_where) — graph.py
    # candidate 생성과 같은 술어를 쓴다(SoT, 코드리뷰 #10/§3). caller==None은 헬퍼가 company-only로
    # 축약한다.
    where = [
        wiki_pages.c.slug == slug,
        wiki_pages.c.kind == kind,
        owner_inclusive_read_where(wiki_pages.c.scope, wiki_pages.c.owner_id, caller_id),
    ]
    if caller_id is None:
        stmt = select(wiki_pages.c.page_id, wiki_pages.c.scope).where(*where).limit(1)
    else:
        stmt = (
            select(wiki_pages.c.page_id, wiki_pages.c.scope)
            .where(*where)
            # owner-own row 우선(personal-first), 나머지는 company. uq가 ≤2행 보장. WHERE가
            # personal row를 caller-own으로 한정하므로 scope='personal'을 앞세우면 곧 자기 것
            # 우선이다. CASE로 명시 — `(owner_id==caller).desc()`는 company row의 NULL 비교가
            # Postgres DESC 기본 NULLS FIRST로 personal보다 앞서 company를 잘못 골랐다(코드리뷰).
            .order_by(case((wiki_pages.c.scope == "personal", 0), else_=1))
            .limit(1)
        )
    if s is not None:
        row = s.execute(stmt).first()
    else:
        with session() as own_s:
            row = own_s.execute(stmt).first()
    if row is None:
        return None
    return ResolvedSlug(page_id=row.page_id, scope=row.scope)


def caller_has_personal_pages(caller_id: UUID | None, *, s: Any = None) -> bool:
    """caller가 자기 소유 personal `WikiPage`를 1개라도 갖는지(K7.4 short-circuit 신호).

    False면 owner-variant path가 company-only path와 구조적으로 같다(caller에게 personal
    hop이 존재할 수 없음) → `run_path_framings`가 framing A만 1회 실행하고 framing B를 합성한다
    (지배적 company caller가 2배 왕복/2배 kg_query_runs row를 내지 않게). True면 dual-path로
    framing B를 실제 owner-variant로 실행한다.

    index-backed LIMIT-1(uq `(slug,scope,owner_id)` 상의 `scope='personal' AND
    owner_id=$caller`) — 답변당 1회만 계산해 재사용한다(framing/anchor마다 재실행 금지, §8
    probe-cost). caller None(B-null-caller)은 항상 False — company에는 personal page가 없다.

    **fingerprint 주의:** 이 신호는 missing_link 경로(gap.py)도 같은 헬퍼로 독립 호출해
    노트-0 caller의 owner branch를 건너뛴다 — 그 경로의 byte-identity(노트-0 caller가 company-only
    경로와 동일 비용)는 이 LIMIT-1 probe를 **포함**한 비용으로 측정한다(§4/§8)."""
    if caller_id is None:
        return False
    stmt = (
        select(wiki_pages.c.page_id)
        .where(wiki_pages.c.scope == "personal", wiki_pages.c.owner_id == caller_id)
        .limit(1)
    )
    if s is not None:
        return s.execute(stmt).first() is not None
    with session() as own_s:
        return own_s.execute(stmt).first() is not None


def personal_slug_log_value(slug: str, caller_id: UUID | str) -> str:
    """L4(2026-06-16) — 개인-scope로 resolve된 slug를 `kg_query_runs`에 raw 제목이 아니라
    안정적 hash로 기록한다(개인 메모 제목이 admin-readable 로그에 raw로 남는 창 차단).
    company-scope slug는 raw 유지(진단성), 404는 caller 입력 그대로(B5 등가).

    hash 입력에 **caller_id를 salt로 섞는다**(코드리뷰): salt가 없으면 sha256(slug)가
    owner-무관 결정론이라, 서로 다른 owner가 같은 제목의 personal 노트를 질의하면 동일 hash가
    남아 admin이 cross-owner 제목 일치를 상관추론할 수 있다(평문은 가렸지만 동일성 oracle).
    caller별로 salt하면 같은 owner의 같은 slug는 여전히 안정 hash(진단 상관 보존)지만 owner 간
    동일성은 관측 불가다."""
    digest = hashlib.sha256(f"{caller_id}:{slug}".encode()).hexdigest()[:12]
    return "personal:" + digest
