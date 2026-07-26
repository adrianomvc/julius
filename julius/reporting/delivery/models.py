"""Modelos de mensagem e resultado de envio."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EmailMessage:
    subject: str
    sender: str
    recipients: list[str]
    text_body: str
    html_body: str
    cc: list[str] = field(default_factory=list)
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    idempotency_key: str = ""


@dataclass
class SendResult:
    status: str  # composed_not_sent | sent | blocked
    transport: str
    provider_message_id: str | None = None
    outbox_dir: str | None = None
    reason: str | None = None
