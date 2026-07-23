"""Interface de transporte de e-mail."""

from __future__ import annotations

from typing import Protocol

from julius.notification.models import EmailMessage, SendResult


class EmailTransport(Protocol):
    name: str

    def send(self, message: EmailMessage) -> SendResult: ...
