"""Multi-conta: roda o Julius em várias contas Consumer e agrega o portfólio.

No MVP 1B habilita rodar em 3 contas com histórico compartilhado. A normalização
por percentis (governança) entra no MVP 2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from julius.collection.policy import policy_for_profile
from julius.config import DEFAULT_CONFIG, Config
from julius.knowledge.rules import REGISTRY, families_without_evidence
from julius.pipeline import Analysis, analyze
from julius.state import BacklogStore, HistoryStore


@dataclass
class AccountRollup:
    account: str
    total_cost_monthly: float
    identified_monthly: float
    high_confidence_monthly: float
    realizable_year: float
    opportunities: int
    actionability_rate: float
    coverage_overall: float
    reviewed_at_10: int
    precision_at_10: float | None
    false_positives_at_10: int


@dataclass
class Portfolio:
    analyses: list[Analysis] = field(default_factory=list)
    rollups: list[AccountRollup] = field(default_factory=list)
    source_coverage: dict[str, dict[str, str]] = field(default_factory=dict)
    rule_coverage: dict[str, dict[str, str]] = field(default_factory=dict)
    calibration_report: dict[str, dict] = field(default_factory=dict)

    @property
    def total_identified_monthly(self) -> float:
        return round(sum(r.identified_monthly for r in self.rollups), 2)

    @property
    def total_realizable_year(self) -> float:
        return round(sum(r.realizable_year for r in self.rollups), 2)


def _identified(a: Analysis) -> float:
    return sum(
        o.portfolio_gain.monthly_expected
        for o in a.opportunities
        if o.include_in_portfolio
    )


def _high_conf(a: Analysis) -> float:
    return sum(
        o.portfolio_gain.monthly_expected
        for o in a.opportunities
        if o.include_in_portfolio and o.confidence >= 0.80
    )


def analyze_portfolio(
    inputs: Sequence[str | Path],
    config: Config = DEFAULT_CONFIG,
    *,
    store: BacklogStore | None = None,
    history: HistoryStore | None = None,
    cadence: str | None = None,
) -> Portfolio:
    portfolio = Portfolio()
    for path in inputs:
        a = analyze(path, config, store=store, history=history, cadence=cadence)
        portfolio.analyses.append(a)
        portfolio.source_coverage[a.account.account_id] = {
            item.source: item.status for item in a.account.collection_health
        }
        missing = {family.name for family in families_without_evidence(a.account)}
        policy = policy_for_profile(a.account.scope_profile)
        portfolio.rule_coverage[a.account.account_id] = {
            f"{family.service}:{family.name}": (
                "out_of_scope"
                if not policy.allows(*family.required_capabilities)
                else "missing_evidence"
                if family.name in missing
                else "covered"
            )
            for family in REGISTRY
        }
        portfolio.rollups.append(
            AccountRollup(
                account=a.account.account_id,
                total_cost_monthly=round(a.account.total_monthly_cost, 2),
                identified_monthly=round(_identified(a), 2),
                high_confidence_monthly=round(_high_conf(a), 2),
                realizable_year=round(
                    sum(
                        o.portfolio_gain.realizable_year
                        for o in a.opportunities
                        if o.include_in_portfolio
                    ),
                    2,
                ),
                opportunities=len(a.opportunities),
                actionability_rate=a.kpis.actionability_rate,
                coverage_overall=a.kpis.coverage_overall,
                reviewed_at_10=a.kpis.reviewed_at_10,
                precision_at_10=a.kpis.precision_at_10,
                false_positives_at_10=a.kpis.false_positives_at_10,
            )
        )
        if history is not None:
            for rule_id in {item.rule_id for item in a.opportunities}:
                sample = next(
                    item for item in a.opportunities if item.rule_id == rule_id
                )
                factor = history.calibration_for(rule_id, opportunity=sample)
                if factor is not None:
                    portfolio.calibration_report[rule_id] = {
                        "sample_count": factor.sample_count,
                        "predicted_total": factor.predicted_total,
                        "realized_total": factor.realized_total,
                        "factor": factor.factor,
                        "mean_precision": factor.mean_precision,
                        "factor_low": factor.factor_low,
                        "factor_high": factor.factor_high,
                        "median_error": factor.median_error,
                        "confidence": factor.confidence,
                        "segment": factor.segment,
                        "fallback_level": factor.fallback_level,
                        "automatic_threshold_change": False,
                    }
    # Portfólio ordenado por economia identificada (onde focar primeiro).
    order = {r.account: i for i, r in enumerate(
        sorted(portfolio.rollups, key=lambda r: r.identified_monthly, reverse=True)
    )}
    portfolio.rollups.sort(key=lambda r: order[r.account])
    return portfolio


def discover_inputs(input_dir: str | Path) -> list[Path]:
    return sorted(Path(input_dir).glob("*.json"))
