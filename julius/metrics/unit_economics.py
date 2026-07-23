"""Economia unitária para separar redução de volume de ganho de eficiência."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnitEconomics:
    baseline_cost_per_unit: float
    after_cost_per_unit: float
    normalized_saving: float
    efficiency_change_pct: float


def calculate(
    *,
    baseline_cost: float,
    after_cost: float,
    baseline_volume: float,
    after_volume: float,
) -> UnitEconomics:
    if baseline_volume <= 0 or after_volume <= 0:
        raise ValueError("Volumes devem ser maiores que zero.")
    before_unit = baseline_cost / baseline_volume
    after_unit = after_cost / after_volume
    normalized_saving = (before_unit - after_unit) * after_volume
    efficiency_change = (
        (after_unit / before_unit - 1.0) * 100 if before_unit > 0 else 0.0
    )
    return UnitEconomics(
        baseline_cost_per_unit=round(before_unit, 6),
        after_cost_per_unit=round(after_unit, 6),
        normalized_saving=round(normalized_saving, 2),
        efficiency_change_pct=round(efficiency_change, 2),
    )
