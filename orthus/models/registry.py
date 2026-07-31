"""Adapter selection by env. Single source for which model fills each slot.

Solar-only build (docs/model-orchestration.md): every real slot in the system —
chat/compile, retrieval embedding, the structured verification cascade, and the
in-process agentic /ask engine — is backed by Upstage Solar over the
OpenAI-compatible protocol. `mock` remains the deterministic offline slot that
tests and CI pin.

LLM/chat and embedding are intentionally separate slots:
- `get_chat_model()` is the default chat slot: compile/answer generation, the
  flag-off path, and the last fallback rung. Measured tasks (including distill)
  route through `orchestration.get_chat_model_for()`, which falls back here.
- `get_embedding_model()` backs corpus/wiki retrieval indexes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from orthus.models.adapters.mock import MockChat, MockEmbedding
from orthus.models.adapters.openai_compat import _RETRIES as _ADAPTER_RETRIES
from orthus.models.base import ChatModel, EmbeddingModel
from orthus.settings import get_settings

logger = logging.getLogger(__name__)

# The accepted values, in one place, so the error message cannot drift from the branches
# below. Anything outside these raises — see the note at the end of each selector.
CHAT_SLOTS = ("mock", "solar")
EMBEDDING_SLOTS = ("mock", "solar")


@dataclass(frozen=True)
class VendorSpec:
    """A chat vendor endpoint. Values are the ones the experiments actually ran with."""

    slug: str
    base_url: str
    model: str
    api_key: str
    timeout: float = 40.0
    extra_body: dict = field(default_factory=dict)
    min_interval: float = 0.0  # seconds between calls, for vendors with a req/s cap


VENDORS = ("solar",)


def vendor_specs() -> dict[str, VendorSpec]:
    s = get_settings()
    return {
        "solar": VendorSpec(
            "solar", s.llm_solar_base_url, s.llm_solar_model, s.llm_solar_api_key, timeout=40.0
        ),
    }


def build_vendor_chat(
    spec: VendorSpec, *, retries: int, model: str | None = None
) -> ChatModel | None:
    """None when the vendor is not configured — the caller then keeps its default.

    `model` overrides the spec's model id: the cascade names its second model by env
    (`ORTHUS_LLM_FALLBACK_MODEL`) while still needing the vendor's endpoint and limits.
    """
    model_id = (model or spec.model).strip()
    if not spec.api_key.strip() or not model_id:
        return None
    from orthus.models.adapters.openai_compat import OpenAIChat

    return OpenAIChat(
        spec.base_url,
        spec.api_key,
        model_id,
        spec.timeout,
        extra_body=spec.extra_body,
        min_interval=spec.min_interval,
        retries=retries,
    )


# The cascade's second call happens inside a request that already spent one round trip. It
# has nothing behind it — on failure we keep the primary's (suspect) answer — so a long
# backoff would make the user wait minutes for a *maybe*. Two attempts absorb a single 429
# without turning a wrong answer into a slow wrong answer.
_CASCADE_RETRIES = 2

# The default slot (`ORTHUS_LLM=solar`) has nothing behind it either, but it backs bulk wiki
# compile, where giving up means the document never gets indexed. It keeps the adapter's
# full budget.
_DEFAULT_RETRIES = _ADAPTER_RETRIES


def get_embedding_model() -> EmbeddingModel:
    s = get_settings()
    if s.embedding == "solar":
        # Upstage speaks the OpenAI-compatible protocol, so this reuses the same
        # adapter. The endpoint and model come from the spec, so they cannot be
        # omitted — only the key can, and that fails loudly below.
        #
        # The model is `embedding-passage` for BOTH index and query. That is not an
        # oversight: Upstage documents a query/passage split, and it measured
        # significantly WORSE on our corpus in all four conditions (p<0.001,
        # experiments/fugu-ko/embedding/README.md §8.6a) — `embedding-query` lands far
        # from passage space (top-1 cosine 0.489 vs 0.624), so it both ranks worse and
        # loses to the lexical arm inside `_merge_candidates`'s `max(vec, lex)`. The
        # Protocol having no mode argument IS the symmetric configuration, and symmetric
        # is what won. Do not wire the split.
        from orthus.models.adapters.openai_compat import OpenAIEmbedding

        key = s.embedding_solar_api_key or s.llm_solar_api_key
        if not key.strip():
            raise ValueError(
                "ORTHUS_EMBEDDING=solar requires ORTHUS_EMBEDDING_SOLAR_API_KEY "
                "(or ORTHUS_LLM_SOLAR_API_KEY — same Upstage account)"
            )
        return OpenAIEmbedding(
            s.embedding_solar_base_url,
            key,
            s.embedding_solar_model,
            dimensions=s.embedding_dimensions,
        )
    if s.embedding == "mock":
        return MockEmbedding()
    # Fail loudly, with a longer tail than the chat slot: mock vectors get WRITTEN to
    # pgvector, so a typo here does not just degrade one request — it silently poisons
    # the index, and every later retrieval reads the poison back as if it were
    # evidence. Recovering means re-embedding the corpus.
    raise ValueError(
        f"unknown ORTHUS_EMBEDDING={s.embedding!r} (expected: {', '.join(EMBEDDING_SLOTS)})"
    )


def get_chat_model() -> ChatModel:
    s = get_settings()
    if s.llm == "solar":
        # The endpoint and model come from the vendor spec, so they cannot be omitted —
        # only the key can, and that fails loudly here.
        chat = build_vendor_chat(vendor_specs()["solar"], retries=_DEFAULT_RETRIES)
        if chat is None:
            raise ValueError("ORTHUS_LLM=solar requires ORTHUS_LLM_SOLAR_API_KEY")
        return chat
    if s.llm == "mock":
        return MockChat()
    # An unrecognised value used to land here and get MockChat, which is the worst possible
    # failure: a typo (`so1ar`, a trailing space, a vendor we never wired) would put the
    # WHOLE system on canned answers with no error, no warning, and output that still looks
    # like an answer. Silent, confident, and structural — the exact failure mode this
    # codebase exists to prevent. Mock must be asked for by name.
    raise ValueError(f"unknown ORTHUS_LLM={s.llm!r} (expected: {', '.join(CHAT_SLOTS)})")


def get_fallback_chat_model() -> ChatModel | None:
    """Second (verification) model slot for the structured cascade — SVC.

    A SIBLING of `get_chat_model()` (which stays an argument-less single slot); it
    never changes which model the primary path uses. Returns None — fail-closed,
    i.e. the cascade is entirely off — when `ORTHUS_LLM_FALLBACK_MODEL` is unset.

    What the cascade needs from its second model is NOT accuracy — it is decorrelated
    error: a second opinion that fails on different questions than the first (E1). In
    the Solar-only build that means a DIFFERENT Solar model (e.g. `solar-mini` behind
    a `solar-pro` primary):

        ORTHUS_LLM_FALLBACK_PROVIDER=solar
        ORTHUS_LLM_FALLBACK_MODEL=solar-mini
    """
    s = get_settings()
    model = s.llm_fallback_model.strip()
    if not model:
        return None
    provider = (s.llm_fallback_provider or s.llm).strip()
    # A second model that is the SAME model is not a verification pass — it is the
    # first model asked twice. The cascade only recovers an error the second model
    # does not share, so a systematic bug survives it untouched while the extra call
    # is still paid for (~1.22x). Warn rather than refuse: an operator may be
    # mid-rollout, and a cascade that costs a call and fixes nothing is still not a
    # correctness bug. But it must not be silent.
    if provider in VENDORS:
        spec = vendor_specs()[provider]
        if model == spec.model.strip():
            logger.warning(
                "ORTHUS_LLM_FALLBACK_MODEL is the same model as the primary (%s/%s). "
                "The verification cascade can only recover errors the second model does "
                "NOT share, so systematic failures survive it while the extra call is "
                "still paid. Set a different model (e.g. solar-mini).",
                provider,
                model,
            )
        return build_vendor_chat(spec, retries=_CASCADE_RETRIES, model=model)
    if provider == "mock":
        return MockChat()
    # Anything unknown has no per-model slot → fail closed to no cascade.
    return None


def get_agent_chat_model():
    """Engine slot for the inline agentic /ask loop (docs/inline-agentic-ask.md).

    SEPARATE from `get_chat_model()` (the default compile/answer chat slot). Gated
    fail-closed on `agentic_ask_enabled`; returns None when off/unconfigured so the
    router falls back to the legacy wiki/structured ladder.

    The engine is Solar's native function calling: `OpenAIChat.run_tool_loop` drives
    an in-process tool-use loop over the deterministic tool backends (wiki grounding,
    the sqlglot-gated structured query, KG template gate). The loop is orchestration
    only — correctness lives in the tools.
    """
    s = get_settings()
    if not s.agentic_ask_enabled:
        return None
    return build_vendor_chat(vendor_specs()["solar"], retries=1)
