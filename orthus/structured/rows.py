"""Generic structured row helpers."""

from __future__ import annotations

import uuid
from uuid import UUID

STRUCTURED_ROW_NS = uuid.UUID("6e0a5d3c-2b4f-4c1a-9e7d-8f1a2b3c4d6f")


def structured_row_uuid(
    source: str, record_type: str, source_doc_id: UUID, record_key: str
) -> UUID:
    return uuid.uuid5(STRUCTURED_ROW_NS, f"{source}:{record_type}:{source_doc_id}:{record_key}")
