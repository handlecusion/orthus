"""K7.2 Step 6 (L3) — owner-scope flag/게이트 불일치 fail-closed 가드 (neo4j 불필요).

flag-on인데 게이트가 owner-variant가 아니면 KG를 process 수준 강제 off(supported:false
수렴)하고, refuse-boot은 하지 않는다. flag-off/일관 상태에선 무동작.
"""

from __future__ import annotations

import pytest

import orthus.kg.client as client
from orthus.kg.client import (
    kg_enabled,
    reset_kg_force_disabled,
    verify_owner_scope_gate_consistency,
)
from orthus.settings import get_settings


@pytest.fixture(autouse=True)
def _reset_force_disabled():
    # 명시적 래치 해제(코드리뷰: close_kg_driver는 더 이상 latch를 리셋하지 않는다).
    reset_kg_force_disabled()
    yield
    reset_kg_force_disabled()


def _set_flags(monkeypatch, *, kg, kg_owner, owner_scope):
    s = get_settings()
    monkeypatch.setattr(s, "kg_enabled", kg, raising=False)
    monkeypatch.setattr(s, "kg_owner_scope_enabled", kg_owner, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", owner_scope, raising=False)


def test_guard_noop_when_owner_scope_off(monkeypatch):
    _set_flags(monkeypatch, kg=True, kg_owner=False, owner_scope=False)
    assert verify_owner_scope_gate_consistency() is True
    assert client.kg_force_disabled() is False
    assert kg_enabled() is True


def test_guard_consistent_when_templates_owner_variant(monkeypatch):
    # 실제 owner-variant 템플릿이 존재하므로(Step 2) flag-on에서 일관.
    _set_flags(monkeypatch, kg=True, kg_owner=True, owner_scope=True)
    assert verify_owner_scope_gate_consistency() is True
    assert client.kg_force_disabled() is False
    assert kg_enabled() is True


def test_guard_forces_kg_off_when_owner_variant_absent(monkeypatch):
    # 빌드/배포 불일치 시뮬레이션 — owner_variants_present()가 False.
    _set_flags(monkeypatch, kg=True, kg_owner=True, owner_scope=True)
    monkeypatch.setattr("orthus.kg.templates.owner_variants_present", lambda: False)
    assert verify_owner_scope_gate_consistency() is False
    # KG fail-open: refuse-boot이 아니라 KG만 강제 off → kg_enabled() False로 수렴.
    assert client.kg_force_disabled() is True
    assert kg_enabled() is False


def test_guard_fails_closed_when_consistency_check_raises(monkeypatch):
    # 코드리뷰 #2 — 판정 함수가 예외를 던져도(예: 미래 템플릿이 _OWNER_SAMPLE_PARAMS에 없어
    # cypher_for(None) assert 실패) lifespan으로 전파돼 부팅을 죽이면 안 된다. 판정 불가 =
    # 불일치로 보고 KG만 강제 off(refuse-boot 아님).
    _set_flags(monkeypatch, kg=True, kg_owner=True, owner_scope=True)

    def _boom() -> bool:
        raise RuntimeError("missing sample params")

    monkeypatch.setattr("orthus.kg.templates.owner_variants_present", _boom)
    assert verify_owner_scope_gate_consistency() is False  # 예외 전파 안 함
    assert client.kg_force_disabled() is True
    assert kg_enabled() is False
