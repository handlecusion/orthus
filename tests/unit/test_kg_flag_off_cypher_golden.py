"""K7.2 Step 1.5 — flag-off Cypher byte-identity (fast-rollback 전제).

`_kg_flag_off_cypher_golden.json`은 owner-variant 코드가 들어오기 **전**(feat/k7-1)에
체크인된 v1 `cypher_for()` 출력 스냅샷이다. owner-variant를 추가한 뒤에도
`ORTHUS_KG_OWNER_SCOPE_ENABLED`가 off면 `cypher_for()`가 이 golden과 **byte-identical**
이어야 한다(= flag만 끄면 v1 동작 복원 = rollback 전제). 부분집합/멤버십 비교가
아니라 정확 일치다 — owner-variant가 섞여 들어와도 green-by-vacuity가 되지 않게.
"""

from __future__ import annotations

import json
from pathlib import Path

from orthus.kg.templates import (
    TEMPLATES,
    ConflictsOfParams,
    EntityMentionsParams,
    NeighborsParams,
    PageConflictsParams,
    PathBetweenParams,
    ProvenanceChainParams,
)

_GOLDEN = Path(__file__).parent / "_kg_flag_off_cypher_golden.json"


def _flag_off_map() -> dict[str, dict[str, str]]:
    combos = {
        "neighbors": [NeighborsParams(slug="s", depth=1), NeighborsParams(slug="s", depth=2)],
        "path_between": [PathBetweenParams(slug_a="a", slug_b="b", max_hops=h) for h in (2, 3, 4)],
        "conflicts_of": [ConflictsOfParams(slug="s")],
        "page_conflicts": [PageConflictsParams(slug="s")],
        "provenance_chain": [ProvenanceChainParams(slug="s")],
        "entity_mentions": [EntityMentionsParams(name_norm="n")],
    }
    out: dict[str, dict[str, str]] = {}
    for name, params_list in combos.items():
        tmpl = TEMPLATES[name]
        entries: dict[str, str] = {}
        for p in params_list:
            if name == "neighbors":
                key = f"depth={p.depth}"
            elif name == "path_between":
                key = f"max_hops={p.max_hops}"
            else:
                key = "default"
            entries[key] = tmpl.cypher_for(p)
        out[name] = entries
    return out


def test_k7_flag_off_results_unchanged(monkeypatch):
    # owner-scope flag explicitly OFF — cypher_for must equal the pre-owner-variant golden.
    monkeypatch.setenv("ORTHUS_KG_OWNER_SCOPE_ENABLED", "false")
    golden = json.loads(_GOLDEN.read_text())
    assert _flag_off_map() == golden
