"""Método 80/20 em dois cortes: financeiro e executável."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.findings.opportunity import Opportunity

_FINANCIAL_TARGET = 0.80


@dataclass
class Pareto:
    monthly_total: float = 0.0
    financial_focus: list[Opportunity] = field(default_factory=list)
    financial_pct: int = 0
    financial_sum: float = 0.0
    executable_focus: list[Opportunity] = field(default_factory=list)
    executable_pct: int = 0
    executable_sum: float = 0.0

    @property
    def sentence(self) -> str:
        return (
            f"{len(self.financial_focus)} ações capturam {self.financial_pct}% da economia mensal "
            f"(US$ {self.financial_sum:,.2f} de US$ {self.monthly_total:,.2f}). "
            f"Destas, {len(self.executable_focus)} são implementáveis já ({self.executable_pct}%)."
        )


def _monthly(o: Opportunity) -> float:
    return o.portfolio_gain.monthly_expected


def compute(opportunities: list[Opportunity]) -> Pareto:
    quant = [o for o in opportunities if not o.estimated_gain.is_strategic and _monthly(o) > 0]
    monthly_total = sum(_monthly(o) for o in quant)
    p = Pareto(monthly_total=round(monthly_total, 2))
    if monthly_total <= 0:
        return p

    # Corte financeiro: mínimo de ações que somam ~80% da economia.
    for o in sorted(quant, key=_monthly, reverse=True):
        p.financial_focus.append(o)
        p.financial_sum += _monthly(o)
        if p.financial_sum / monthly_total >= _FINANCIAL_TARGET:
            break
    p.financial_pct = round(p.financial_sum / monthly_total * 100)
    p.financial_sum = round(p.financial_sum, 2)

    # Corte executável: subconjunto sem bloqueadores, implementável neste mês.
    executable = [o for o in quant if o.actionable and o.bucket == "fazer_agora"]
    p.executable_focus = sorted(executable, key=_monthly, reverse=True)
    p.executable_sum = round(sum(_monthly(o) for o in p.executable_focus), 2)
    p.executable_pct = round(p.executable_sum / monthly_total * 100)
    return p
