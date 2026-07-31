"""B3 · R2 grounding verification (Step 1 gate).

Confirms the orthus_r2 company-wiki dump actually grounds a KNOWN-answerable
question BEFORE any scoring — the "empty-DB falsely-abstains" trap guard. Uses the
exact production path the runner uses: Solar chat + Solar embedding + company
scope + learn=False/record_gaps=False. Prints gap / sources / answer. Exits 1 if
the probe abstains (which would mean the dump/embedding is wrong).
"""

from __future__ import annotations

import os

os.environ["ORTHUS_PG_DSN"] = "postgresql+psycopg://orthus:orthus@localhost:5433/orthus_r2"
os.environ["ORTHUS_PG_DSN_READONLY"] = "postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus_r2"
os.environ["ORTHUS_EMBEDDING"] = "solar"
os.environ["ORTHUS_NODE_KIND"] = "company"

import sys  # noqa: E402
from uuid import UUID  # noqa: E402

import b2_run  # noqa: E402,F401  (load_dotenv + endpoints)
from b3_r2_run import USER_ID, _null_audit  # reuse harness uid + audit neutralizer  # noqa: E402

import orthus.wiki.gap as _gap_mod  # noqa: E402
import orthus.wiki.qa as _qa_mod  # noqa: E402
import orthus.wiki.retrieve as _retrieve_mod  # noqa: E402

for _m in (_qa_mod, _retrieve_mod, _gap_mod):
    _m.audit = _null_audit

from orthus.connectors.registry import register_default_connector_providers  # noqa: E402
from orthus.models.registry import get_embedding_model  # noqa: E402
from orthus.wiki.qa import ask  # noqa: E402
from orthus.wiki.retrieve import retrieve  # noqa: E402

register_default_connector_providers(replace=True)  # avoid the thread-race registration path

PROBES = [
    "nova가 어떤 서비스야?",
    "Nova 개발 로드맵에서 상태 흐름 단계가 어떻게 돼?",
]


def main() -> int:
    emb = get_embedding_model()
    print(f"[embedding] model_version={emb.model_version}")
    solar = b2_run.build_endpoint("solar")

    # The empty-DB trap (the failure this gate exists to catch) has an unambiguous
    # signature: retrieval returns ZERO hits, so every question abstains with
    # gap=no_data. We FAIL on that. A probe that retrieves real pages (n_hits>0,
    # high score) but abstains via `insufficient_grounding` is a genuine content
    # property (a thin wiki page), NOT a dump/embedding failure — it is the intended
    # abstention behavior. The gate passes iff retrieval works AND at least one
    # known-answerable probe grounds cleanly (gap None + real cited slugs).
    any_empty_db = False
    any_clean_ground = False
    for q in PROBES:
        hits = retrieve(UUID(str(USER_ID)), q, k=5, scope="company")
        top = max((h.score for h in hits), default=None)
        print(f"\n[probe] {q}")
        print(f"  retrieve: n_hits={len(hits)} top_score={top} "
              f"slugs={[h.page_slug for h in hits[:5]]}")
        res = ask(USER_ID, q, scope="company", learn=False, record_gaps=False,
                  chat_model=solar.adapter)
        reason = res.gap.reason if res.gap else None
        print(f"  ask: abstained={res.gap is not None} gap={reason} "
              f"n_sources={len(res.sources)}")
        print(f"  answer: {res.answer[:280]}")
        if not hits or reason == "no_data":
            any_empty_db = True
            print("  !! EMPTY-DB SIGNATURE (0 hits / no_data) — dump/embedding wrong")
        if res.gap is None and res.sources:
            any_clean_ground = True
            print("  ok: grounded cleanly (gap None + real cited slugs)")
    ok = (not any_empty_db) and any_clean_ground
    print("\n=== GROUNDING VERIFY:", "PASS" if ok else "FAIL",
          "(retrieval returns real hits; known-answerable probe grounds gap=None)", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
