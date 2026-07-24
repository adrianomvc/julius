"""Modelos financeiros Athena baseados em contrafactual comprovável."""

from __future__ import annotations

from julius.config import Config
from julius.inventory.model import AthenaQuery
from julius.opportunities.base import Estimation

_TB = 1024**4


def _cost(bytes_scanned: int, pricing) -> float:
    return (bytes_scanned / _TB) * pricing.athena_per_tb


def _baseline(query: AthenaQuery, pricing) -> tuple[float, str, str]:
    if query.allocated_cost is not None and query.cost_quality == "reconciled":
        return (
            query.allocated_cost,
            "custo líquido alocado e reconciliado",
            "allocated",
        )
    # Compatibilidade com inventários anteriores à coleta mensal integrada.
    if not query.structural_fingerprint:
        return (
            _cost(query.monthly_bytes_scanned, pricing),
            "preço de tabela do inventário legado",
            "modeled",
        )
    return 0.0, "economia indisponível: custo Athena não reconciliado", "unavailable"


def partition_pruning_saving(query: AthenaQuery, config: Config) -> Estimation:
    """Não presume seletividade sem conhecer as partições do SQL proposto."""
    pricing = config.pricing
    baseline, source, baseline_quality = _baseline(query, pricing)
    return Estimation(
        method="athena_partition_pruning_counterfactual_required_v2",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline, 2),
        estimated_saving=0,
        assumptions=[
            f"{query.executions_per_month} execuções/mês",
            "filtro ou conjunto de partições recomendado ainda não medido",
            "SAVE será calculado após EXPLAIN/benchmark ou tamanho das partições selecionadas",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=baseline_quality,
        saving_quality="unavailable",
        baseline_bytes=query.billed_bytes or None,
    )


def result_reuse_saving(query: AthenaQuery, config: Config) -> Estimation:
    """Query result reuse evita reprocessar resultados idênticos em execuções repetidas."""
    pricing = config.pricing
    baseline, source, baseline_quality = _baseline(query, pricing)
    if (
        query.structural_fingerprint
        and query.cost_quality == "reconciled"
        and query.reuse_avoidable_cost is not None
    ):
        quality = "measured"
        saving = (
            query.reuse_avoidable_cost
        )
    else:
        quality = "unavailable"
        saving = 0.0
    return Estimation(
        method="athena_result_reuse_exact_duplicates_v2",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{query.reuse_eligible_runs} repetições exatas elegíveis",
            "janela conservadora de 60 minutos",
            "a validade funcional do cache deve acompanhar a atualização da origem",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=baseline_quality,
        saving_quality=quality,
        baseline_bytes=query.billed_bytes or None,
        projected_bytes=(
            max(0, query.billed_bytes - query.reuse_avoidable_billed_bytes)
            if quality == "measured" else None
        ),
        avoidable_bytes=(
            query.reuse_avoidable_billed_bytes if quality == "measured" else None
        ),
    )


def projection_saving(query: AthenaQuery, config: Config) -> Estimation:
    """Não presume tamanho igual entre colunas de Parquet/ORC."""
    pricing = config.pricing
    baseline, source, baseline_quality = _baseline(query, pricing)
    return Estimation(
        method="athena_column_projection_counterfactual_required_v2",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline, 2),
        estimated_saving=0,
        assumptions=[
            f"{query.executions_per_month} execuções/mês",
            "colunas necessárias e bytes comprimidos por coluna ainda não medidos",
            "SAVE será calculado por metadados Parquet/ORC ou benchmark controlado",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=baseline_quality,
        saving_quality="unavailable",
        baseline_bytes=query.billed_bytes or None,
    )
