"""Máquina de estados auditável das oportunidades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

STATUSES = {
    "detected",
    "reviewed",
    "accepted",
    "planned",
    "implemented",
    "validated",
    "dismissed",
    "expired",
}

_TRANSITIONS = {
    "detected": {"reviewed", "dismissed", "expired"},
    "reviewed": {"accepted", "dismissed", "expired"},
    "accepted": {"planned", "dismissed", "expired"},
    "planned": {"implemented", "dismissed", "expired"},
    "implemented": {"validated", "planned"},
    "validated": {"detected"},
    "dismissed": {"detected"},
    "expired": {"detected"},
}


@dataclass(frozen=True)
class LifecycleEvent:
    fingerprint: str
    account: str
    opportunity_id: str
    from_status: str
    to_status: str
    actor: str
    reason: str
    occurred_at: datetime
    automatic: bool = False


def can_transition(from_status: str, to_status: str) -> bool:
    return (
        from_status in STATUSES
        and to_status in STATUSES
        and to_status in _TRANSITIONS[from_status]
    )


def transition(
    *,
    fingerprint: str,
    account: str,
    opportunity_id: str,
    from_status: str,
    to_status: str,
    actor: str,
    reason: str,
    occurred_at: datetime | None = None,
    automatic: bool = False,
) -> LifecycleEvent:
    if not can_transition(from_status, to_status):
        raise ValueError(f"Transição inválida: {from_status} -> {to_status}")
    if not actor.strip():
        raise ValueError("A transição exige um ator.")
    if not reason.strip():
        raise ValueError("A transição exige uma justificativa.")
    return LifecycleEvent(
        fingerprint=fingerprint,
        account=account,
        opportunity_id=opportunity_id,
        from_status=from_status,
        to_status=to_status,
        actor=actor.strip(),
        reason=reason.strip(),
        occurred_at=occurred_at or datetime.now(timezone.utc),
        automatic=automatic,
    )
