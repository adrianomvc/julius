"""Custo real do Glue pelo Cost Explorer, por usage type, rateado por consumo.

O Cost Explorer não expõe dimensão de recurso para Glue: a cobrança chega
agregada por `USAGE_TYPE`. Este módulo traz essa cobrança real, classifica-a em
buckets versionados e rateia cada bucket entre os ativos já coletados,
proporcionalmente ao consumo medido (DPU-hora, node-hora).

O resultado é **custo alocado**, não fatura por job. Custo real por execução
exigiria tags de alocação ativadas ou CUR (`line_item_resource_id`), ambos com
dependência administrativa fora da coleta read-only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from julius.collection.collectors.billing_matrix import BillingMatrix
from julius.collection.currency import non_usd_gap, usd_amount
from julius.collection.models import Account, GlueCostCoverage
from julius.collection.window import AnalysisWindow

_METRICS = ("NetUnblendedCost", "UnblendedCost")


def classify_usage_type(
    usage_type: str, markers: Sequence[tuple[str, str]]
) -> str:
    """Traduz um `USAGE_TYPE` do Cost Explorer em bucket de cobrança.

    A tabela de marcadores é conhecimento de domínio e chega como parâmetro: a
    coleta aplica a classificação sem depender de quem a define.
    """
    normalized = str(usage_type or "").lower()
    for marker, bucket in markers:
        if marker in normalized:
            return bucket
    return "other"


def collect_glue_costs(
    ce_client,
    *,
    window: AnalysisWindow,
    markers: Sequence[tuple[str, str]],
    version: str = "",
    matrix: BillingMatrix | None = None,
) -> GlueCostCoverage:
    """Cobrança Glue da janela de análise por bucket, em USD.

    A janela é a mesma do consumo coletado e a mesma do Athena. O Cost Explorer
    aceita intervalo arbitrário, então não há motivo para o custo seguir o
    mês-calendário enquanto o consumo segue outra coisa.
    """
    coverage = GlueCostCoverage(
        period_start=window.start_date.isoformat(),
        data_through=window.data_through.isoformat(),
        allocation_version=version,
    )

    for metric in _METRICS:
        responses = matrix.for_service("AWS Glue", metric) if matrix else None
        if matrix is not None and responses is None:
            continue
        if matrix is None:
            try:
                responses = [
                    ce_client.get_cost_and_usage(
                        TimePeriod={
                            "Start": window.start_date.isoformat(),
                            "End": window.end_date.isoformat(),
                        },
                        Granularity="MONTHLY",
                        Metrics=[metric],
                        Filter={
                            "Dimensions": {"Key": "SERVICE", "Values": ["AWS Glue"]}
                        },
                        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
                    )
                ]
            except Exception as exc:
                coverage.gaps.append(
                    f"Cost Explorer {metric}: {type(exc).__name__}"
                )
                continue

        buckets: dict[str, float] = {}
        unknown: list[str] = []
        for response in responses or []:
            for period in response.get("ResultsByTime", []):
                for group in period.get("Groups", []):
                    usage_type = next(iter(group.get("Keys", [])), "")
                    value = (group.get("Metrics") or {}).get(metric, {})
                    amount = usd_amount(value.get("Amount"), value.get("Unit"))
                    if amount is None:
                        coverage.gaps.append(non_usd_gap(value.get("Unit")))
                        return coverage
                    bucket = classify_usage_type(usage_type, markers)
                    if bucket == "other" and usage_type:
                        # Usage type desconhecido nunca é descartado em silêncio.
                        unknown.append(str(usage_type))
                    buckets[bucket] = buckets.get(bucket, 0.0) + amount

        coverage.cost_metric = metric
        coverage.buckets = {name: round(value, 6) for name, value in buckets.items()}
        coverage.unknown_usage_types = sorted(set(unknown))
        coverage.net_cost = round(sum(buckets.values()), 6)
        coverage.cost_quality = "partial" if coverage.net_cost else "unavailable"
        return coverage

    return coverage


def allocate_costs(
    account: Account,
    coverage: GlueCostCoverage,
    config: Any,
    *,
    allocatable_buckets: frozenset[str] | set[str] = frozenset(),
    jobs_collection_complete: bool = True,
) -> GlueCostCoverage:
    """Rateia cada bucket entre os ativos coletados e define a qualidade."""
    if not coverage.buckets:
        coverage.cost_quality = "unavailable"
        return coverage

    flex_jobs = [job for job in account.glue_jobs if _is_flex(job)]
    standard_jobs = [job for job in account.glue_jobs if not _is_flex(job)]
    pools = (
        ("etl_job", standard_jobs, lambda job: job.total_dpu_hours_window),
        ("flex", flex_jobs, lambda job: job.total_dpu_hours_window),
        ("crawler", account.glue_crawlers, lambda item: item.dpu_hours_window),
        (
            "interactive_session",
            account.interactive_sessions,
            lambda item: item.dpu_hours,
        ),
        (
            "databrew",
            account.databrew_jobs,
            lambda item: item.estimated_node_hours_window,
        ),
    )

    # Um bucket marcado como rateável sem pool correspondente sumiria em
    # silêncio; a divergência é registrada em vez de descartada.
    for bucket in sorted(set(allocatable_buckets) - {pool[0] for pool in pools}):
        if coverage.buckets.get(bucket):
            coverage.gaps.append(f"bucket {bucket} sem regra de rateio definida")

    quality = _quality(account, coverage, config, jobs_collection_complete)
    allocated: list[str] = []
    for bucket, assets, consumption in pools:
        cost = coverage.buckets.get(bucket, 0.0)
        if not cost:
            continue
        total = sum(max(0.0, consumption(asset)) for asset in assets)
        if total <= 0:
            coverage.gaps.append(
                f"bucket {bucket} cobrado sem consumo coletado para ratear"
            )
            continue
        for asset in assets:
            share = max(0.0, consumption(asset)) / total
            asset.allocated_cost = round(cost * share, 6)
            asset.cost_quality = quality
            if bucket in {"etl_job", "flex"}:
                _allocate_failure_cost(asset)
        allocated.append(bucket)

    # A cobertura registra o que foi rateado de fato — um bucket cobrado sem
    # consumo para ratear continua contando como não atribuído.
    coverage.allocated_buckets = sorted(allocated)
    coverage.cost_quality = quality
    return coverage


def _allocate_failure_cost(job) -> None:
    """Separa a parcela de falhas sem acrescentá-la novamente ao custo total."""
    total = job.total_dpu_hours_window
    failed = min(job.total_failed_dpu_hours_window, total)
    # Zero também representa dataset legado, que não possuía a separação de
    # DPU das falhas. Nesse caso o estimador mantém o fallback histórico.
    if job.allocated_cost is None or total <= 0 or failed <= 0:
        return
    job.failed_cost_window = round(
        min(job.allocated_cost, job.allocated_cost * failed / total), 6
    )
    job.failure_cost_quality = (
        "partial"
        if (job.estimated_failed_dpu_hours_window or 0) > 0
        else job.cost_quality
    )


def _is_flex(job) -> bool:
    return str(job.execution_class or "").upper() == "FLEX"


def _quality(
    account: Account,
    coverage: GlueCostCoverage,
    config: Any,
    jobs_collection_complete: bool,
) -> str:
    """`reconciled` só com coleta íntegra, DPU medido e consumo aderente."""
    thresholds = config.thresholds
    compute_cost = sum(
        coverage.buckets.get(bucket, 0.0) for bucket in ("etl_job", "flex")
    )
    modeled = sum(
        job.total_dpu_hours_window * _rate(job, config) for job in account.glue_jobs
    )
    if compute_cost > 0:
        coverage.modeled_ratio = round(modeled / compute_cost, 4)

    if not jobs_collection_complete:
        coverage.gaps.append("inventário de jobs incompleto para reconciliar custo")
        return "partial"
    if _estimated_share(account) > thresholds.glue_estimated_dpu_tolerance:
        coverage.gaps.append(
            "parte das execuções do mês não reportou DPUSeconds; "
            "rateio usa duração estimada"
        )
        return "partial"
    if coverage.modeled_ratio is None:
        return "partial"
    if not (
        thresholds.glue_reconciliation_low
        <= coverage.modeled_ratio
        <= thresholds.glue_reconciliation_high
    ):
        coverage.gaps.append(
            "consumo modelado e cobrança do Cost Explorer divergem além da banda"
        )
        return "partial"
    if coverage.unknown_usage_types:
        coverage.gaps.append("usage types não classificados na cobrança Glue")
        return "partial"
    return "reconciled"


def _estimated_share(account: Account) -> float:
    """Fração das DPU-horas da janela que veio de duração estimada."""
    estimated = sum(
        max(0.0, job.estimated_dpu_hours_window) for job in account.glue_jobs
    )
    total = sum(job.total_dpu_hours_window for job in account.glue_jobs)
    if total <= 0:
        return 1.0 if estimated else 0.0
    return estimated / total


def _rate(job, config: Any) -> float:
    if job.capacity_unit == "M-DPU":
        return config.pricing.glue_ray_mdpu_hour
    return config.pricing.glue_rate(job.execution_class)
