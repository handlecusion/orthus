"""Task-aware model assignment over the single chat slot (docs/model-orchestration.md).

`get_chat_model()` gives every caller the same model. This module keeps the
task-slot seam that the Fugu-KO measurement campaign built — every LLM call site
in the system names its task, and the table below decides which model serves it.

In this Solar-only build the table resolves every task to **Solar**. That is not
a shortcut; it is what the measurement supported: on the held-out benchmark no
pairwise difference between the candidate models was significant on any task
(McNemar p > 0.05 everywhere), and Solar was first-or-tied on most tasks, the
fastest (p50 698ms), and the only candidate with no disqualifying failure mode
(native tool calling, 0 JSON-mode failures, 0% distill pollution). Collapsing
the whole table onto Solar therefore costs nothing measurable and buys one
vendor, one credential, and one operational surface. The per-task seam stays so
the assignment remains measurable per task — see experiments/fugu-ko.

Selection is a **constant dictionary lookup** — no confidence score, no learned
selector, no LLM call. That is deliberate: the Fugu-KO experiment trained a
question-level learned selector (three tiers) and it *lost* to this table on the
real workload, while also adding an inference per request. The table is both
better and free.

Fail-closed: with the flag off, or the worker unconfigured, every task resolves
to `get_chat_model()` — i.e. default behaviour, exactly.
"""

from __future__ import annotations

from orthus.audit import audit
from orthus.models.base import ChatModel
from orthus.models.registry import VendorSpec, build_vendor_chat, get_chat_model, vendor_specs
from orthus.settings import get_settings

# --- Tasks we measured. Only these may be assigned. ---------------------------
# Anything not listed keeps the default model.
TASK_STRUCTURED = "structured"  # NL->SQL              (structured/query.py)
TASK_ROUTING = "routing"  # route classify             (router/route.py)
TASK_WIKI_QA = "wiki_qa"  # grounded answer            (wiki/qa.py)
TASK_INTENT = "intent"  # command-vs-question          (router/route.py)
TASK_DECOMPOSE = "decompose"  # split compound Q       (router/decompose.py)
TASK_SYNTHESIZE = "synthesize"  # merge partials       (router/decompose.py)
TASK_GRAPH_BIND = "graph_bind"  # KG intent + noun-phrase extraction (router/graph.py)
TASK_DELEGATION_EXTRACT = "delegation_extract"  # delegation detect (agentwork/delegation.py)
TASK_EMAIL_DRAFT = "email_draft"  # draft/reply mail    (agentwork/service.py)
TASK_GAP_SUGGEST = "gap_suggest"  # data-gap suggestion (wiki/gap.py)
TASK_CLAIM_HEADLINE = "claim_headline"  # claim headline (wiki/backfill_claim_headline.py)
TASK_DISTILL = "distill"  # wiki authoring (wiki/distill.py, connectors/base.py)
TASK_FOLLOWUP_REWRITE = "followup_rewrite"  # follow-up deixis resolution (router/rewrite.py)

# --- Assignment table ----------------------------------------------------------
# Solar everywhere. Kept as an explicit per-task table (rather than a constant)
# because the seam is the point: each slot was measured separately and can be
# re-measured separately (experiments/fugu-ko). Highlights from the holdout:
#
#   task            n    solar   (accuracy unless noted)
#   structured     59    86.4%
#   routing        28    92.9%
#   intent         20    100%
#   decompose      16    81.2%
#   graph_bind     32    100%
#   delegation     22    95.5% (FP=1 — behind two deterministic guards)
#   wiki_qa        30    12 wins / 0 losses vs judged alternatives
#   gap_suggest    12    0 off-spec
#   distill        —     8.4 claims/doc @ 100% precision @ 9.3s/doc
#   latency p50          698ms (fastest measured)
ASSIGNMENTS: dict[str, str] = {
    TASK_STRUCTURED: "solar",
    TASK_ROUTING: "solar",
    TASK_WIKI_QA: "solar",
    TASK_INTENT: "solar",
    TASK_DECOMPOSE: "solar",
    TASK_SYNTHESIZE: "solar",
    TASK_GRAPH_BIND: "solar",
    TASK_DELEGATION_EXTRACT: "solar",
    TASK_EMAIL_DRAFT: "solar",
    TASK_GAP_SUGGEST: "solar",
    TASK_CLAIM_HEADLINE: "solar",
    TASK_DISTILL: "solar",
    TASK_FOLLOWUP_REWRITE: "solar",
}

# NOTE on distill: the assignment is only sound WITH the lifted claim cap in
# wiki/distill.py. An earlier prompt capped claims at 8 with no lower bound and
# produced an apparent −40% coverage trade (4.8 vs 7.1 claims/doc); lifting the
# cap showed the trade was a prompt artefact, not a model property — Solar
# distills 8.4 claims/doc at 100% precision (T14, docs/model-orchestration.md §13).


# An assigned worker has the default model waiting right behind it, so it should
# give up quickly rather than make a live request sit through the adapter's full
# backoff ladder (~76s of sleep across six attempts) to reach a model we could
# have used immediately. One retry still absorbs a single blip or a Retry-After.
_WORKER_RETRIES = 1


# The vendor spec (endpoint, model, timeout) lives in `registry` — the single
# source for which model fills each slot.
WorkerSpec = VendorSpec  # kept as the assignment layer's local name for the shared spec
_worker_specs = vendor_specs


def _build_worker(spec: WorkerSpec) -> ChatModel | None:
    """None when the worker is not configured — the caller then keeps the default."""
    return build_vendor_chat(spec, retries=_WORKER_RETRIES)


class FallbackChat:
    """Assigned worker with the default slot behind it.

    Availability beats quality: if the assigned model errors, times out, or hits a
    rate limit, the request still completes on the default slot. Every fallback is
    audited — a silently-degrading route would otherwise look like the assigned
    model is serving traffic when it is not, and a rising fallback rate is exactly
    how we learn an assignment is failing.
    """

    def __init__(self, task: str, primary: ChatModel, backups: list[ChatModel]):
        self.task = task
        self._primary = primary
        self._backups = backups
        self.model_id = primary.model_id

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        try:
            return self._primary.complete(system, user, json_only=json_only)
        except Exception as first:  # noqa: BLE001 — any worker failure falls back
            last = first
            for backup in self._backups:
                with audit("model.fallback") as span:
                    span.add_meta(
                        task=self.task,
                        assigned=self._primary.model_id,
                        fallback=backup.model_id,
                        reason=type(last).__name__,
                    )
                try:
                    return backup.complete(system, user, json_only=json_only)
                except Exception as e:  # noqa: BLE001 — try the next rung
                    last = e
            raise last


def get_chat_model_for(task: str) -> ChatModel:
    """The model assigned to `task`, else the default model.

    Falls back to `get_chat_model()` when the default slot is mock, the flag is off,
    the task is unassigned, or the assigned worker has no credentials configured.
    """
    s = get_settings()
    default = get_chat_model()
    # `ORTHUS_LLM=mock` means "deterministic, offline" — that is how tests and CI pin
    # the chat slot. Routing to a real worker here would punch a hole straight through
    # that pin: a developer with credentials in their local .env would silently make
    # the suite call real APIs over the network, and the results would drift with
    # whatever the model happens to answer. Mock stays mock.
    if s.llm == "mock":
        return default
    if not s.model_orchestration_enabled:
        return default
    slug = ASSIGNMENTS.get(task)
    if slug is None:
        return default
    specs = _worker_specs()
    worker = _build_worker(specs[slug])
    if worker is None:
        return default
    # The default slot is the only rung behind the worker. When the default slot
    # IS the same Solar endpoint, this still matters: the worker runs on the short
    # retry budget while the default keeps the full one.
    return FallbackChat(task, worker, [default])
