"""Aplica fatores de calibração somente após amostra mínima validada."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from julius.config import Config
from julius.findings.opportunity import CalibrationMetadata, Opportunity
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
            opportunity.rule_id,
            minimum_samples=minimum_samples,
            opportunity=opportunity,
        )
        if calibration is None or not opportunity.include_in_portfolio:
            continue
        _apply(opportunity, calibration, config)


def _apply(
    opportunity: Opportunity,
    calibration,
    config: Config,
) -> None:
    denominator = opportunity.gain_score * opportunity.confidence
    risk = (
        opportunity.strategic_priority / denominator if denominator > 0 else 0.6
    )
    factor = calibration.factor
    technical = opportunity.estimated_gain
    maximum = (
        opportunity.estimation.baseline_cost
        if opportunity.estimation is not None
        and opportunity.estimation.baseline_cost > 0
        else float("inf")
    )
    low = max(0.0, technical.monthly_low * calibration.factor_low)
    expected = max(0.0, technical.monthly_expected * factor)
    high = max(expected, technical.monthly_high * calibration.factor_high)
    low, expected, high = (
        min(maximum, value) for value in (low, expected, high)
    )
    opportunity.calibrated_gain = replace(
        technical,
        monthly_low=round(min(low, expected), 2),
        monthly_expected=round(expected, 2),
        monthly_high=round(max(expected, high), 2),
        annual_potential=round(expected * 12, 2),
        realizable_year=round(technical.realizable_year * factor, 2),
    )
    opportunity.calibration = CalibrationMetadata(
        factor_low=calibration.factor_low,
        factor_expected=factor,
        factor_high=calibration.factor_high,
        sample_count=calibration.sample_count,
        median_error=calibration.median_error,
        confidence=calibration.confidence,
        segment=calibration.segment,
        fallback_level=calibration.fallback_level,
    )
    opportunity.calibration_factor = factor
    opportunity.gain_score = impact.gain_score(
        opportunity.portfolio_gain.monthly_expected, config, is_strategic=False
    )
    prioritizer.assign(opportunity, risk=max(0.0, min(1.2, risk)))
