"""Model slot Protocols. Nodes depend on these, never on a concrete vendor SDK
(architecture §2.2). P1 ships Mock; real adapters swap in behind the same Protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingModel(Protocol):
    model_version: str  # e.g. "mock:1024", "solar-embedding-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one 1024-d vector per input text."""
        ...


@runtime_checkable
class ChatModel(Protocol):
    model_id: str

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        """Single-turn completion. `json_only` asks the model for a bare JSON object."""
        ...
