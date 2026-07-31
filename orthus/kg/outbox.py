"""K3 — transactional outbox: enqueue helper + `KGOutboxWorker` + drain/trim CLI.

wiki consolidate / document publish / promote approve의 PG commit과 **같은
트랜잭션**에서 `kg_outbox`에 이벤트를 적재하고(§5.2 — 호출 지점 3곳은
`wiki/store.py::_persist`, `documents.py::publish_agent_draft_document`,
`documents.py::upsert_source_document`), worker가 단일 Cypher 트랜잭션으로
멱등 적용한다(`:OutboxApplied` 마커 — kg-model §3).

크로스-DB 원자성은 두지 않는다 — Postgres 권위, Neo4j eventual convergence.
불일치는 언제든 `kg-rebuild`가 수렴시킨다. Neo4j 미가용은 이벤트 실패가
아니다(attempts 미증가 — claim 해제 후 재기동 시 drain, §2.6).

이 모듈의 top-level import는 `orthus.kg.project`를 포함하지 않는다 —
`wiki/store.py`가 enqueue hook으로 이 모듈을 import하는데, project.py가
`orthus.wiki.store`를 import하므로(frontmatter 로더) 순환이 생긴다. 재투영
경로(`_apply_change`)에서만 lazy import한다.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from orthus.audit.logger import audit, current_correlation_id
from orthus.db import session
from orthus.kg import store as kg_store
from orthus.kg.client import (
    KgDisabled,
    KgUnavailable,
    get_kg_driver,
    kg_enabled,
    kg_owner_scope_enabled,
    kg_write_session,
    require_company_node,
)
from orthus.kg.schema import KG_SCHEMA_VERSION
from orthus.settings import get_settings
from orthus.tables import kg_outbox

if TYPE_CHECKING:
    import neo4j

# §5.1 CHECK와 1:1 (kg-model §3 표).
KgOutboxEntityKind = Literal["wiki_page", "document", "structured_row"]

MAX_ATTEMPTS = 5  # kg-model §3 4항 — 5회 실패 → dead
LEASE_SECONDS = 60
BATCH_SIZE = 20
# applied row(PG) + :OutboxApplied 마커(Neo4j) 보존 기간(§5.5/§4.6). 둘을 같은
# 기준으로 지워 replay 면이 없다 — 멱등 마커의 권위는 Neo4j이고, PG row 삭제는
# 재적용을 유발하지 않는다. `dead`는 운영자 처리 전까지 보존.
APPLIED_RETENTION_DAYS = 30
TRIM_INTERVAL_SECONDS = 3600  # worker 내 best-effort trim 주기 (2026-06-12 사용자 결정)

# delete 이벤트의 라벨 무관 키 매치 — 메타 라벨(:KgMeta/:OutboxApplied)은 이
# 키가 없어 안전하다. 키는 고정 dict에서만 — 문자열 보간 금지(파라미터는 바인딩).
_DELETE_KEYS: dict[str, str] = {
    "wiki_page": "page_id",
    "document": "doc_id",
    "structured_row": "row_id",
}


def enqueue_kg_event(
    s: Session,
    *,
    entity_kind: KgOutboxEntityKind,
    entity_id: UUID,
    op: str = "upsert",
    scope: str,
    owner_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> None:
    """같은 PG 세션 `s`에 INSERT — 호출자 트랜잭션과 원자적으로 commit/rollback.

    Neo4j 상태와 무관하게 **무조건 enqueue**한다(§2.6 — worker가 적체 처리).
    no-op 규칙(§5.2 + K7):
    - `kg_enabled=false`: off 기간 변경은 적재하지 않는다(flag를 켤 때 rebuild 1회가
      운영 절차다 — `docs/operations.md` §2.1, rebuild가 flag보다 먼저). **이 절은
      반드시 보존**한다(fast-rollback 전제).
    - `scope='company'`: 항상 enqueue.
    - `scope='personal'`: **owner-scope hard-guard(`kg_owner_scope_enabled()`)가 켜져
      있고 `owner_id`가 있을 때만** enqueue. 그 외(flag off / owner 부재 / 비-company
      비-personal scope)는 no-op — K7 이전 `scope != 'company'` no-op과 flag-off에서
      동일하다(§1.5 `test_enqueue_noops_on_personal_today` 보존).

    company 이벤트는 `owner_id`를 NULL로 적재한다(exact-scope DELETE가
    `scope='company'`만 보도록).
    """
    if not kg_enabled():
        return
    if scope == "company":
        store_owner: UUID | None = None
    elif scope == "personal" and owner_id is not None and kg_owner_scope_enabled():
        store_owner = owner_id
    else:
        return
    s.execute(
        insert(kg_outbox).values(
            outbox_id=uuid4(),
            entity_kind=entity_kind,
            entity_id=entity_id,
            op=op,
            scope=scope,
            owner_id=store_owner,
            correlation_id=correlation_id or current_correlation_id(),
        )
    )


class OutboxRow(BaseModel):
    """`kg_outbox` claim 컬럼의 1:1 사상(§5.3).

    `scope`/`owner_id`는 K7.1(migration 0057)에서 추가됐다 — exact-scope worker
    DELETE가 이벤트에서 읽는다. claim() SELECT가 아직 두 컬럼을 안 고르는
    중간 상태에서도 OutboxRow 생성이 깨지지 않도록 기본값(company/None)을 둔다.
    """

    outbox_id: UUID
    entity_kind: str
    entity_id: UUID
    op: str
    attempts: int
    correlation_id: UUID | None = None
    scope: str = "company"
    owner_id: UUID | None = None


class KGOutboxWorker:
    """poll → claim(`FOR UPDATE SKIP LOCKED` + lease) → 멱등 apply (§5.3).

    런타임은 FastAPI lifespan background thread(§11-3 기결정)이고, 수동 drain은
    `python -m orthus.kg.outbox drain`이 같은 코드를 쓴다.
    """

    def __init__(
        self,
        *,
        poll_seconds: float | None = None,
        batch_size: int = BATCH_SIZE,
        lease_seconds: int = LEASE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.poll_seconds = (
            poll_seconds
            if poll_seconds is not None
            else float(get_settings().kg_outbox_poll_seconds)
        )
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._last_trim_monotonic = 0.0
        self._last_warning: str | None = None

    # --- poll loop ---------------------------------------------------------

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                processed = self.run_once()
            except Exception as exc:  # noqa: BLE001 — poll loop는 죽지 않는다
                # apply 실패는 audit("kg.apply")가 기록하지만 claim/release 등
                # PG-경로 실패는 여기가 유일한 관측 지점이다(리뷰 교정 — 무증거
                # 적체 방지). 동일 메시지 반복은 1회만 남긴다.
                self._warn(f"kg outbox worker error: {type(exc).__name__}: {exc}")
                processed = 0
            if processed == 0:
                stop.wait(self.poll_seconds)

    def _warn(self, message: str) -> None:
        """stderr 관측 — 같은 메시지의 연속 반복은 억제(5s poll 스팸 방지)."""
        if message == self._last_warning:
            return
        self._last_warning = message
        print(message, file=sys.stderr)

    def run_once(self) -> int:
        """1 batch claim→apply. **성공** 적용 수를 반환한다(이벤트 실패는
        attempts/lease backoff로 남으므로 집계하지 않는다 — drain/CLI 보고와
        run_forever 대기 판단의 입력).

        Neo4j 미가용/rebuild lock은 claim 해제 후 0 반환 — attempts 미증가(§2.6).
        """
        if not kg_enabled():
            return 0
        # projection 쓰기 경로는 company node 전용(K2 교정 ② — rebuild/sync와
        # 동일하게 핵심 경로에서 가드한다. lifespan 조건/CLI 가드와 별개의
        # fail-closed 방어선).
        require_company_node()
        rows = self.claim()
        if not rows:
            self._maybe_trim()
            return 0
        try:
            # driver 생성은 lazy 연결이라 미가용이 첫 쿼리(_rebuild_locked)에서
            # raw ServiceUnavailable로 드러난다 — KgUnavailable로 정규화한다.
            get_kg_driver()
            meta = self._read_meta()
            if self._rebuild_locked(meta):
                # rebuild 진행 중 — no-op 대기(§4.7/§5.5). 다음 poll에 재시도.
                self.release(rows)
                return 0
            if self._schema_version_held(meta):
                # 코드(v2)가 그래프보다 앞섬 — v2 rebuild 전까지 hold(§2). worker가
                # v2-shape 노드를 v1 그래프에 적재하는 활성화 transient를 구조적으로
                # 닫는다(sync와 같은 fail-closed). owner-scope 활성화 절차에서 rebuild가
                # flag보다 먼저이므로, rebuild 완료(version=2 set)까지 이 hold가 유지된다.
                self._warn(
                    "kg outbox holding: schema version mismatch "
                    "(code ahead of graph — run kg-rebuild)"
                )
                self.release(rows)
                return 0
        except (KgDisabled, KgUnavailable) as exc:
            self._warn(f"kg outbox idle (graph unavailable, events accumulate): {exc}")
            self.release(rows)
            return 0
        except Exception as exc:
            if _normalize_unavailable(exc) is None:
                raise
            self._warn(f"kg outbox idle (graph unavailable, events accumulate): {exc}")
            self.release(rows)
            return 0

        # batch 1회 공유 스냅샷 — load_one이 이벤트마다 전 company slug-map/doc-meta를
        # 다시 읽고 conflict index를 위해 전 task 파일을 glob/parse하던 O(corpus × 이벤트)
        # 비용을 batch당 O(corpus)로 낮춘다(리뷰 교정 — apply_one은 이벤트 단위). scope_filter는
        # 이벤트별로(`scope_filter_for_event`) 갈리므로 필터별로 한 캐시를 둔다 —
        # company/personal이 섞인 batch에서도 각 스냅샷이 자기 필터로만 쓰인다.
        from orthus.kg import project  # noqa: PLC0415 — wiki.store 순환 회피(모듈 docstring)

        caches: dict[project.ScopeFilter, project.LoadOneCache] = {}
        # batch당 한 번만 PG `applied` UPDATE를 친다 — 이벤트마다 `_mark_applied`로
        # 따로 commit하던 것을 한 번으로 모은다(리뷰 교정: applied event당 PG commit 수 감소).
        # 멱등은 Neo4j `:OutboxApplied` 마커가 권위라(이미 per-event Cypher tx에 commit됨)
        # mark 전에 worker가 죽어도 재claim→MATCH guard no-op→재mark로 수렴한다 —
        # per-event mark와 동일한 crash window이고 의미 변화 없음(§5.5).
        applied_ids: list[UUID] = []
        for i, row in enumerate(rows):
            try:
                if self.apply_one(row, caches=caches, applied_ids=applied_ids):
                    pass
            except KgUnavailable as exc:
                # 인프라 다운 — 이 row 포함 잔여 claim 해제, 재기동 시 drain. 이미 적용된
                # 앞선 row들(applied_ids)은 마커가 그래프에 있으므로 PG도 batch flush한다
                # (안 하면 pending으로 남아 재claim되지만 마커 no-op라 무해 — 그래도 정확히 닫는다).
                self._warn(f"kg outbox idle (graph unavailable, events accumulate): {exc}")
                self.release(rows[i:])
                break
        else:
            self._last_warning = None  # 정상 사이클 — 다음 incident는 다시 기록
        self._mark_applied_batch(applied_ids)
        self._maybe_trim()
        return len(applied_ids)

    # --- claim / lease (§5.3) ------------------------------------------------

    def claim(self) -> list[OutboxRow]:
        with session() as s:
            selected = s.execute(
                select(
                    kg_outbox.c.outbox_id,
                    kg_outbox.c.entity_kind,
                    kg_outbox.c.entity_id,
                    kg_outbox.c.op,
                    kg_outbox.c.attempts,
                    kg_outbox.c.correlation_id,
                    kg_outbox.c.scope,  # K7 — exact-scope DELETE가 읽는다
                    kg_outbox.c.owner_id,
                )
                .where(
                    kg_outbox.c.status == "pending",
                    (kg_outbox.c.lease_until.is_(None)) | (kg_outbox.c.lease_until < func.now()),
                )
                .order_by(kg_outbox.c.enqueued_at)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            if selected:
                s.execute(
                    update(kg_outbox)
                    .where(kg_outbox.c.outbox_id.in_([r.outbox_id for r in selected]))
                    .values(lease_until=func.now() + timedelta(seconds=self.lease_seconds))
                )
            s.commit()
        return [OutboxRow(**r._mapping) for r in selected]

    def release(self, rows: list[OutboxRow]) -> None:
        """claim 해제 — attempts 미증가(인프라 다운은 이벤트 실패가 아니다, §2.6)."""
        if not rows:
            return
        with session() as s:
            s.execute(
                update(kg_outbox)
                .where(
                    kg_outbox.c.outbox_id.in_([r.outbox_id for r in rows]),
                    kg_outbox.c.status == "pending",
                )
                .values(lease_until=None)
            )
            s.commit()

    # --- apply (§5.3 3항 / §5.4) ----------------------------------------------

    def apply_one(
        self,
        row: OutboxRow,
        *,
        caches: "dict | None" = None,
        applied_ids: "list[UUID] | None" = None,
    ) -> bool:
        """단일 Cypher 트랜잭션: 마커 검사 → 변경 적용 → 마커 CREATE.

        성공 → PG `applied` + True. 이벤트 실패 → attempts+1(5회 → `dead`),
        lease를 backoff로 유지한 채 False — 같은 이벤트를 같은 poll 사이클에서
        즉시 재시도하지 않는다(리뷰 교정: backoff 없는 즉시 5연속 소진 방지).
        인프라 미가용은 `KgUnavailable`로 전파해 호출자가 claim을 해제한다.

        `caches`는 `run_once`가 넘기는 scope_filter→LoadOneCache 맵이다(batch당 1회
        slug-map/doc-meta/conflict-index 로드). None이면(직접 호출/테스트) 캐시 없이
        매 호출 로드한다 — `_apply_change`가 결정.

        `applied_ids`가 주어지면 성공 시 PG mark를 **즉시 하지 않고** 거기에 모은다 —
        `run_once`가 batch 끝에 한 번에 UPDATE한다(applied event당 PG commit 감소,
        리뷰 교정). None이면 종전대로 즉시 `_mark_applied`(직접 호출/CLI 단발).
        """
        try:
            with audit("kg.apply", correlation_id=row.correlation_id) as span:
                span.add_meta(
                    outbox_id=str(row.outbox_id),
                    entity_kind=row.entity_kind,
                    entity_id=str(row.entity_id),
                    op=row.op,
                    attempt=row.attempts + 1,
                )
                with kg_write_session() as s, s.begin_transaction() as tx:
                    already = (
                        tx.run(
                            "MATCH (o:OutboxApplied {outbox_id: $id}) RETURN 1 LIMIT 1",
                            id=str(row.outbox_id),
                        ).single()
                        is not None
                    )
                    if not already:
                        self._apply_change(tx, row, caches=caches)
                        tx.run(
                            "CREATE (:OutboxApplied {outbox_id: $id, applied_at: datetime()})",
                            id=str(row.outbox_id),
                        ).consume()
                    tx.commit()
                span.add_meta(already_applied=already)
        except Exception as exc:
            unavailable = _normalize_unavailable(exc)
            if unavailable is not None:
                raise unavailable from exc
            self._mark_failed(row, f"{type(exc).__name__}: {exc}")
            return False
        if applied_ids is not None:
            applied_ids.append(row.outbox_id)
        else:
            self._mark_applied(row.outbox_id)
        return True

    def _apply_change(
        self, tx: "neo4j.Transaction", row: OutboxRow, *, caches: "dict | None" = None
    ) -> None:
        """§5.4 매핑. upsert는 항상 '현재 PG 상태' 재로드라 순서 역전에도 마지막
        적용이 곧 최신으로 수렴한다. 재로드 miss(이미 삭제/비-company)는 no-op +
        마커 생성(이벤트 소비) — 최종 상태는 rebuild가 권위(§5.5).

        kg_store의 merge 함수들은 `.run()`만 쓰므로 Session 대신 Transaction을
        받아도 동작한다 — 마커와 변경이 한 트랜잭션에 묶이는 멱등 보장 지점.
        """
        if row.op == "delete":
            key = _DELETE_KEYS[row.entity_kind]
            # 세 entity_kind(page_id/doc_id/row_id)는 전역 유니크 PG UUID라 키만으로 정확히
            # 한 노드를 가리킨다.
            # company delete는 **무가드**다(코드리뷰 #1) — v1 동작과 동일. v1·v2 혼합
            # 그래프에서 v1-shape 노드(:Document/:StructuredFact/:Project/placeholder)는
            # scope 속성이 없어, `WHERE n.scope='company'`를 박으면 `null = 'company'` →
            # 0 매칭으로 삭제가 조용히 실패해 노드가 부활한다(version-NULL 활성화 구간에서
            # version-hold가 못 막음). 전역 유니크 키가 이미 cross-scope 오삭제를 막으므로
            # company scope WHERE는 순이익이 없다.
            # personal delete만 exact owner-scope WHERE를 쓴다 — 삭제 범위는 **enqueued
            # 이벤트의** owner에서 오고(읽기 predicate `company OR owner=$caller`가 아니다:
            # personal delete는 그 owner 노드만 건드린다), owner가 빈 personal 이벤트는
            # `owner_id = null` → 매칭 0(over-delete 대신 no-op). 이건 미래의 non-unique
            # delete 키(sharing-revoke/mail-unlink)를 위한 defense-in-depth이기도 하다.
            if row.scope == "personal":
                tx.run(
                    f"MATCH (n {{{key}: $value}}) "
                    "WHERE n.scope = 'personal' AND n.owner_id = $owner_id "
                    "DETACH DELETE n",
                    value=str(row.entity_id),
                    owner_id=str(row.owner_id) if row.owner_id is not None else None,
                ).consume()
            else:
                tx.run(
                    f"MATCH (n {{{key}: $value}}) DETACH DELETE n",
                    value=str(row.entity_id),
                ).consume()
            return
        from orthus.kg import project  # noqa: PLC0415 — wiki.store 순환 회피(모듈 docstring)

        # upsert reload 필터는 **이벤트의 저장된 scope**에서 유도한다(코드리뷰 #5) —
        # apply 시점 live flag 재독이 flag-flip race(enqueue-on→drain-off에서 personal
        # 노드 영구 누락)를 내기 때문이다.
        scope_filter = project.scope_filter_for_event(row.scope)
        if caches is None:
            # 직접 호출/테스트 — 캐시 없이 매 호출 로드(load_one cache 인자 미전달로
            # 종전 시그니처 호환 유지).
            rows = project.load_one(row.entity_kind, row.entity_id, scope_filter=scope_filter)
        else:
            # batch 공유 캐시 — scope_filter별 LoadOneCache를 lazy 생성해 slug-map/doc-meta/
            # conflict-index를 batch당 한 번만 로드한다(O(corpus) per-event 제거).
            cache = caches.get(scope_filter)
            if cache is None:
                cache = project.LoadOneCache(scope_filter)
                caches[scope_filter] = cache
            rows = project.load_one(
                row.entity_kind, row.entity_id, scope_filter=scope_filter, cache=cache
            )
        plan = project.build(rows)
        kg_store.merge_all(tx, plan)  # type: ignore[arg-type] — .run() 프로토콜 공유
        if row.entity_kind == "wiki_page" and rows.pages:
            # 그 page의 wiki_links 유래 엣지 통째 재투영 — 삭제된 링크 수렴
            # (kg-model §3: wiki_links는 별도 entity_kind가 아니다).
            kg_store.replace_page_link_edges(tx, str(row.entity_id), plan.edges)  # type: ignore[arg-type]

    def _read_meta(self) -> "kg_store.KgMeta | None":
        with kg_write_session() as s:
            return kg_store.get_meta(s)

    def _rebuild_locked(self, meta: "kg_store.KgMeta | None") -> bool:
        # K7.5 — clock 산술 없이 boolean HOLD만 본다. rebuild가 완료/abort에서 clear한다.
        return meta is not None and meta.rebuild_in_progress

    def _schema_version_held(self, meta: "kg_store.KgMeta | None") -> bool:
        """KgMeta.kg_schema_version가 **설정돼 있고** KG_SCHEMA_VERSION과 다르면 hold(§2).

        실제 활성화 transient는 version=N(이전 rebuild가 남긴 값, 예: v1)이고 코드가
        N+1인 상태다 — 이 hold가 v2 rebuild 전까지 v2-shape 노드의 v1 그래프 적재를
        막는다. `version IS NULL`(KgMeta는 있으나 rebuild 전, bootstrap 직후/증분
        populate 중)은 **hold하지 않는다**: v1 rebuild가 없었으니 v1/v2 혼합이 없고,
        다음 rebuild가 권위로 수렴시킨다(기존 worker 동작 보존). meta 부재(빈 그래프)도
        같은 이유로 진행한다."""
        return (
            meta is not None
            and meta.kg_schema_version is not None
            and meta.kg_schema_version != KG_SCHEMA_VERSION
        )

    # --- PG 상태 전이 ----------------------------------------------------------

    def _mark_applied(self, outbox_id: UUID) -> None:
        with session() as s:
            s.execute(
                update(kg_outbox)
                .where(kg_outbox.c.outbox_id == outbox_id)
                .values(status="applied", lease_until=None, last_error=None)
            )
            s.commit()

    def _mark_applied_batch(self, outbox_ids: list[UUID]) -> None:
        """batch 적용분을 한 UPDATE/commit으로 applied 전이(리뷰 교정 — per-event commit 감소).

        멱등 권위는 Neo4j 마커라 이 PG mark가 늦거나 빠져도(worker 죽음) 재claim 시
        MATCH guard가 no-op로 흡수한다. status 가드 없이 outbox_id IN 매칭만 쓴다 —
        이 함수에 들어온 id는 전부 이번 batch에서 마커 CREATE를 성공한 것이라 dead/재claim
        race가 없고, 종전 `_mark_applied`도 status 무가드였다(동작 보존)."""
        if not outbox_ids:
            return
        with session() as s:
            s.execute(
                update(kg_outbox)
                .where(kg_outbox.c.outbox_id.in_(outbox_ids))
                .values(status="applied", lease_until=None, last_error=None)
            )
            s.commit()

    def _mark_failed(self, row: OutboxRow, error: str) -> None:
        attempts = row.attempts + 1
        status = "dead" if attempts >= self.max_attempts else "pending"
        # pending으로 남는 실패는 lease를 backoff로 유지한다 — 즉시 재claim되면
        # 일시 오류가 1초 안에 attempts 5를 소진하고 dead가 된다(리뷰 교정).
        lease_until = (
            None if status == "dead" else func.now() + timedelta(seconds=self.lease_seconds)
        )
        with session() as s:
            s.execute(
                update(kg_outbox)
                .where(kg_outbox.c.outbox_id == row.outbox_id)
                .values(
                    attempts=attempts,
                    status=status,
                    last_error=error[:2000],
                    lease_until=lease_until,
                )
            )
            s.commit()

    # --- trim (worker 내 저빈도 best-effort — 실패해도 poll loop 무영향) ---------

    def _maybe_trim(self) -> None:
        now = time.monotonic()
        if now - self._last_trim_monotonic < TRIM_INTERVAL_SECONDS:
            return
        self._last_trim_monotonic = now
        try:
            trim_applied()
        except Exception:  # noqa: BLE001 — best-effort(§5.5)
            pass


def _normalize_unavailable(exc: Exception) -> KgUnavailable | None:
    """driver 연결류/일시 예외 → KgUnavailable 정규화(인프라 다운 ≠ 이벤트 실패).

    `TransientError`(락 경합/deadlock — 재시도 가능으로 정의된 클래스)도
    포함한다: worker와 CLI drain이 lease 만료 직후 겹칠 때의 경합이 attempts를
    소진해 정당한 이벤트를 dead로 만드는 것을 막는다(리뷰 교정).
    """
    if isinstance(exc, KgUnavailable):
        return exc
    try:
        from neo4j.exceptions import (  # noqa: PLC0415 — lazy import 계약(§2.3)
            AuthError,
            ServiceUnavailable,
            SessionExpired,
            TransientError,
        )
    except ImportError:  # pragma: no cover — uv 환경에는 항상 설치됨
        return None
    if isinstance(exc, (AuthError, ServiceUnavailable, SessionExpired, TransientError)):
        return KgUnavailable(f"{type(exc).__name__}: {exc}")
    return None


def trim_applied(*, retention_days: int = APPLIED_RETENTION_DAYS) -> dict[str, int]:
    """30일 경과 `applied` row(PG)와 `:OutboxApplied` 마커(Neo4j)를 정리한다.

    마커는 자체 `applied_at` 기준으로 지운다 — PG row가 먼저 사라진 마커도
    수렴한다. 30일 넘게 pending으로 남은 이벤트의 마커가 지워지는 극단 케이스는
    재적용으로 이어지지만, apply가 '현재 PG 상태' 재로드 멱등이라 무해하다.
    `dead`는 보존(§5.5).
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    with session() as s:
        result = s.execute(
            delete(kg_outbox).where(
                kg_outbox.c.status == "applied", kg_outbox.c.enqueued_at < cutoff
            )
        )
        s.commit()
    try:
        with kg_write_session() as kg_s:
            summary = kg_s.run(
                "MATCH (o:OutboxApplied) WHERE o.applied_at < $cutoff DELETE o", cutoff=cutoff
            ).consume()
    except Exception as exc:
        # lazy 연결이라 미가용이 여기서 raw driver 예외로 드러난다 — CLI의
        # fail-loud 계약(KgUnavailable catch)이 raw traceback으로 깨지지 않게
        # 정규화한다(리뷰 교정). PG 절반은 이미 commit — 마커는 다음 trim의
        # applied_at 기준으로 수렴한다(docstring).
        unavailable = _normalize_unavailable(exc)
        if unavailable is not None:
            raise unavailable from exc
        raise
    return {"pg_rows": result.rowcount or 0, "markers": summary.counters.nodes_deleted}


# --- lifespan 통합 (§5.3 런타임) ---------------------------------------------------


def start_outbox_worker_thread() -> tuple[threading.Thread, threading.Event] | None:
    """기동 조건(§5.3): `kg_enabled and node_kind == "company"`. 미충족이면 None.

    uvicorn reload 모드에서 lifespan은 serving 자식 프로세스에서만 실행되므로
    감시용 부모에 worker가 뜨지 않는다(§12 — K3 PR에서 검증). 최악의 중복
    기동도 `:OutboxApplied` 멱등 마커가 흡수한다.
    """
    settings = get_settings()
    # kg_enabled()는 L3 force-disable(_kg_force_disabled)까지 AND한다 — raw settings.kg_enabled가
    # 아니라 이걸 봐야 owner-scope flag/게이트 불일치로 KG가 강제-off된 상태에서 worker 스레드가
    # 아예 안 뜬다(read+write 동시 차단, lifespan 주석과 일치 — 코드리뷰 #6). 내부 run_once의
    # kg_enabled() 재검사만 믿지 않고 기동 시점에 fail-closed.
    if not kg_enabled() or settings.node_kind != "company":
        return None
    stop = threading.Event()
    worker = KGOutboxWorker()
    thread = threading.Thread(
        target=worker.run_forever, args=(stop,), name="kg-outbox-worker", daemon=True
    )
    thread.start()
    return thread, stop


# --- CLI: python -m orthus.kg.outbox drain|trim (테스트·장애 복구·launchd fallback) ---


def drain(*, max_batches: int = 10_000) -> int:
    """pending 전부 적용 시도. 적용한 이벤트 수를 반환한다."""
    worker = KGOutboxWorker(poll_seconds=0)
    total = 0
    for _ in range(max_batches):
        processed = worker.run_once()
        if processed == 0:
            break
        total += processed
    return total


def status_counts() -> dict[str, int]:
    """`kg_outbox` status별 건수 — drain 보고와 K6 monitor가 공유하는 단일 출처."""
    with session() as s:
        rows = s.execute(
            select(kg_outbox.c.status, func.count()).group_by(kg_outbox.c.status)
        ).all()
    return {r[0]: r[1] for r in rows}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "drain"
    if cmd not in ("drain", "trim"):
        print(
            f"unknown command: {cmd} (usage: python -m orthus.kg.outbox [drain|trim])",
            file=sys.stderr,
        )
        return 2
    if not kg_enabled():
        print("kg outbox refused: ORTHUS_KG_ENABLED=false (fail-closed)", file=sys.stderr)
        return 1
    try:
        require_company_node()
        if cmd == "trim":
            report = trim_applied()
            print(
                f"kg outbox trim complete: pg_rows={report['pg_rows']}, markers={report['markers']}"
            )
            return 0
        applied = drain()
        counts = status_counts()
        pending = counts.get("pending", 0)
        dead = counts.get("dead", 0)
        print(f"kg outbox drain: applied={applied}, pending={pending}, dead={dead}")
        if pending:
            # CLI는 fail-loud(§2.6) — Neo4j 미가용/rebuild lock으로 못 비웠다.
            print("kg outbox drain incomplete: pending events remain", file=sys.stderr)
            return 1
        return 0
    except (KgDisabled, KgUnavailable) as exc:
        print(f"kg outbox {cmd} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
