"""Comparação determinística entre snapshots consecutivos."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.findings.opportunity import Opportunity
from julius.state.store import Reconciliation


@dataclass(frozen=True)
class DiffEvent:
    event_type: str
    fingerprint: str
    account: str
    asset_name: str
    rule_id: str
    previous_value: float | None = None
    current_value: float | None = None
    details: dict[str, object] = field(default_factory=dict)


def compare(
    previous: list[dict],
    current: list[Opportunity],
    reconciliation: Reconciliation | None = None,
) -> list[DiffEvent]:
    previous_by_fp = {row["fingerprint"]: row for row in previous}
    current_by_fp = {item.fingerprint(): item for item in current}
    events: list[DiffEvent] = []

    for fingerprint, opportunity in current_by_fp.items():
        old = previous_by_fp.get(fingerprint)
        current_gain = opportunity.estimated_gain.monthly_expected
        if old is None:
            events.append(
                _event("new_opportunity", fingerprint, opportunity, current_value=current_gain)
            )
            continue

        previous_gain = float(old.get("monthly_expected") or 0.0)
        if previous_gain > 0 and current_gain > previous_gain * 1.2:
            events.append(
                _event(
                    "worsened",
                    fingerprint,
                    opportunity,
                    previous_value=previous_gain,
                    current_value=current_gain,
                    details={
                        "increase_pct": round(
                            (current_gain / previous_gain - 1.0) * 100, 1
                        )
                    },
                )
            )
        old_signature = old.get("evidence_hash") or ""
        if old_signature and old_signature != opportunity.evidence_signature():
            events.append(
                _event(
                    "new_evidence",
                    fingerprint,
                    opportunity,
                    previous_value=previous_gain,
                    current_value=current_gain,
                )
            )

    for fingerprint, old in previous_by_fp.items():
        if fingerprint in current_by_fp:
            continue
        events.append(
            DiffEvent(
                event_type="disappeared",
                fingerprint=fingerprint,
                account=str(old.get("account") or ""),
                asset_name=str(old.get("asset_name") or ""),
                rule_id=str(old.get("rule_id") or ""),
                previous_value=float(old.get("monthly_expected") or 0.0),
                details={"previous_status": old.get("status") or "detected"},
            )
        )

    if reconciliation is not None:
        reopened = set(reconciliation.reopened)
        for opportunity in current:
            if opportunity.fingerprint() in reopened:
                events.append(
                    _event(
                        "reopened",
                        opportunity.fingerprint(),
                        opportunity,
                        current_value=opportunity.estimated_gain.monthly_expected,
                    )
                )
    return events


def _event(
    event_type: str,
    fingerprint: str,
    opportunity: Opportunity,
    *,
    previous_value: float | None = None,
    current_value: float | None = None,
    details: dict[str, object] | None = None,
) -> DiffEvent:
    return DiffEvent(
        event_type=event_type,
        fingerprint=fingerprint,
        account=opportunity.account,
        asset_name=opportunity.asset_name,
        rule_id=opportunity.rule_id,
        previous_value=previous_value,
        current_value=current_value,
        details=details or {},
    )
