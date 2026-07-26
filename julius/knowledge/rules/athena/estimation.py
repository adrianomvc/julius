"""Modelos financeiros Athena com medição primeiro e fallback versionado."""

from __future__ import annotations

from julius.collection.models import AthenaQuery
from julius.config import (
    ATHENA_RECOVERY_RATES,
    ATHENA_RECOVERY_VERSION,
    Config,
)
from julius.findings.opportunity import Estimation

_TB = 1024**4


def _cost(bytes_scanned: int, pricing) -> float:
    """Preço de tabela em USD; a conversão de moeda já ocorreu na ingestão."""
    return (bytes_scanned / _TB) * pricing.athena_per_tb_usd


def _baseline(query: AthenaQuery, pricing) -> tuple[float, str, str]:
    if query.allocated_cost is not None:
        quality = (
            "allocated"
            if query.cost_quality == "reconciled"
            else "allocated_partial"
        )
        return query.allocated_cost, "custo líquido alocado", quality
    return (
        _cost(query.monthly_bytes_scanned, pricing),
        "preço de tabela aplicado aos bytes faturáveis",
        "modeled",
    )


def modeled_saving(
    query: AthenaQuery,
    config: Config,
    profile: str,
    *,
    evidence: list[str] | None = None,
) -> Estimation:
    """Aplica faixa por regra somente quando não existe contrafactual melhor."""
    pricing = config.pricing
    baseline, source, baseline_quality = _baseline(query, pricing)
    rates = ATHENA_RECOVERY_RATES[profile]
    low = baseline * rates.low
    expected = baseline * rates.expected
    high = baseline * rates.high
    assumptions = [
        f"{query.executions_per_month} execuções/mês",
        (
            f"faixa da regra {profile}: "
            f"{rates.low:.0%}/{rates.expected:.0%}/{rates.high:.0%}"
        ),
        source,
        f"modelo {ATHENA_RECOVERY_VERSION}; substituir por benchmark quando disponível",
    ]
    assumptions.extend(evidence or [])
    return Estimation(
        method=f"athena_{profile}_modeled_rule_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - expected, 2),
        estimated_saving=round(expected, 2),
        estimated_saving_low=round(low, 2),
        estimated_saving_high=round(high, 2),
        assumptions=assumptions,
        pricing_region=pricing.region,
        estimation_version=ATHENA_RECOVERY_VERSION,
        baseline_quality=baseline_quality,
        saving_quality="modeled_rule",
        baseline_bytes=query.billed_bytes or query.monthly_bytes_scanned or None,
        projected_bytes=(
            round((query.billed_bytes or query.monthly_bytes_scanned) * (1 - rates.expected))
            if query.billed_bytes or query.monthly_bytes_scanned
            else None
        ),
        avoidable_bytes=(
            round((query.billed_bytes or query.monthly_bytes_scanned) * rates.expected)
            if query.billed_bytes or query.monthly_bytes_scanned
            else None
        ),
    )


def partition_pruning_saving(
    query: AthenaQuery,
    config: Config,
    profile: str = "missing_partition_filter",
) -> Estimation:
    return modeled_saving(
        query,
        config,
        profile,
        evidence=["seletividade ainda não medida; confiança baixa"],
    )


def projection_saving(
    query: AthenaQuery,
    config: Config,
    profile: str = "select_star",
) -> Estimation:
    return modeled_saving(
        query,
        config,
        profile,
        evidence=["bytes comprimidos por coluna ainda não medidos; confiança baixa"],
    )


def result_reuse_saving(query: AthenaQuery, config: Config) -> Estimation:
    """Usa custo das duplicatas quando reconciliado; caso contrário modela."""
    pricing = config.pricing
    baseline, source, baseline_quality = _baseline(query, pricing)
    if (
        query.structural_fingerprint
        and query.cost_quality == "reconciled"
        and query.reuse_avoidable_cost is not None
    ):
        saving = query.reuse_avoidable_cost
        return Estimation(
            method="athena_result_reuse_exact_duplicates_v2",
            baseline_cost=round(baseline, 2),
            projected_cost=round(baseline - saving, 2),
            estimated_saving=round(saving, 2),
            estimated_saving_low=round(saving, 2),
            estimated_saving_high=round(saving, 2),
            assumptions=[
                f"{query.reuse_eligible_runs} repetições exatas elegíveis",
                "janela conservadora de 60 minutos",
                "validade funcional do cache acompanha a atualização da origem",
                source,
            ],
            pricing_region=pricing.region,
            estimation_version=ATHENA_RECOVERY_VERSION,
            baseline_quality=baseline_quality,
            saving_quality="measured",
            baseline_bytes=query.billed_bytes or None,
            projected_bytes=max(
                0, query.billed_bytes - query.reuse_avoidable_billed_bytes
            ),
            avoidable_bytes=query.reuse_avoidable_billed_bytes,
        )

    if query.reuse_avoidable_billed_bytes and query.billed_bytes:
        ratio = min(1.0, query.reuse_avoidable_billed_bytes / query.billed_bytes)
        saving = baseline * ratio
        return Estimation(
            method="athena_result_reuse_bytes_ratio_v1",
            baseline_cost=round(baseline, 2),
            projected_cost=round(baseline - saving, 2),
            estimated_saving=round(saving, 2),
            estimated_saving_low=round(saving, 2),
            estimated_saving_high=round(saving, 2),
            assumptions=[
                f"{query.reuse_eligible_runs} repetições exatas elegíveis",
                f"{ratio:.1%} dos bytes faturáveis pertencem às duplicatas",
                source,
            ],
            pricing_region=pricing.region,
            estimation_version=ATHENA_RECOVERY_VERSION,
            baseline_quality=baseline_quality,
            saving_quality="modeled_evidence",
            baseline_bytes=query.billed_bytes,
            projected_bytes=max(
                0, query.billed_bytes - query.reuse_avoidable_billed_bytes
            ),
            avoidable_bytes=query.reuse_avoidable_billed_bytes,
        )

    return modeled_saving(query, config, "legacy_result_reuse")
