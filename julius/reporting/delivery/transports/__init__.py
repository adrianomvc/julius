"""Transportes de e-mail; dry-run é sempre o padrão seguro."""

from julius.reporting.delivery.transports.base import EmailTransport
from julius.reporting.delivery.transports.dry_run import DryRunTransport
from julius.reporting.delivery.transports.smtp import SmtpTransport

__all__ = ["DryRunTransport", "EmailTransport", "SmtpTransport"]
