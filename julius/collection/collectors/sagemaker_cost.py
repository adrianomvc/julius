"""Custo SageMaker do Cost Explorer e rateio conservador por componente."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from julius.collection.collectors.billing_matrix import BillingMatrix
from julius.collection.currency import non_usd_gap, usd_amount
from julius.collection.models import (
    Account,
    SageMakerCostCoverage,
    SageMakerSavingsPlanCoverage,
)
from julius.collection.window import AnalysisWindow

_METRICS = ("NetUnblendedCost", "UnblendedCost")


def classify_usage_type(
    usage_type: str, markers: Sequence[tuple[str, str]]
) -> str:
    normalized = str(usage_type or "").lower()
    for marker, bucket in markers:
        if marker in normalized:
            return bucket
    return "other"


def collect_sagemaker_costs(
    ce_client,
    *,
    window: AnalysisWindow,
    markers: Sequence[tuple[str, str]],
    version: str = "",
    matrix: BillingMatrix | None = None,
) -> SageMakerCostCoverage:
    coverage = SageMakerCostCoverage(
        period_start=window.start_date.isoformat(),
        data_through=window.data_through.isoformat(),
        window_days=window.days,
        allocation_version=version,
    )
    for metric in _METRICS:
        responses = matrix.for_service("Amazon SageMaker", metric) if matrix else None
        if matrix is not None and responses is None:
            continue
        if matrix is None:
            try:
                request: dict[str, Any] = {
                    "TimePeriod": {
                        "Start": window.start_date.isoformat(),
                        "End": window.end_date.isoformat(),
                    },
                    "Granularity": "MONTHLY",
                    "Metrics": [metric],
                    "Filter": {
                        "Dimensions": {
                            "Key": "SERVICE",
                            "Values": ["Amazon SageMaker"],
                        }
                    },
                    "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
                }
                responses = []
                while True:
                    response = ce_client.get_cost_and_usage(**request)
                    responses.append(response)
                    token = response.get("NextPageToken")
                    if not token:
                        break
                    request["NextPageToken"] = token
            except Exception as exc:
                coverage.gaps.append(
                    f"Cost Explorer {metric}: {type(exc).__name__}"
                )
                continue

        buckets: dict[str, float] = {}
        unknown: list[str] = []
        for response in responses or []:
            for period in response.get("ResultsByTime", []) or []:
                for group in period.get("Groups", []) or []:
                    usage_type = next(iter(group.get("Keys", []) or []), "")
                    value = (group.get("Metrics") or {}).get(metric, {})
                    amount = usd_amount(value.get("Amount"), value.get("Unit"))
                    if amount is None:
                        coverage.gaps.append(non_usd_gap(value.get("Unit")))
                        return coverage
                    bucket = classify_usage_type(usage_type, markers)
                    if bucket == "other" and usage_type:
                        unknown.append(str(usage_type))
                    buckets[bucket] = buckets.get(bucket, 0.0) + amount
        coverage.cost_metric = metric
        coverage.buckets = {
            name: round(value, 6) for name, value in buckets.items()
        }
        coverage.unknown_usage_types = sorted(set(unknown))
        coverage.net_cost = round(sum(buckets.values()), 6)
        coverage.cost_quality = "partial" if coverage.net_cost else "unavailable"
        return coverage
    return coverage


def allocate_costs(
    account: Account,
    coverage: SageMakerCostCoverage,
    allocatable: frozenset[str] | set[str],
) -> SageMakerCostCoverage:
    assets = _assets_by_component(account)
    allocated: list[str] = []
    for bucket, amount in coverage.buckets.items():
        if bucket not in allocatable or amount <= 0:
            continue
        candidates = assets.get(bucket, [])
        # Job sem base de rateio sai do denominador, e a cobrança dele é
        # repartida entre os que ficaram — que passam a parecer mais caros do
        # que são. A redistribuição é inevitável sem base; o silêncio não.
        fora = _jobs_without_cost_base(account, bucket)
        if fora:
            coverage.gaps.append(
                f"{bucket}: {len(fora)} job(s) sem base de rateio; a cobrança "
                f"deles foi redistribuída entre os demais"
            )
        if not candidates:
            coverage.gaps.append(f"{bucket}: cobrança sem ativo coletado")
            continue
        total = sum(weight for _, weight in candidates)
        if total <= 0:
            coverage.gaps.append(f"{bucket}: ativo sem base de rateio")
            continue
        for asset, weight in candidates:
            allocated_cost = round(amount * weight / total, 6)
            if hasattr(asset, "allocated_storage_cost"):
                asset.allocated_storage_cost = allocated_cost
            else:
                asset.allocated_cost = allocated_cost
            asset.cost_coverage_days = coverage.window_days or None
            asset.cost_quality = (
                "reconciled" if len(candidates) == 1 else "allocated"
            )
        allocated.append(bucket)
    coverage.allocated_buckets = sorted(set(allocated))
    recognized = {
        bucket
        for bucket, amount in coverage.buckets.items()
        if amount > 0 and bucket in allocatable
    }
    coverage.cost_quality = (
        "reconciled"
        if recognized
        and recognized <= set(coverage.allocated_buckets)
        and not coverage.unknown_usage_types
        else "partial"
    )
    return coverage


def collect_and_allocate_costs(
    account: Account,
    ce_client,
    *,
    window: AnalysisWindow,
    markers: Sequence[tuple[str, str]],
    version: str,
    allocatable: frozenset[str] | set[str],
    matrix: BillingMatrix | None = None,
) -> SageMakerCostCoverage:
    """Coleta SageMaker e ancora também o storage EFS conhecido dos Domains."""
    coverage = collect_sagemaker_costs(
        ce_client,
        window=window,
        markers=markers,
        version=version,
        matrix=matrix,
    )
    allocate_costs(account, coverage, allocatable)
    _allocate_efs_storage(account, coverage, ce_client, window)
    return coverage


def _allocate_efs_storage(
    account: Account,
    coverage: SageMakerCostCoverage,
    ce_client,
    window: AnalysisWindow,
) -> None:
    domains = [
        domain
        for domain in account.sagemaker_domains
        if (domain.efs_storage_bytes or 0) > 0
    ]
    if not domains:
        return
    for metric in _METRICS:
        try:
            request: dict[str, Any] = {
                "TimePeriod": {
                    "Start": window.start_date.isoformat(),
                    "End": window.end_date.isoformat(),
                },
                "Granularity": "MONTHLY",
                "Metrics": [metric],
                "Filter": {
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": ["Amazon Elastic File System"],
                    }
                },
                "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            }
            responses = []
            while True:
                response = ce_client.get_cost_and_usage(**request)
                responses.append(response)
                token = response.get("NextPageToken")
                if not token:
                    break
                request["NextPageToken"] = token
        except Exception as exc:
            coverage.gaps.append(
                f"EFS Cost Explorer {metric}: {type(exc).__name__}"
            )
            continue

        storage_cost = 0.0
        for response in responses:
            for period in response.get("ResultsByTime", []) or []:
                for group in period.get("Groups", []) or []:
                    usage_type = str(
                        next(iter(group.get("Keys", []) or []), "")
                    )
                    if "storage" not in usage_type.lower():
                        continue
                    value = (group.get("Metrics") or {}).get(metric, {})
                    amount = usd_amount(
                        value.get("Amount"),
                        value.get("Unit"),
                    )
                    if amount is None:
                        coverage.gaps.append(
                            "EFS " + non_usd_gap(value.get("Unit"))
                        )
                        return
                    storage_cost += amount

        coverage.efs_cost_metric = metric
        coverage.efs_storage_cost = round(storage_cost, 6)
        if storage_cost <= 0:
            coverage.efs_cost_quality = "unavailable"
            return
        total_bytes = sum(domain.efs_storage_bytes or 0 for domain in domains)
        if total_bytes <= 0:
            coverage.gaps.append("EFS storage sem bytes para rateio por Domain")
            coverage.efs_cost_quality = "partial"
            return
        for domain in domains:
            domain.allocated_storage_cost = round(
                storage_cost
                * (domain.efs_storage_bytes or 0)
                / total_bytes,
                6,
            )
            domain.cost_coverage_days = window.days
            domain.cost_quality = (
                "reconciled" if len(domains) == 1 else "allocated"
            )
        coverage.efs_cost_quality = (
            "reconciled" if len(domains) == 1 else "allocated"
        )
        return


def _assets_by_component(account: Account) -> dict[str, list[tuple[Any, float]]]:
    days = max(1, account.window_days)
    out: dict[str, list[tuple[Any, float]]] = {
        "studio": [
            (app, max(1.0, app.active_days_per_month * 24.0))
            for app in account.sagemaker_apps
            if app.status == "InService"
        ],
        "studio_storage": [
            (space, float(space.ebs_volume_size_gb * days))
            for space in account.sagemaker_spaces
            if space.ebs_volume_size_gb > 0
        ],
        "notebook": [
            (item, float(days * 24))
            for item in account.sagemaker_notebooks
            if item.status == "InService"
        ],
        "training": _job_weights(account, "training"),
        "processing": _job_weights(account, "processing"),
        "transform": _job_weights(account, "transform"),
        "feature_store": [
            (
                item,
                float(
                    max(
                        1,
                        item.provisioned_read_capacity
                        + item.provisioned_write_capacity,
                    )
                    * days
                    * 24
                ),
            )
            for item in account.sagemaker_feature_groups
        ],
    }
    endpoints: list[tuple[Any, float]] = []
    serverless: list[tuple[Any, float]] = []
    for endpoint in account.sagemaker_endpoints:
        if endpoint.mode == "serverless":
            serverless.append(
                (
                    endpoint,
                    float(max(1, endpoint.provisioned_concurrency) * days * 24),
                )
            )
            continue
        capacity = sum(
            max(
                variant.current_instance_count,
                variant.desired_instance_count,
                variant.initial_instance_count,
            )
            for variant in endpoint.variants
        ) or endpoint.instance_count
        endpoints.append((endpoint, float(max(1, capacity) * days * 24)))
    out["endpoint"] = endpoints
    out["serverless"] = serverless
    return out


def _job_weights(account: Account, kind: str) -> list[tuple[Any, float]]:
    return [
        (job, max(job.instance_hours, 1e-9))
        for job in account.sagemaker_jobs
        if job.kind == kind
        and job.in_financial_window
        and job.instance_hours > 0
    ]


def _jobs_without_cost_base(account: Account, kind: str) -> list[str]:
    """Jobs da janela que o rateio não consegue considerar, e por isso distorce.

    `kind` é o nome do bucket de cobrança; só os buckets de job têm este caso.
    """
    return [
        job.name
        for job in account.sagemaker_jobs
        if job.kind == kind and job.in_financial_window and job.instance_hours <= 0
    ]


def collect_savings_plan_signal(
    ce_client, *, window: AnalysisWindow
) -> SageMakerSavingsPlanCoverage:
    signal = SageMakerSavingsPlanCoverage(
        period_start=window.start_date.isoformat(),
        data_through=window.data_through.isoformat(),
    )
    service_filter = {
        "Dimensions": {"Key": "SERVICE", "Values": ["Amazon SageMaker"]}
    }
    try:
        coverage = ce_client.get_savings_plans_coverage(
            TimePeriod={
                "Start": window.start_date.isoformat(),
                "End": window.end_date.isoformat(),
            },
            Granularity="MONTHLY",
            Metrics=["SpendCoveredBySavingsPlans"],
            Filter=service_filter,
        )
        rows = coverage.get("SavingsPlansCoverages", []) or []
        percentages = [
            _float((row.get("Coverage") or {}).get("CoveragePercentage"))
            for row in rows
        ]
        on_demand = [
            _float((row.get("Coverage") or {}).get("OnDemandCost"))
            for row in rows
        ]
        valid_percentages = [value for value in percentages if value is not None]
        valid_on_demand = [value for value in on_demand if value is not None]
        if valid_percentages:
            signal.coverage_percent = sum(valid_percentages) / len(valid_percentages)
        if valid_on_demand:
            signal.current_on_demand_spend = sum(valid_on_demand)
    except Exception as exc:
        signal.gaps.append(f"coverage: {type(exc).__name__}")

    try:
        utilization = ce_client.get_savings_plans_utilization(
            TimePeriod={
                "Start": window.start_date.isoformat(),
                "End": window.end_date.isoformat(),
            }
        )
        signal.utilization_percent = _float(
            (utilization.get("Total") or {}).get("UtilizationPercentage")
        )
    except Exception as exc:
        signal.gaps.append(f"utilization: {type(exc).__name__}")

    try:
        recommendation = ce_client.get_savings_plans_purchase_recommendation(
            SavingsPlansType="SAGEMAKER_SP",
            TermInYears="ONE_YEAR",
            PaymentOption="NO_UPFRONT",
            LookbackPeriodInDays="THIRTY_DAYS",
        )
        summary = (
            recommendation.get("SavingsPlansPurchaseRecommendation") or {}
        ).get("SavingsPlansPurchaseRecommendationSummary") or {}
        hourly = _float(summary.get("HourlyCommitmentToPurchase"))
        signal.estimated_monthly_commitment = (
            hourly * 730.0 if hourly is not None else None
        )
        signal.estimated_monthly_saving = _float(
            summary.get("EstimatedMonthlySavingsAmount")
        )
    except Exception as exc:
        signal.gaps.append(f"recommendation: {type(exc).__name__}")

    signal.quality = (
        "measured"
        if signal.coverage_percent is not None
        else "partial"
        if signal.utilization_percent is not None
        or signal.estimated_monthly_saving is not None
        else "unavailable"
    )
    return signal


def _float(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
