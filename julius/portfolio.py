"""Multi-conta: roda o Julius em várias contas Consumer e agrega o portfólio.

No MVP 1B habilita rodar em 3 contas com histórico compartilhado. A normalização
por percentis (governança) entra no MVP 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from julius.config import DEFAULT_CONFIG, Config
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

    @property
    def total_identified_monthly(self) -> float:
        return round(sum(r.identified_monthly for r in self.rollups), 2)

    @property
    def total_realizable_year(self) -> float:
        return round(sum(r.realizable_year for r in self.rollups), 2)


def _identified(a: Analysis) -> float:
    return sum(
        o.estimated_gain.monthly_expected
        for o in a.opportunities
        if not o.estimated_gain.is_strategic
    )


def _high_conf(a: Analysis) -> float:
    return sum(
        o.estimated_gain.monthly_expected
        for o in a.opportunities
        if not o.estimated_gain.is_strategic and o.confidence >= 0.80
    )


def analyze_portfolio(
    inputs: list[str | Path],
    config: Config = DEFAULT_CONFIG,
    *,
    store: BacklogStore | None = None,
    history: HistoryStore | None = None,
) -> Portfolio:
    portfolio = Portfolio()
    for path in inputs:
        a = analyze(path, config, store=store, history=history)
        portfolio.analyses.append(a)
        portfolio.rollups.append(
            AccountRollup(
                account=a.account.account_id,
                total_cost_monthly=round(a.account.total_monthly_cost, 2),
                identified_monthly=round(_identified(a), 2),
                high_confidence_monthly=round(_high_conf(a), 2),
                realizable_year=round(
                    sum(o.estimated_gain.realizable_year for o in a.opportunities), 2
                ),
                opportunities=len(a.opportunities),
                actionability_rate=a.kpis.actionability_rate,
                coverage_overall=a.kpis.coverage_overall,
                reviewed_at_10=a.kpis.reviewed_at_10,
                precision_at_10=a.kpis.precision_at_10,
                false_positives_at_10=a.kpis.false_positives_at_10,
            )
        )
    # Portfólio ordenado por economia identificada (onde focar primeiro).
    order = {r.account: i for i, r in enumerate(
        sorted(portfolio.rollups, key=lambda r: r.identified_monthly, reverse=True)
    )}
    portfolio.rollups.sort(key=lambda r: order[r.account])
    return portfolio


def discover_inputs(input_dir: str | Path) -> list[Path]:
    return sorted(Path(input_dir).glob("*.json"))
