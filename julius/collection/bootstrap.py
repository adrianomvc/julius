"""Profundidade da janela na primeira coleta de uma conta.

Várias regras só produzem cifra com maturidade: três coletas consistentes, ou 90
dias de cobertura de leitura. Com a janela fixa de 30 dias, uma conta nova espera
de um a três meses de coletas semanais para o portfólio ter número — mesmo quando
a AWS já retém o histórico necessário hoje. A primeira coleta pede uma janela
profunda para não gastar esse tempo.

O checkpoint é o próprio `--output` anterior, como em `sagemaker_history`. A
camada de coleta **não** consulta o histórico em DuckDB de propósito: ela não
conhece a camada de estado, e essa seta aponta só para baixo.

O teto de retenção de cada família é aplicado depois, por fonte, em
`sources.run` — pedir 90 dias não significa que Athena devolva 90.
"""

from __future__ import annotations

from pathlib import Path

from julius.collection.settings import BOOTSTRAP_WINDOW_DAYS


def resolve_depth(
    *,
    lookback_days: int,
    cadence: str,
    checkpoint: str | Path,
    explicit: bool | None = None,
) -> tuple[int, bool]:
    """Dias da janela e se esta coleta é um bootstrap.

    `explicit` vem de `--bootstrap/--no-bootstrap`; `None` decide pela ausência
    do checkpoint. Cadência mensal nunca é bootstrap: mês-calendário é
    fechamento financeiro de um período específico, não janela móvel.

    A profundidade é o **maior** entre o pedido e a do bootstrap, não uma
    substituição: um `--lookback-days` explícito acima dela continua valendo.
    """
    if cadence == "monthly":
        return lookback_days, False
    primeira = not Path(checkpoint).is_file()
    bootstrap = primeira if explicit is None else explicit
    if not bootstrap:
        return lookback_days, False
    return max(lookback_days, BOOTSTRAP_WINDOW_DAYS), True
