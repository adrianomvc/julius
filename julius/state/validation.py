"""Validação de benefício previsto versus realizado, normalizada por volume."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from julius.metrics.unit_economics import calculate
from julius.opportunities.base import Opportunity


@dataclass(frozen=True)
class ValidationResult:
    fingerprint: str
    account: str
    opportunity_id: str
    rule_id: str
    validated_at: datetime
    predicted_monthly: float
    realized_monthly: float
    absolute_saving: float
    baseline_cost: float
    after_cost: float
    baseline_volume: float | None
    after_volume: float | None
    baseline_cost_per_unit: float | None
    after_cost_per_unit: float | None
    normalized_saving: float | None
    estimation_precision: float
    realization_rate: float | None
    performance_change_pct: float | None
    failure_rate_change_pct: float | None
    actor: str
    notes: str


def validate_benefit(
    opportunity: Opportunity,
    *,
    baseline_cost: float,
    after_cost: float,
    actor: str,
    baseline_volume: float | None = None,
    after_volume: float | None = None,
    baseline_performance: float | None = None,
    after_performance: float | None = None,
    baseline_failure_rate: float | None = None,
    after_failure_rate: float | None = None,
    notes: str = "",
    validated_at: datetime | None = None,
) -> ValidationResult:
    if baseline_cost < 0 or after_cost < 0:
        raise ValueError("Custos não podem ser negativos.")
    if not actor.strip():
        raise ValueError("A validação exige um ator.")
    if (baseline_volume is None) != (after_volume is None):
        raise ValueError("Informe os dois volumes ou nenhum.")
    if baseline_volume is not None and (baseline_volume <= 0 or after_volume <= 0):
        raise ValueError("Volumes devem ser maiores que zero.")

    absolute_saving = baseline_cost - after_cost
    before_unit = after_unit = normalized_saving = None
    realized = absolute_saving
    if baseline_volume is not None and after_volume is not None:
        unit = calculate(
            baseline_cost=baseline_cost,
            after_cost=after_cost,
            baseline_volume=baseline_volume,
            after_volume=after_volume,
        )
        before_unit = unit.baseline_cost_per_unit
        after_unit = unit.after_cost_per_unit
        normalized_saving = unit.normalized_saving
        realized = normalized_saving

    predicted = opportunity.estimated_gain.monthly_expected
    precision = _precision(predicted, realized)
    realization_rate = round(realized / predicted, 4) if predicted > 0 else None
    return ValidationResult(
        fingerprint=opportunity.fingerprint(),
        account=opportunity.account,
        opportunity_id=opportunity.opportunity_id,
        rule_id=opportunity.rule_id,
        validated_at=validated_at or datetime.now(timezone.utc),
        predicted_monthly=round(predicted, 2),
        realized_monthly=round(realized, 2),
        absolute_saving=round(absolute_saving, 2),
        baseline_cost=round(baseline_cost, 2),
        after_cost=round(after_cost, 2),
        baseline_volume=baseline_volume,
        after_volume=after_volume,
        baseline_cost_per_unit=_round_optional(before_unit),
        after_cost_per_unit=_round_optional(after_unit),
        normalized_saving=_round_optional(normalized_saving),
        estimation_precision=precision,
        realization_rate=realization_rate,
        performance_change_pct=_change(baseline_performance, after_performance),
        failure_rate_change_pct=_change(
            baseline_failure_rate, after_failure_rate
        ),
        actor=actor.strip(),
        notes=notes,
    )


def _precision(predicted: float, realized: float) -> float:
    if predicted <= 0:
        return 0.0
    error = abs(realized - predicted) / predicted
    return round(max(0.0, 1.0 - error), 4)


def _change(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return round((after / before - 1.0) * 100, 2)


def _round_optional(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None
