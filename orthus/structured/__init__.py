"""structured(PG) backend — NL→SQL over our own JSONB row store `notion_rows`
(P2.2a, docs/architecture-v2.md §1/§5). Reuses the 비서 validation gate."""

from orthus.structured.query import build_notion_catalog, query_structured, retry_signal

__all__ = ["build_notion_catalog", "query_structured", "retry_signal"]
