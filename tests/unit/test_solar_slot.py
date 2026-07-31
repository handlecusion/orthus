"""`ORTHUS_LLM=solar` / `ORTHUS_EMBEDDING=solar` — Solar 단일 레지스트리 계약.

여기서 고정하는 계약:
- `solar` → Upstage 엔드포인트·모델. 엔드포인트/모델은 워커 스펙에서 읽으므로 빠뜨릴
  수 없고, 키만 빠질 수 있으며 그건 **즉시(시끄럽게) 실패**한다.
- 모르는 슬롯 값은 MockChat으로 조용히 떨어지지 않고 raise한다.
- 임베딩은 색인/질의 **양쪽 대칭**으로 `embedding-passage`를 쓴다(실측 최적).
- **캐스케이드가 꺼지지 않는다**: 폴백 provider는 primary(`s.llm`)를 기본값으로
  삼으므로, `solar` 분기가 없으면 `ORTHUS_LLM=solar`가 SVC를 조용히 무력화한다
  (E1: 77.8% → 100%).
"""

from __future__ import annotations

import pytest

from orthus.models import registry
from orthus.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(monkeypatch, **kv):
    for k, v in kv.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_solar_slot_uses_upstage_endpoint(monkeypatch):
    _env(monkeypatch, ORTHUS_LLM="solar", ORTHUS_LLM_SOLAR_API_KEY="k-solar")

    chat = registry.get_chat_model()

    assert chat.model_id == "solar-pro"
    assert "upstage.ai" in chat._base


def test_solar_slot_without_key_fails_loudly(monkeypatch):
    _env(monkeypatch, ORTHUS_LLM="solar", ORTHUS_LLM_SOLAR_API_KEY="")

    with pytest.raises(ValueError, match="ORTHUS_LLM_SOLAR_API_KEY"):
        registry.get_chat_model()


def test_solar_primary_keeps_the_cascade_alive(monkeypatch):
    """회귀: 폴백 provider는 primary를 상속한다 — `solar` 분기가 없으면 SVC가 조용히 죽는다."""
    _env(
        monkeypatch,
        ORTHUS_LLM="solar",
        ORTHUS_LLM_SOLAR_API_KEY="k-solar",
        ORTHUS_LLM_FALLBACK_MODEL="solar-mini",
        ORTHUS_LLM_FALLBACK_PROVIDER="",  # 명시 안 하면 primary(solar)를 상속한다
    )

    fb = registry.get_fallback_chat_model()

    assert fb is not None, "ORTHUS_LLM=solar가 검증 캐스케이드를 조용히 꺼뜨렸다"
    assert fb.model_id == "solar-mini"
    assert fb._retries == registry._CASCADE_RETRIES


def test_unknown_chat_slot_raises_instead_of_silently_mocking(monkeypatch):
    """오타 하나가 **시스템 전체를 가짜 응답으로** 돌리던 fail-open을 닫는다.

    이전에는 모르는 값이 `MockChat`으로 떨어졌다 — 에러도 경고도 없이, 그럴듯한 답을 내면서.
    조용하고, 확신에 차 있고, 구조적인 실패. mock은 이름으로 요청해야만 얻는다.
    """
    _env(monkeypatch, ORTHUS_LLM="so1ar")  # 오타

    with pytest.raises(ValueError, match="unknown ORTHUS_LLM"):
        registry.get_chat_model()


def test_unknown_embedding_slot_raises(monkeypatch):
    """임베딩 쪽 fail-open은 꼬리가 더 길다 — 가짜 벡터가 pgvector에 **기록**된다."""
    _env(monkeypatch, ORTHUS_EMBEDDING="solar_")  # 오타

    with pytest.raises(ValueError, match="unknown ORTHUS_EMBEDDING"):
        registry.get_embedding_model()


# --- ORTHUS_EMBEDDING=solar --------------------------------------------------------
# 임베딩은 잘못 새면 꼬리가 길다: 벡터가 **기록**되므로 슬롯이 어긋나는 순간 corpus
# 전체가 오염되고, 되돌리려면 재임베딩해야 한다.


def test_solar_embedding_uses_upstage_endpoint(monkeypatch):
    _env(monkeypatch, ORTHUS_EMBEDDING="solar", ORTHUS_EMBEDDING_SOLAR_API_KEY="k-solar")

    emb = registry.get_embedding_model()

    assert "upstage.ai" in emb._base


def test_solar_embedding_defaults_to_the_passage_model_on_both_sides(monkeypatch):
    """측정된 최적은 **대칭**이다 — 색인/질의 둘 다 `embedding-passage`.

    Upstage는 query/passage 분리를 문서화하지만, 우리 corpus에서 비대칭은 4/4 조건에서
    유의하게 나빴다(p<0.001, experiments/fugu-ko/embedding/README.md §8.6a). `embedding-query`가
    passage 공간에서 멀리 임베딩해(1위 코사인 0.489 vs 0.624) 순위도 나쁘고 `max(vec, lex)`
    블렌드에서 lexical에 먹힌다. 이 기본값을 `embedding-query`로 "고치면" 검색이 나빠진다.
    """
    _env(monkeypatch, ORTHUS_EMBEDDING="solar", ORTHUS_EMBEDDING_SOLAR_API_KEY="k-solar")

    emb = registry.get_embedding_model()

    assert emb._model == "embedding-passage"
    assert emb._dimensions == 1024  # pgvector vector(1024) 고정 — 4096은 측정상 근거 없음


def test_solar_embedding_model_version_distinguishes_the_vendor(monkeypatch):
    """`model_version`이 모델을 구분하지 못하면 혼재 벡터가 조용히 섞인다.

    서로 다른 모델의 벡터는 같은 공간에 있지 않다. 교체 시 전량 재임베딩이 필수인 이유이고,
    그 판정 근거가 이 문자열이다.
    """
    _env(monkeypatch, ORTHUS_EMBEDDING="solar", ORTHUS_EMBEDDING_SOLAR_API_KEY="k-solar")

    emb = registry.get_embedding_model()

    assert emb.model_version == "embedding-passage:1024"


def test_solar_embedding_falls_back_to_the_chat_upstage_key(monkeypatch):
    """같은 벤더·같은 계정이라 자격증명은 하나다 — 키를 두 번 설정하게 만들지 않는다."""
    _env(
        monkeypatch,
        ORTHUS_EMBEDDING="solar",
        ORTHUS_EMBEDDING_SOLAR_API_KEY="",
        ORTHUS_LLM_SOLAR_API_KEY="k-shared",
    )

    emb = registry.get_embedding_model()

    assert emb._key == "k-shared"


def test_solar_embedding_without_any_key_fails_loudly(monkeypatch):
    _env(
        monkeypatch,
        ORTHUS_EMBEDDING="solar",
        ORTHUS_EMBEDDING_SOLAR_API_KEY="",
        ORTHUS_LLM_SOLAR_API_KEY="",
    )

    with pytest.raises(ValueError, match="ORTHUS_EMBEDDING_SOLAR_API_KEY"):
        registry.get_embedding_model()


def test_mock_still_works_when_asked_for_by_name(monkeypatch):
    """테스트/CI의 mock 핀은 그대로 살아 있어야 한다."""
    _env(monkeypatch, ORTHUS_LLM="mock", ORTHUS_EMBEDDING="mock")

    assert type(registry.get_chat_model()).__name__ == "MockChat"
    assert type(registry.get_embedding_model()).__name__ == "MockEmbedding"


def test_assignment_layer_and_cascade_read_the_same_vendor_spec(monkeypatch):
    """스펙이 한 곳에만 있다는 것 자체를 고정한다 — 두 벌이 되면 한쪽이 또 뒤처진다."""
    from orthus.models import orchestration as orch

    assert orch._worker_specs is registry.vendor_specs
    assert orch.WorkerSpec is registry.VendorSpec


def test_mock_fallback_provider_still_available(monkeypatch):
    """테스트가 SVC 경로를 결정론으로 돌릴 수 있게 mock provider는 유지된다."""
    _env(
        monkeypatch,
        ORTHUS_LLM="solar",
        ORTHUS_LLM_SOLAR_API_KEY="k-solar",
        ORTHUS_LLM_FALLBACK_PROVIDER="mock",
        ORTHUS_LLM_FALLBACK_MODEL="mock-2",
    )

    fb = registry.get_fallback_chat_model()

    assert type(fb).__name__ == "MockChat"
