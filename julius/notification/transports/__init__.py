"""Transportes de e-mail (MVP 1B: somente dry-run)."""

from julius.notification.transports.base import EmailTransport
from julius.notification.transports.dry_run import DryRunTransport

__all__ = ["DryRunTransport", "EmailTransport"]
