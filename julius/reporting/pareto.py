"""Método 80/20 em dois cortes: financeiro e executável."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.findings.opportunity import Opportunity
from julius.scoring.priority import ranking_key

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
    quant = [o for o in opportunities if o.include_in_portfolio]
    monthly_total = sum(_monthly(o) for o in quant)
    p = Pareto(monthly_total=round(monthly_total, 2))
    if monthly_total <= 0:
        return p

    # Corte financeiro: mínimo de ações que somam ~80% da economia. Aqui a
    # ordem é por valor porque a pergunta é essa — quantas ações somam 80% —, e
    # respondê-la exige as maiores primeiro. É também a ordem da barra do
    # relatório, que num Pareto é decrescente por definição.
    for o in sorted(quant, key=_monthly, reverse=True):
        p.financial_focus.append(o)
        p.financial_sum += _monthly(o)
        if p.financial_sum / monthly_total >= _FINANCIAL_TARGET:
            break
    p.financial_pct = round(p.financial_sum / monthly_total * 100)
    p.financial_sum = round(p.financial_sum, 2)

    # Corte executável: subconjunto sem bloqueadores, implementável neste mês.
    # Esta é uma lista de implantação, não um corte financeiro, então a ordem é
    # a mesma da tabela: valor × confiança × urgência ÷ dificuldade. Ordenar por
    # valor puro colocaria a ação cara e difícil antes da barata e imediata.
    executable = [o for o in quant if o.actionable and o.bucket == "fazer_agora"]
    p.executable_focus = sorted(executable, key=ranking_key, reverse=True)
    p.executable_sum = round(sum(_monthly(o) for o in p.executable_focus), 2)
    p.executable_pct = round(p.executable_sum / monthly_total * 100)
    return p
