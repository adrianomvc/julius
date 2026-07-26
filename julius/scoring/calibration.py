"""Aplica fatores de calibração somente após amostra mínima validada."""

from __future__ import annotations

from typing import TYPE_CHECKING

from julius.config import Config
from julius.findings.opportunity import Opportunity
from julius.scoring import impact
from julius.scoring import priority as prioritizer

if TYPE_CHECKING:
    from julius.state.history import HistoryStore


def apply_calibrations(
    opportunities: list[Opportunity],
    history: HistoryStore,
    config: Config,
    *,
    minimum_samples: int = 3,
) -> None:
    for opportunity in opportunities:
        calibration = history.calibration_for(
            opportunity.rule_id, minimum_samples=minimum_samples
        )
        if calibration is None or opportunity.estimated_gain.is_strategic:
            continue
        _apply(opportunity, calibration.factor, calibration.sample_count, config)


def _apply(
    opportunity: Opportunity,
    factor: float,
    sample_count: int,
    config: Config,
) -> None:
    denominator = opportunity.gain_score * opportunity.confidence
    risk = (
        opportunity.strategic_priority / denominator if denominator > 0 else 0.6
    )
    estimation = opportunity.estimation
    if estimation is not None:
        estimation.estimated_saving = round(estimation.estimated_saving * factor, 2)
        estimation.projected_cost = round(
            max(0.0, estimation.baseline_cost - estimation.estimated_saving), 2
        )
        estimation.assumptions.append(
            f"calibração histórica {factor:.3f} com {sample_count} ganhos validados"
        )
        estimation.estimation_version = f"{estimation.estimation_version}+cal"

    gain = opportunity.estimated_gain
    gain.monthly_low = round(gain.monthly_low * factor, 2)
    gain.monthly_expected = round(gain.monthly_expected * factor, 2)
    gain.monthly_high = round(gain.monthly_high * factor, 2)
    gain.annual_potential = round(gain.annual_potential * factor, 2)
    gain.realizable_year = round(gain.realizable_year * factor, 2)
    opportunity.calibration_factor = factor
    opportunity.gain_score = impact.gain_score(
        gain.monthly_expected, config, is_strategic=False
    )
    prioritizer.assign(opportunity, risk=max(0.0, min(1.2, risk)))
