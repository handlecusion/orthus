"""K1 — Neo4j driver lifecycle: lazy import, fail-closed flag, session 래퍼.

호출 규약(docs/kg-implementation-spec.md §2.3): 읽기 경로는 `kg_read_session`만,
쓰기 경로(projection/outbox/bootstrap/last_accessed_at)는 `kg_write_session`만
쓴다. Neo4j Community Edition에는 DB 레벨 read-only 계정이 없으므로(role/grant는
Enterprise 전용) 이 코드 경계가 그 역할을 대신한다 — K4 게이트 코드에 write
session import가 등장하면 리뷰에서 거부한다.

`import neo4j`는 이 모듈의 함수 내부에서만 한다. KG off(`ORTHUS_KG_ENABLED=false`)
경로는 driver 미설치/미기동 상태에서도 무영향이어야 한다(kg-model §1 node-smoke
계약).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import TYPE_CHECKING, Any

from orthus.secrets import SecretStoreError, get_secret
from orthus.settings import get_settings

if TYPE_CHECKING:
    import neo4j

# keychain secret ref (connector secret 패턴 동형). env ORTHUS_KG_PASSWORD는
# bootstrap/dev fallback이다 — 구현 명세 §2.4 해석 순서.
KG_PASSWORD_SECRET_REF = "orthus/kg/password"


class KgDisabled(RuntimeError):
    """`ORTHUS_KG_ENABLED=false` — 호출측 분기용."""


class KgUnavailable(RuntimeError):
    """driver 미설치 / 연결 실패 / 인증 실패 / password 미설정의 정규화 예외."""


_driver: Any = None
_driver_lock = Lock()

# 가용성 캐시(§2.6 / 구현 명세 §9.3 가드 ⓒ): KG가 내려간 동안 모든 호출이
# verify_connectivity connect-timeout을 반복해서 물지 않도록 False 결과를 짧게 캐시한다.
# 정상(True)도 아주 짧게 캐시한다: K4b가 graph-routed `/ask`마다 kg_available()를 호출
# 하므로(핫패스), 정상 클러스터에서 매 요청 verify_connectivity 왕복을 물지 않게 한다.
# **트레이드오프:** 음성 캐시가 차 있으면 그 사이 복구돼도 ≤_AVAIL_NEG_TTL_S, 양성
# 캐시가 차 있으면 그 사이 다운돼도 ≤_AVAIL_POS_TTL_S 동안 stale 보고한다(둘 다 의도된
# bounded fail-open — 양성 stale은 곧바로 run_kg_template가 fail-open으로 demote). 양성
# TTL은 음성보다 훨씬 짧게 둔다(다운 감지 지연 최소화). _driver_lock으로 보호하고
# close_kg_driver에서 리셋한다(테스트 격리 + 운영자 재기동 정합).
_AVAIL_NEG_TTL_S = 30.0
_AVAIL_POS_TTL_S = 5.0
_avail_neg_until: float = 0.0
_avail_pos_until: float = 0.0

# K9 (R7) — entity presence(":Entity 노드가 하나라도 있나")의 process-global 캐시. 패널 티저
# (C-C/U2)가 "엔티티 존재 시에만" 자동 노출되게 하는 presence-gate의 입력이다. **page-무관**
# (전역 존재)이라 page-keyed 금지 — kg_available과 같은 monotonic TTL 캐시 단일 슬롯이다.
# presence는 rebuild(드뭄)에만 바뀌므로 양/음성 동일 TTL(60s)로 둔다. dormant(kg_entities=0)
# 동안 모든 page 뷰가 LIMIT-1 probe를 반복하지 않게 한다(2차 호출/row 0). bounded staleness:
# rebuild로 엔티티가 적재돼도 ≤TTL 동안 false stale → 티저 미노출(무해, 곧 노출).
_ENTITIES_PRESENT_TTL_S = 60.0
_entities_present_until: float = 0.0
_entities_present_value: bool = False


# L3 (kg-k7.2-plan §6) — owner-scope flag/게이트 불일치 시 process 수준 KG 강제 off.
# kg_enabled()가 이 값을 AND하므로 모든 KG 경로(read gate·outbox worker)가 동시에
# fail-closed(200 supported:false)로 수렴한다. refuse-boot이 아니라 KG만 끈다.
_kg_force_disabled = False


def kg_enabled() -> bool:
    return get_settings().kg_enabled and not _kg_force_disabled


def force_disable_kg() -> None:
    """L3 — process 수준 KG 강제 off(부팅 가드 전용)."""
    global _kg_force_disabled
    _kg_force_disabled = True


def reset_kg_force_disabled() -> None:
    """L3 강제-off 래치 해제 — **테스트/운영 재검증 전용**. driver 종료(`close_kg_driver`)와
    분리한다(코드리뷰): close가 이 래치를 리셋하면, 부팅 시 mismatch로 KG를 끈 장수
    프로세스에서 driver 재연결류가 close를 호출할 때 mismatched 게이트로 KG가 조용히
    재활성돼 L3가 막으려던 cross-owner 노출이 재개된다. 래치 해제는 명시적 재검증의
    몫이지 connection 수명주기의 부수효과가 아니다."""
    global _kg_force_disabled
    _kg_force_disabled = False


def kg_force_disabled() -> bool:
    return _kg_force_disabled


def verify_owner_scope_gate_consistency() -> bool:
    """L3 inter-PR fail-closed 가드 — lifespan startup에서 호출.

    owner-scope flag가 켜졌는데 게이트 템플릿이 owner-variant가 아니면(빌드/배포
    불일치 = K7.2 게이트 코드 없이 K7.1 projection flag만 켜진 위험 상태) KG를
    process 수준에서 강제 off하고 `kg.owner_scope_mismatch` audit를 남긴다. KG
    fail-open 원칙대로 API는 정상 부팅하되 모든 KG 경로가 supported:false로 수렴해
    개인 row가 절대 노출되지 않는다. 반환: 일관(또는 flag off)이면 True, 강제 off면 False.
    """
    if not kg_owner_scope_enabled():
        return True
    # 일관성 판정 자체의 어떤 예외도 fail-CLOSED로 수렴시킨다(코드리뷰): owner_variants_present
    # 가 예외를 던지면(예: 미래 템플릿이 _OWNER_SAMPLE_PARAMS에 없어 cypher_for(None) assert
    # 실패) 그 예외가 lifespan으로 전파돼 API 부팅을 죽이는데, 그건 L3 계약('refuse-boot 아님 —
    # KG만 강제 off')을 뒤집는 hard outage다. 판정 불가 = 불일치로 보고 KG만 끈다.
    consistent = False
    reason = "owner_variant_absent"
    try:
        # lazy import — templates가 client를 import하므로 module-load 순환을 피한다.
        from orthus.kg.templates import owner_variants_present  # noqa: PLC0415

        consistent = owner_variants_present()
    except Exception as exc:  # noqa: BLE001 — 판정 예외도 fail-closed(KG off), 부팅은 계속
        reason = f"gate_check_error:{type(exc).__name__}"
    if consistent:
        return True
    force_disable_kg()
    try:
        from orthus.audit.logger import audit  # noqa: PLC0415

        with audit("kg.owner_scope_mismatch") as span:
            span.add_meta(forced_kg_disabled=True, reason=reason)
    except Exception:  # noqa: BLE001 — 경보 실패가 부팅을 막지 않는다
        pass
    return False


def kg_owner_scope_enabled() -> bool:
    """K7 owner-scope projection/read의 **유효** 게이트 — 단일 출처.

    D3 hard-guard: `ORTHUS_KG_OWNER_SCOPE_ENABLED`가 켜져도 P8
    `ORTHUS_OWNER_SCOPE_ENABLED`가 꺼져 있으면 no-op이다. personal owner-scope
    row는 P8 owner-scope가 켜진 central에만 존재하므로(kg-k7-plan D3), 두 flag를
    AND로 묶어 무의미·모순 상태(개인 row 없는데 owner-scope 투영 시도)를
    fail-closed로 막는다. projection SELECT 확장, enqueue 분기, (K7.2) 게이트
    owner-variant 선택이 전부 이 함수를 참조한다 — raw flag를 직접 보지 않는다.
    """
    settings = get_settings()
    return settings.kg_owner_scope_enabled and settings.owner_scope_enabled


def require_company_node() -> None:
    """KG projection 쓰기 경로(rebuild/sync, K3 worker)의 company-node 강제.

    Neo4j는 loopback 공유라 personal node env에서 rebuild가 돌면 personal DB
    기준 prune이 company 그래프를 삭제한다 — 셸 가드(sync_cycle.sh)와 별개로
    코드 레벨에서 fail-closed로 막는다(`mail_pull_ingest`의 node_kind 강제와
    동형). K2 projection 자체도 company scope row만 읽지만(kg-model §3), 이
    가드는 '어느 DB를 SoR로 보느냐'를 막는 장치라 중복이 아니다.
    """
    node_kind = get_settings().node_kind
    if node_kind != "company":
        raise KgDisabled(f"KG projection is company-node only (node_kind={node_kind!r})")


def _resolve_password() -> str:
    try:
        value = get_secret(KG_PASSWORD_SECRET_REF)
    except SecretStoreError:
        # secret backend 미가용(비-macOS auto 등)은 miss로 취급 — env fallback.
        value = None
    if value:
        return value
    env_value = get_settings().kg_password
    if env_value:
        return env_value
    # 평문 기본 password를 코드에 두지 않는다 — 미설정이면 생성 거부.
    raise KgUnavailable(
        "KG password not configured (keychain ref orthus/kg/password or ORTHUS_KG_PASSWORD)"
    )


def get_kg_driver() -> "neo4j.Driver":
    """프로세스 싱글턴 driver. flag off면 `KgDisabled`, 생성 실패는 `KgUnavailable`."""
    global _driver
    if not kg_enabled():
        raise KgDisabled("ORTHUS_KG_ENABLED is false")
    if _driver is not None:
        return _driver
    with _driver_lock:
        if _driver is not None:
            return _driver
        password = _resolve_password()
        settings = get_settings()
        try:
            import neo4j  # noqa: PLC0415 — lazy import 계약 (§2.3)
        except ImportError as exc:  # pragma: no cover — uv 환경에는 항상 설치됨
            raise KgUnavailable(f"neo4j driver not installed: {exc}") from exc
        try:
            created = neo4j.GraphDatabase.driver(settings.kg_uri, auth=(settings.kg_user, password))
        except Exception as exc:
            raise KgUnavailable(f"{type(exc).__name__}: {exc}") from exc
        _driver = created
        return _driver


def kg_available() -> bool:
    """`verify_connectivity()` best-effort — False여도 예외 전파 금지.

    K4 unsupported 응답(`reason="kg_unavailable"`)의 판정 함수다(§2.6 fail-open).
    음성 결과는 `_AVAIL_NEG_TTL_S`, 양성 결과는 `_AVAIL_POS_TTL_S` 동안 캐시해, KG가
    내려간 동안에도/정상일 때도 호출자(K4 게이트·K4b graph 분기·missing_link 동기-가드
    등)가 매 호출 connect-timeout/verify 왕복을 물지 않게 한다."""
    global _avail_neg_until, _avail_pos_until
    now = time.monotonic()
    with _driver_lock:
        neg_until = _avail_neg_until
        pos_until = _avail_pos_until
    if now < neg_until:
        return False
    if now < pos_until:
        return True
    try:
        get_kg_driver().verify_connectivity()
    except Exception:
        # Stamp the TTL from AFTER the probe completes, not from `now` at entry: a slow
        # verify_connectivity (handshake/timeout taking seconds) would otherwise burn most
        # of the (short, 5s) positive window before the call even returns, defeating the
        # per-/ask hot-path cache.
        with _driver_lock:
            _avail_neg_until = time.monotonic() + _AVAIL_NEG_TTL_S
        return False
    with _driver_lock:
        _avail_pos_until = time.monotonic() + _AVAIL_POS_TTL_S
    return True


def entities_present() -> bool:
    """K9 (C-C/U2/R7) — ":Entity 노드가 그래프에 하나라도 있나"의 best-effort presence-gate.
    패널 entity-티저를 엔티티 존재 시에만 자동 노출하고, dormant(kg_entities=0) 동안 매 page
    뷰가 2차 Neo4j 왕복을 물지 않게 process-global TTL 캐시한다(kg_available 패턴 재사용).

    fail-OPEN으로 **False**를 돌려준다 — flag-off/미가용/예외면 티저를 안 띄운다(없는 게 안전).
    LIMIT-1 presence probe(monitor tripwire 패턴 — full COUNT 아님)라 위반 없이 가볍다."""
    global _entities_present_until, _entities_present_value
    if not kg_enabled():
        return False
    now = time.monotonic()
    with _driver_lock:
        if now < _entities_present_until:
            return _entities_present_value
    if not kg_available():
        return False
    try:
        recs = run_read("MATCH (e:Entity) WITH e LIMIT 1 RETURN count(e) AS c", {})
        present = bool(recs and int(list(recs[0].values())[0]) > 0)
    except Exception:  # noqa: BLE001 — fail-open: probe 실패는 티저 미노출(False)
        return False
    with _driver_lock:
        _entities_present_value = present
        _entities_present_until = time.monotonic() + _ENTITIES_PRESENT_TTL_S
    return present


@contextmanager
def kg_read_session() -> Iterator["neo4j.Session"]:
    """읽기 전용 세션 — K4 게이트 전용 진입로.

    transaction timeout의 단일 출처는 `settings.kg_query_timeout_ms`다. K4 게이트는
    이 세션 안에서 timeout을 별도로 만들지 않고 `read_timeout_seconds()`를 쓴다.
    """
    import neo4j  # noqa: PLC0415 — lazy import 계약

    with get_kg_driver().session(
        default_access_mode=neo4j.READ_ACCESS, database="neo4j"
    ) as session:
        yield session


def read_timeout_seconds() -> float:
    """K4 게이트가 transaction timeout으로 쓰는 값 — 호출자 자체 timeout 금지(§2.3)."""
    return get_settings().kg_query_timeout_ms / 1000.0


def run_read(cypher: str, params: dict[str, Any]) -> list[Any]:
    """READ 세션 + transaction timeout으로 쿼리 1개를 실행해 record 목록을 반환.

    K4 게이트의 유일한 실행 진입로다 — `neo4j.Query` 생성(lazy import)과 timeout
    적용을 이 함수가 전담하므로 게이트 모듈에는 neo4j import가 등장하지 않는다
    (§2.1 lazy import 계약). timeout 값의 단일 출처는 `read_timeout_seconds()`.
    """
    import neo4j  # noqa: PLC0415 — lazy import 계약

    with kg_read_session() as session:
        query = neo4j.Query(cypher, timeout=read_timeout_seconds())
        return list(session.run(query, **params))


def is_timeout_error(exc: Exception) -> bool:
    """driver 예외가 transaction timeout인지 판정 — 정규화 단일 지점.

    timeout 예외의 클래스/status code는 driver 버전에 따라 다를 수 있다
    (구현 명세 §12). 버전 업그레이드로 K4 timeout 회귀가 깨지면 이 함수만
    고친다."""
    code = getattr(exc, "code", None) or ""
    if "TransactionTimedOut" in code:
        return True
    return "transaction timeout" in str(exc).lower()


@contextmanager
def kg_write_session() -> Iterator["neo4j.Session"]:
    """projection/outbox/bootstrap 및 last_accessed_at best-effort 기록 전용."""
    import neo4j  # noqa: PLC0415 — lazy import 계약

    with get_kg_driver().session(
        default_access_mode=neo4j.WRITE_ACCESS, database="neo4j"
    ) as session:
        yield session


def close_kg_driver() -> None:
    """테스트 teardown / 프로세스 종료용 — 싱글턴 + 가용성 캐시(양성/음성) 무효화 포함.

    L3 강제-off 래치(`_kg_force_disabled`)는 **여기서 리셋하지 않는다**(코드리뷰) —
    driver 수명주기와 분리한다. 테스트 격리는 `reset_kg_force_disabled()`를 명시 호출한다."""
    global _driver, _avail_neg_until, _avail_pos_until
    global _entities_present_until, _entities_present_value
    with _driver_lock:
        _avail_neg_until = 0.0
        _avail_pos_until = 0.0
        _entities_present_until = 0.0  # K9 — entity presence 캐시도 무효화(테스트 격리)
        _entities_present_value = False
        if _driver is not None:
            try:
                _driver.close()
            except Exception:  # noqa: BLE001 — teardown은 best-effort
                pass
            _driver = None
