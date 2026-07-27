"""Custo real de Redshift pelo Cost Explorer, rateado por capacidade.

Mesmo problema do Glue: o Cost Explorer não expõe dimensão de recurso, então a
cobrança chega agregada por `USAGE_TYPE` e o custo por cluster é rateio, nunca
fatura por cluster.

A proporção do rateio é a capacidade declarada × dias observados. É a única que
o plano de controle sustenta: sem histórico de query não há consumo medido por
cluster, e usar qualquer outra base seria inventar uma precisão que não existe.
Um único cluster na conta recebe o bucket inteiro, e aí o rateio é exato.

O que o rateio produz é `allocated_compute_cost` — só compute, porque só ele
para de ser cobrado quando o cluster pausa. Sem cobrança classificada o campo
fica `None`, e as regras seguem sem quantificar economia, como já faziam.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from julius.collection.currency import non_usd_gap, usd_amount
from julius.collection.models import Account, RedshiftCostCoverage
from julius.collection.window import AnalysisWindow

_METRICS = ("NetUnblendedCost", "UnblendedCost")


def classify_usage_type(usage_type: str, markers: Sequence[tuple[str, str]]) -> str:
    normalized = str(usage_type or "").lower()
    for marker, bucket in markers:
        if marker in normalized:
            return bucket
    return "other"


def collect_redshift_costs(
    ce_client,
    *,
    window: AnalysisWindow,
    markers: Sequence[tuple[str, str]],
    version: str = "",
) -> RedshiftCostCoverage:
    """Cobrança Redshift da janela de análise por bucket, em USD."""
    coverage = RedshiftCostCoverage(
        period_start=window.start_date.isoformat(),
        data_through=window.data_through.isoformat(),
        allocation_version=version,
    )

    for metric in _METRICS:
        try:
            response = ce_client.get_cost_and_usage(
                TimePeriod={
                    "Start": window.start_date.isoformat(),
                    "End": window.end_date.isoformat(),
                },
                Granularity="MONTHLY",
                Metrics=[metric],
                Filter={
                    "Dimensions": {"Key": "SERVICE", "Values": ["Amazon Redshift"]}
                },
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
        except Exception as exc:
            coverage.gaps.append(f"Cost Explorer {metric}: {type(exc).__name__}")
            continue

        buckets: dict[str, float] = {}
        unknown: list[str] = []
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
    coverage: RedshiftCostCoverage,
    compute_buckets: frozenset[str] | set[str],
) -> RedshiftCostCoverage:
    """Rateia o compute entre os clusters, por capacidade × dias observados."""
    clusters = list(getattr(account, "redshift_clusters", []) or [])
    compute = coverage.compute_cost(compute_buckets)
    if not clusters or compute <= 0:
        return coverage

    weights = {cluster.name: _weight(cluster) for cluster in clusters}
    total = sum(weights.values())
    if total <= 0:
        # Capacidade desconhecida em todos: sem base para ratear, e um rateio
        # igualitário aqui seria número inventado.
        coverage.gaps.append(
            "capacidade declarada ausente: compute não rateado entre clusters"
        )
        return coverage

    for cluster in clusters:
        share = weights[cluster.name] / total
        cluster.allocated_compute_cost = round(compute * share, 2)
        cluster.cost_quality = (
            "reconciled" if len(clusters) == 1 else coverage.cost_quality
        )
    coverage.cost_quality = "reconciled" if len(clusters) == 1 else "partial"
    return coverage


def _weight(cluster: Any) -> float:
    """Capacidade declarada × dias com métrica.

    Provisionado pesa por nós; serverless por RPU base. Cluster sem dias
    observados não puxa cobrança: não sabemos que ele esteve lá.
    """
    days = max(1, cluster.observed_days or cluster.coverage_days or 0)
    if cluster.kind == "serverless":
        capacity = float(cluster.base_rpu or 0)
    else:
        capacity = float(cluster.node_count or 0)
    return capacity * days
