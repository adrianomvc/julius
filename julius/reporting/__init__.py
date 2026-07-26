"""Relatório: como o resultado do scan chega a uma pessoa.

Reúne o que antes eram três pacotes com o mesmo propósito — renderizar
(`report/`), entregar (`notification/`) e medir o produto (`metrics/`). São
etapas de uma coisa só: transformar achados em algo que alguém lê e decide.

`delivery/` é a saída ativa (e-mail, outbox) e mantém os próprios guardrails de
envio. `view_models` é onde toda regra de apresentação mora — o template não
decide nada.
"""

from julius.reporting.kpis import ProductKPIs, compute_kpis

__all__ = ["ProductKPIs", "compute_kpis"]
