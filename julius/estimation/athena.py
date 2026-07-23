"""Modelos financeiros para Athena (partição e projeção de colunas)."""

from __future__ import annotations

from julius.config import Config
from julius.inventory.model import AthenaQuery
from julius.opportunities.base import Estimation

_TB = 1024**4


def _cost(bytes_scanned: int, pricing) -> float:
    return (bytes_scanned / _TB) * pricing.athena_per_tb


def partition_pruning_saving(query: AthenaQuery, config: Config, reduction: float = 0.7) -> Estimation:
    """Filtro de partição elimina scan de partições irrelevantes."""
    pricing = config.pricing
    baseline = _cost(query.monthly_bytes_scanned, pricing)
    saving = baseline * reduction
    return Estimation(
        method="athena_partition_pruning_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{query.executions_per_month} execuções/mês",
            "tabela particionada; filtro elimina ~70% do scan",
            "mesmo período consultado",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def result_reuse_saving(query: AthenaQuery, config: Config, reduction: float = 0.2) -> Estimation:
    """Query result reuse evita reprocessar resultados idênticos em execuções repetidas."""
    pricing = config.pricing
    baseline = _cost(query.monthly_bytes_scanned, pricing)
    saving = baseline * reduction
    return Estimation(
        method="athena_result_reuse_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{query.executions_per_month} execuções/mês da mesma query",
            "result reuse serve resultados em cache (~20% evitável)",
            "dados de origem estáveis dentro da janela de reuso",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def projection_saving(query: AthenaQuery, config: Config, reduction: float = 0.35) -> Estimation:
    """Projetar colunas (evitar SELECT *) + result reuse reduzem bytes lidos."""
    pricing = config.pricing
    baseline = _cost(query.monthly_bytes_scanned, pricing)
    saving = baseline * reduction
    return Estimation(
        method="athena_projection_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{query.executions_per_month} execuções/mês",
            "formato colunar lê só as colunas projetadas (~35% menos)",
            "result reuse evita reprocessar resultados idênticos",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
