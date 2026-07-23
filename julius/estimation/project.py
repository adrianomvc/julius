"""Projeção temporal do ganho (potencial × realizável)."""

from __future__ import annotations

from datetime import date

from julius.config import IMPL_OFFSET_BY_DIFFICULTY, Config
from julius.opportunities.base import EstimatedGain


def months_remaining_in_year(today: date | None = None) -> int:
    today = today or date.today()
    return max(0, 12 - today.month)


def build_gain(
    monthly_expected: float,
    difficulty: int,
    config: Config,
    *,
    band: float = 0.35,
    today: date | None = None,
    is_strategic: bool = False,
) -> EstimatedGain:
    """Monta a projeção de ganho a partir da economia mensal esperada.

    - faixa provável: expected × (1 ± band)
    - potencial anual: expected × 12 (referência teórica)
    - realizável no ano: expected × (meses_restantes − offset_impl) × fator
    """
    today = today or date.today()
    if is_strategic or monthly_expected <= 0:
        return EstimatedGain(is_strategic=True, realization_factor=config.realization_factor)

    offset = IMPL_OFFSET_BY_DIFFICULTY.get(difficulty, 1)
    remaining = months_remaining_in_year(today)
    effective_months = max(0, remaining - offset)
    realizable = monthly_expected * effective_months * config.realization_factor

    impl_month = min(12, today.month + offset)
    impl_date = f"{today.year}-{impl_month:02d}"

    return EstimatedGain(
        monthly_low=round(monthly_expected * (1 - band), 2),
        monthly_expected=round(monthly_expected, 2),
        monthly_high=round(monthly_expected * (1 + band), 2),
        annual_potential=round(monthly_expected * 12, 2),
        likely_implementation_date=impl_date,
        realization_factor=config.realization_factor,
        realizable_year=round(realizable, 2),
        is_strategic=False,
    )
