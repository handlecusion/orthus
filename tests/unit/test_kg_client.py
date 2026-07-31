"""K1 — KG driver lifecycle fail-closed/lazy 계약 (Neo4j 서버 불필요).

docs/kg-implementation-spec.md §3.4 unit 행: flag off 기본값, off 시 driver
생성 거부, 죽은 URI에서 kg_available() 예외 미전파.
"""

from __future__ import annotations

import pytest

import orthus.kg.client as kg_client
from orthus.kg.client import (
    KgDisabled,
    KgUnavailable,
    close_kg_driver,
    get_kg_driver,
    kg_available,
    kg_owner_scope_enabled,
)
from orthus.kg.schema import KG_SCHEMA_CHANGELOG, KG_SCHEMA_VERSION
from orthus.settings import Settings, get_settings


def test_kg_disabled_default(monkeypatch):
    for var in (
        "ORTHUS_KG_ENABLED",
        "ORTHUS_KG_OWNER_SCOPE_ENABLED",
        "ORTHUS_KG_URI",
        "ORTHUS_KG_USER",
        "ORTHUS_KG_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.kg_enabled is False
    assert s.kg_owner_scope_enabled is False
    assert s.kg_uri == "bolt://127.0.0.1:7687"
    assert s.kg_user == "neo4j"
    assert s.kg_password == ""
    assert s.kg_query_timeout_ms == 2000
    assert s.kg_query_limit == 50
    assert s.kg_outbox_poll_seconds == 5


def test_get_kg_driver_disabled_raises():
    settings = get_settings()
    old_enabled = settings.kg_enabled
    settings.kg_enabled = False
    close_kg_driver()
    try:
        with pytest.raises(KgDisabled):
            get_kg_driver()
        # driver 생성 시도 자체가 없어야 한다(싱글턴 캐시 비어 있음).
        assert kg_client._driver is None
        # fail-open 판정 함수는 off에서도 예외 없이 False다.
        assert kg_available() is False
    finally:
        settings.kg_enabled = old_enabled
        close_kg_driver()


def test_get_kg_driver_requires_password(monkeypatch):
    settings = get_settings()
    old = (settings.kg_enabled, settings.kg_password)
    close_kg_driver()
    settings.kg_enabled = True
    settings.kg_password = ""
    # keychain 경로도 miss로 고정해 env fallback까지 비어 있는 상태를 만든다.
    monkeypatch.setattr(kg_client, "get_secret", lambda ref: None)
    try:
        with pytest.raises(KgUnavailable):
            get_kg_driver()
    finally:
        settings.kg_enabled, settings.kg_password = old
        close_kg_driver()


def test_kg_owner_scope_enabled_hard_guard():
    """D3: kg_owner_scope_enabled()는 두 flag의 AND다 — KG flag만 켜도 P8
    owner-scope가 꺼져 있으면 no-op(personal row가 존재하지 않으므로 fail-closed)."""
    settings = get_settings()
    old = (settings.kg_owner_scope_enabled, settings.owner_scope_enabled)
    try:
        for kg_flag, p8_flag, expected in [
            (False, False, False),
            (True, False, False),  # KG flag만 — P8 off → no-op (hard-guard 핵심)
            (False, True, False),
            (True, True, True),  # 둘 다 켜져야만 유효
        ]:
            settings.kg_owner_scope_enabled = kg_flag
            settings.owner_scope_enabled = p8_flag
            assert kg_owner_scope_enabled() is expected
    finally:
        settings.kg_owner_scope_enabled, settings.owner_scope_enabled = old


def test_kg_schema_changelog_consistent():
    """KG_SCHEMA_CHANGELOG 최신 entry == KG_SCHEMA_VERSION, 버전 단조 증가."""
    versions = [v for v, _reason, _breaking in KG_SCHEMA_CHANGELOG]
    assert versions == sorted(versions)
    assert versions[-1] == KG_SCHEMA_VERSION
    assert KG_SCHEMA_VERSION == 2  # K7.1 bump (placeholder ns_key rekey, breaking)
    assert KG_SCHEMA_CHANGELOG[-1][2] is True  # v2 is breaking → rebuild 강제


def test_kg_available_false_on_dead_uri():
    settings = get_settings()
    old = (settings.kg_enabled, settings.kg_uri, settings.kg_password)
    close_kg_driver()
    settings.kg_enabled = True
    settings.kg_uri = "bolt://127.0.0.1:1"  # 죽은 URI — 연결 거부
    settings.kg_password = "dead-uri-test"
    try:
        assert kg_available() is False  # 예외 전파 금지 (§2.6 fail-open)
    finally:
        settings.kg_enabled, settings.kg_uri, settings.kg_password = old
        close_kg_driver()
