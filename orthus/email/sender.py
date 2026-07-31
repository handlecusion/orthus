"""Deterministic email sender boundary.

P3.6a deliberately ships only a fake sender. It never opens an SMTP/provider
connection and receives only hashes, not raw recipient/subject/body content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class SendRequest:
    work_id: UUID
    node_id: str
    owner_id: UUID
    recipient_hash: str
    subject_hash: str
    body_hash: str


@dataclass(frozen=True)
class SendResult:
    sender_kind: str
    status: Literal["sent", "failed"]
    error_message: str | None = None


class EmailSender(Protocol):
    sender_kind: str

    def send(self, request: SendRequest) -> SendResult:
        """Send using an implementation-specific backend."""


class FakeEmailSender:
    sender_kind = "fake"

    def send(self, request: SendRequest) -> SendResult:
        return SendResult(sender_kind=self.sender_kind, status="sent")


def get_email_sender(kind: str) -> EmailSender | None:
    if kind == "none":
        return None
    if kind == "fake":
        return FakeEmailSender()
    raise ValueError(f"unsupported email sender: {kind}")
