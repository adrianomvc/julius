"""MIME multipart usado pelo envio SMTP local."""

from __future__ import annotations

from email.message import EmailMessage as MimeMessage

from julius.reporting.delivery.models import EmailMessage


def build_mime(message: EmailMessage) -> MimeMessage:
    mime = MimeMessage()
    mime["Subject"] = message.subject
    mime["From"] = message.sender
    mime["To"] = ", ".join(message.recipients)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    mime.set_content(message.text_body)
    mime.add_alternative(message.html_body, subtype="html")
    for name, content in message.attachments:
        mime.add_attachment(
            content,
            maintype="text",
            subtype="html",
            filename=name,
        )
    return mime
