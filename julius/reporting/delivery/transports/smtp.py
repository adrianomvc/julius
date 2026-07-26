"""Transporte ativo por relay SMTP corporativo."""

from __future__ import annotations

import smtplib
from collections.abc import Callable

from julius.reporting.delivery.mime import build_mime
from julius.reporting.delivery.models import EmailMessage, SendResult


class SmtpTransport:
    name = "smtp"

    def __init__(
        self,
        host: str,
        *,
        port: int = 587,
        username: str = "",
        password: str = "",
        starttls: bool = True,
        timeout: float = 30,
        client_factory: Callable = smtplib.SMTP,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.starttls = starttls
        self.timeout = timeout
        self.client_factory = client_factory

    def send(self, message: EmailMessage) -> SendResult:
        if not self.host:
            return SendResult(
                status="blocked", transport=self.name, reason="smtp_host ausente"
            )
        if self.username and not self.password:
            return SendResult(
                status="blocked",
                transport=self.name,
                reason="senha SMTP ausente no ambiente",
            )
        with self.client_factory(self.host, self.port, timeout=self.timeout) as client:
            if self.starttls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password)
            response = client.send_message(build_mime(message))
        return SendResult(
            status="sent",
            transport=self.name,
            provider_message_id=str(response) if response else None,
        )
