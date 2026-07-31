"""검증 캐스케이드의 2차 모델이 1차와 같으면 경고한다.

같은 모델을 2차로 두면 **검증 패스가 아니라 1차를 두 번 묻는 것**이다. 캐스케이드는 2차 모델이
**공유하지 않는** 오류만 회수하므로, **체계적 버그는 그대로 살아남고 호출 비용(~1.22×)만 낸다.**

가설이 아니다: E2 루프 측정에서 2차를 1차와 같은 모델로 뒀더니, 1차의 zero-answer 2건이
**3회 모두 그대로 살아남았다.** SVC 2차의 요건이 정확도가 아니라 **오류 비상관**인 이유다.
Solar 단일 빌드에서는 같은 벤더의 **다른 모델**(solar-mini)이 2차다.
"""

from __future__ import annotations

import logging

import pytest

from orthus.models.registry import get_fallback_chat_model
from orthus.settings import get_settings


@pytest.fixture
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm", "solar")
    monkeypatch.setattr(s, "llm_solar_api_key", "k-solar")
    monkeypatch.setattr(s, "llm_solar_model", "solar-pro")
    monkeypatch.setattr(s, "llm_fallback_provider", "")
    return s


def test_same_model_as_primary_warns(_settings, monkeypatch, caplog) -> None:
    """★ 조용히 무의미해지지 않는다."""
    monkeypatch.setattr(_settings, "llm_fallback_model", "solar-pro")
    with caplog.at_level(logging.WARNING, logger="orthus.models.registry"):
        get_fallback_chat_model()
    assert any("same model as the primary" in r.message for r in caplog.records), (
        "1차와 같은 모델을 2차로 두면 캐스케이드가 체계적 버그를 못 잡는다 — 경고해야 한다"
    )


def test_different_second_model_does_not_warn(_settings, monkeypatch, caplog) -> None:
    """대조군: 다른 모델이면 경고하지 않는다(정상 SVC 구성 — solar-mini)."""
    monkeypatch.setattr(_settings, "llm_fallback_model", "solar-mini")
    with caplog.at_level(logging.WARNING, logger="orthus.models.registry"):
        fb = get_fallback_chat_model()
    assert fb is not None
    assert fb.model_id == "solar-mini"
    assert not any("same model as the primary" in r.message for r in caplog.records)


def test_unset_fallback_is_still_off(_settings, monkeypatch, caplog) -> None:
    """마스터 스위치가 비어 있으면 캐스케이드 자체가 off — 경고도 없다."""
    monkeypatch.setattr(_settings, "llm_fallback_model", "")
    with caplog.at_level(logging.WARNING, logger="orthus.models.registry"):
        assert get_fallback_chat_model() is None
    assert not caplog.records
