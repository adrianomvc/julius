"""Custo real de S3 pelo Cost Explorer, rateado por bytes medidos.

Terceira vez que o mesmo problema aparece — Glue, Redshift e agora S3 —, e pela
mesma razão: o Cost Explorer não expõe dimensão de recurso, a cobrança chega
agregada por `USAGE_TYPE`, e o custo por bucket é rateio, nunca fatura por
bucket.

A proporção do rateio é o tamanho medido pelo CloudWatch. Ela é boa: o custo de
armazenamento é literalmente proporcional a bytes × tempo, então ratear por
bytes é reproduzir a fórmula da cobrança, não aproximá-la.

O que o rateio não faz é descer ao prefixo. Um bucket sabe quanto custa; um
prefixo dentro dele sabe quantos bytes ocupa, e a economia de apagá-lo é essa
fração do custo do bucket. É por isso que a regra precisa do bucket **e** do
prefixo, e não afirma nada quando falta um dos dois.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from julius.collection.currency import non_usd_gap, usd_amount
from julius.collection.models import Account, S3CostCoverage, S3CostLine
from julius.collection.window import AnalysisWindow

_METRICS = ("NetUnblendedCost", "UnblendedCost")


def classify_usage_type(usage_type: str, markers: Sequence[tuple[str, str]]) -> str:
    normalized = str(usage_type or "").lower()
    for marker, bucket in markers:
        if marker in normalized:
            return bucket
    return "other"


def collect_s3_costs(
    ce_client,
    *,
    window: AnalysisWindow,
    markers: Sequence[tuple[str, str]],
    version: str = "",
) -> S3CostCoverage:
    """Cobrança S3 da janela de análise por bucket de usage type, em USD."""
    coverage = S3CostCoverage(
        period_start=window.start_date.isoformat(),
        data_through=window.data_through.isoformat(),
        allocation_version=version,
    )

    for metric in _METRICS:
        try:
            request: dict[str, Any] = dict(
                TimePeriod={
                    "Start": window.start_date.isoformat(),
                    "End": window.end_date.isoformat(),
                },
                Granularity="MONTHLY",
                Metrics=[metric, "UsageQuantity"],
                Filter={
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": ["Amazon Simple Storage Service"],
                    }
                },
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
            responses = []
            while True:
                response = ce_client.get_cost_and_usage(**request)
                responses.append(response)
                token = response.get("NextPageToken")
                if not token:
                    break
                request["NextPageToken"] = token
        except Exception as exc:
            coverage.gaps.append(f"Cost Explorer {metric}: {type(exc).__name__}")
            continue

        buckets: dict[str, float] = {}
        lines_by_key: dict[tuple[str, str, str], S3CostLine] = {}
        unknown: list[str] = []
        for response in responses:
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
                        unknown.append(str(usage_type))
                    buckets[bucket] = buckets.get(bucket, 0.0) + amount
                    usage = (group.get("Metrics") or {}).get("UsageQuantity", {})
                    usage_unit = str(usage.get("Unit") or "")
                    key = (str(usage_type), bucket, usage_unit)
                    line = lines_by_key.setdefault(
                        key,
                        S3CostLine(
                            usage_type=str(usage_type),
                            bucket=bucket,
                            usage_unit=usage_unit,
                        ),
                    )
                    line.cost = round(line.cost + amount, 6)
                    quantity = _number(usage.get("Amount"))
                    if quantity is not None:
                        line.usage_quantity = (
                            (line.usage_quantity or 0.0) + quantity
                        )

        coverage.cost_metric = metric
        coverage.buckets = {name: round(value, 6) for name, value in buckets.items()}
        coverage.lines = sorted(
            lines_by_key.values(),
            key=lambda item: (item.usage_type, item.usage_unit),
        )
        coverage.unknown_usage_types = sorted(set(unknown))
        coverage.net_cost = round(sum(buckets.values()), 6)
        coverage.cost_quality = "partial" if coverage.net_cost else "unavailable"
        return coverage

    return coverage


def allocate_costs(
    account: Account,
    coverage: S3CostCoverage,
    storage_buckets: frozenset[str] | set[str],
) -> S3CostCoverage:
    """Rateia o armazenamento entre os buckets, proporcional a bytes medidos."""
    inventario = list(getattr(account, "s3_buckets", []) or [])
    storage = coverage.cost_for(storage_buckets)
    if not inventario or storage <= 0:
        return coverage

    pesos = {item.name: _peso(item) for item in inventario}
    total = sum(pesos.values())
    if total <= 0:
        # Sem tamanho medido não há proporção; ratear igualmente inventaria um
        # número, e é justamente isso que este projeto não faz.
        coverage.gaps.append(
            "tamanho por bucket não medido: armazenamento não rateado"
        )
        return coverage

    for item in inventario:
        item.allocated_storage_cost = round(storage * pesos[item.name] / total, 2)
        item.cost_quality = (
            "reconciled" if len(inventario) == 1 else coverage.cost_quality
        )
    coverage.cost_quality = "reconciled" if len(inventario) == 1 else "partial"
    return coverage


def _peso(bucket: Any) -> float:
    """Bytes medidos. Bucket sem métrica não puxa cobrança."""
    return float(bucket.total_bytes or 0.0)


def _number(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
