"""Conversão entre a janela medida e a expressão por mês."""

from __future__ import annotations

from julius.config import ANALYSIS_WINDOW_DAYS, DAYS_PER_MONTH


def monthly_factor(window_days: int) -> float:
    """Converte uma medição da janela em número por mês.

    A coleta mede N dias completos; o relatório fala em mês. A conversão é
    explícita e mora só aqui — 30 dias não são um mês.

    O parâmetro é o tamanho da **janela**, nunca a vida do ativo: uma sessão
    que existiu dois dias mede dois dias de consumo, e projetar esses dois dias
    para um mês seria a extrapolação que este contrato existe para eliminar.
    """
    return DAYS_PER_MONTH / max(1, window_days or ANALYSIS_WINDOW_DAYS)
