"""Transporte ativo via Amazon SES v2 usando MIME Raw."""

from __future__ import annotations

from julius.notification.mime import build_mime
from julius.notification.models import EmailMessage, SendResult


class SesTransport:
    name = "ses"

    def __init__(
        self,
        client=None,
        *,
        client_factory=None,
        configuration_set: str = "",
    ) -> None:
        if client is None and client_factory is None:
            raise ValueError("client ou client_factory é obrigatório")
        self.client = client
        self.client_factory = client_factory
        self.configuration_set = configuration_set

    def send(self, message: EmailMessage) -> SendResult:
        client = self.client or self.client_factory()
        destination = {"ToAddresses": message.recipients}
        if message.cc:
            destination["CcAddresses"] = message.cc
        kwargs = {
            "FromEmailAddress": message.sender,
            "Destination": destination,
            "Content": {"Raw": {"Data": build_mime(message).as_bytes()}},
            "EmailTags": [
                {"Name": key, "Value": value}
                for key, value in message.tags.items()
            ],
        }
        if self.configuration_set:
            kwargs["ConfigurationSetName"] = self.configuration_set
        response = client.send_email(**kwargs)
        return SendResult(
            status="sent",
            transport=self.name,
            provider_message_id=response.get("MessageId"),
        )
