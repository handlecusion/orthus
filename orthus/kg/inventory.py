"""K7.3 §4.6 — cross-scope edge inventory 러너 (머지 전 게이트 아티팩트, M4 / T3).

`scripts/kg/cross_scope_inventory.sql`(SoR)을 노드 PG에 돌려, personal 페이지가
company 노드로 갖는 cross-scope backlink/derived_from/supports 엣지를 **절대 카운트
+ rel-type만**으로 집계한다. owner_id/slug/title은 출력하지 않는다(T3). graph 투영이
아니라 PG-side 집계이므로 `ORTHUS_KG_ENABLED`/Neo4j 가용성과 무관하게 동작한다
(`resolve_slug`가 PG라 동형 — kg-k7.3-plan §4.6).

`OWNER=<uuid>` env로 단일 owner 측정(게이트 권위, 단일 분모), 미지정 시 회사-집계
(distinct-owner 카운트만 — cross-owner 존재 oracle 금지). 결과가 literally 0이면
운영자 필수 escalation(§7)이 수용 트리거다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from orthus.audit.logger import audit
from orthus.db import session

# orthus/kg/inventory.py -> parents[0]=orthus/kg, parents[1]=orthus, parents[2]=repo root.
_SQL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "kg" / "cross_scope_inventory.sql"


def _owner_from_env() -> UUID | None:
    raw = os.environ.get("OWNER", "").strip()
    if not raw:
        return None
    return UUID(raw)  # 잘못된 형식이면 ValueError로 즉시 실패(조용한 전체 스캔 방지)


def run_inventory(owner: UUID | None) -> list[dict[str, object]]:
    sql = _SQL_PATH.read_text(encoding="utf-8")
    with audit("kg.cross_scope_inventory") as span:
        with session() as s:
            rows = s.execute(text(sql), {"owner": str(owner) if owner else None}).all()
        result = [
            {
                "rel": r._mapping["rel"],
                "owners_with_edge": int(r._mapping["owners_with_edge"]),
                "personal_pages_with_edge": int(r._mapping["personal_pages_with_edge"]),
                "edges": int(r._mapping["edges"]),
            }
            for r in rows
        ]
        total_edges = sum(int(x["edges"]) for x in result)
        span.add_meta(
            owner_scoped=owner is not None,
            rel_types=len(result),
            total_edges=total_edges,
        )
        return result


def main() -> int:
    try:
        owner = _owner_from_env()
    except ValueError:
        print("invalid OWNER (uuid 형식 필요)", file=sys.stderr)
        return 2

    # T3: owner_id를 stdout/증거에 노출하지 않는다 — single-owner 측정도 식별자 없이
    # "그 owner 한 명만" 사실만 표기(audit meta도 owner_scoped bool뿐).
    scope_label = (
        "owner-scoped (single owner, id elided)"
        if owner
        else "company-aggregate (distinct-owner counts only)"
    )
    print(f"[kg cross-scope inventory] scope: {scope_label}")
    print("rel             owners  personal_pages  edges")
    print("-" * 48)
    result = run_inventory(owner)
    total = 0
    for r in result:
        total += int(r["edges"])
        print(
            f"{str(r['rel']):<14}  {int(r['owners_with_edge']):>6}  "
            f"{int(r['personal_pages_with_edge']):>14}  {int(r['edges']):>5}"
        )
    print("-" * 48)
    print(f"total cross-scope edges: {total}")
    if total == 0:
        print(
            "\n→ 0건: 패널 헤드라인은 'BACKLINK-real / coming' framing으로 떨어진다.\n"
            "  운영자 필수 escalation(§7: personal-entity vs P7.3 시퀀싱)이 수용 트리거다.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
