"""KG (K-series) — central 전용 rebuildable Neo4j 관계 인덱스.

Postgres + wiki-store가 SoR이고 Neo4j는 파생 인덱스다(docs/kg-model.md).
공개 표면은 re-export만 — 무거운 import(neo4j driver)는 client.py 함수
내부에서만 일어난다(docs/kg-implementation-spec.md §2.1 lazy import 계약).
"""

from orthus.kg.client import kg_available, kg_enabled

__all__ = ["kg_available", "kg_enabled"]
