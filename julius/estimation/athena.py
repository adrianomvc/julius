"""Modelos financeiros para Athena (partição e projeção de colunas)."""

from __future__ import annotations

from julius.config import Config
from julius.inventory.model import AthenaQuery
from julius.opportunities.base import Estimation

_TB = 1024**4


def _cost(bytes_scanned: int, pricing) -> float:
    return (bytes_scanned / _TB) * pricing.athena_per_tb


def _baseline(query: AthenaQuery, pricing) -> tuple[float, str]:
    if query.allocated_cost is not None and query.cost_quality == "reconciled":
        return query.allocated_cost, "custo líquido alocado e reconciliado"
    # Compatibilidade com inventários anteriores à coleta mensal integrada.
    if not query.structural_fingerprint:
        return _cost(query.monthly_bytes_scanned, pricing), "preço de tabela do inventário legado"
    return 0.0, "economia indisponível: custo Athena não reconciliado"


def partition_pruning_saving(query: AthenaQuery, config: Config, reduction: float = 0.7) -> Estimation:
    """Filtro de partição elimina scan de partições irrelevantes."""
    pricing = config.pricing
    baseline, source = _baseline(query, pricing)
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
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def result_reuse_saving(query: AthenaQuery, config: Config) -> Estimation:
    """Query result reuse evita reprocessar resultados idênticos em execuções repetidas."""
    pricing = config.pricing
    baseline, source = _baseline(query, pricing)
    if query.structural_fingerprint:
        saving = (
            query.reuse_avoidable_cost
            if query.cost_quality == "reconciled"
            and query.reuse_avoidable_cost is not None
            else 0.0
        )
    else:
        # Inventário legado não contém execuções exatas; mantém o comportamento
        # histórico sem contaminar a coleta nova.
        saving = baseline * 0.2
    return Estimation(
        method="athena_result_reuse_exact_duplicates_v2",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{query.reuse_eligible_runs} repetições exatas elegíveis"
            if query.structural_fingerprint
            else f"{query.executions_per_month} execuções/mês no inventário legado",
            "janela conservadora de 60 minutos",
            "a validade funcional do cache deve acompanhar a atualização da origem",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def projection_saving(query: AthenaQuery, config: Config, reduction: float = 0.35) -> Estimation:
    """Projetar colunas (evitar SELECT *) + result reuse reduzem bytes lidos."""
    pricing = config.pricing
    baseline, source = _baseline(query, pricing)
    saving = baseline * reduction
    return Estimation(
        method="athena_projection_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{query.executions_per_month} execuções/mês",
            "formato colunar lê só as colunas projetadas (~35% menos)",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
