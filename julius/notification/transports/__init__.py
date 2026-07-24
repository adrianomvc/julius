"""Transportes de e-mail; dry-run é sempre o padrão seguro."""

from julius.notification.transports.base import EmailTransport
from julius.notification.transports.dry_run import DryRunTransport
from julius.notification.transports.smtp import SmtpTransport

__all__ = ["DryRunTransport", "EmailTransport", "SmtpTransport"]
