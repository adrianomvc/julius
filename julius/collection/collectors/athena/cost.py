"""Reconciliação com CloudWatch e rateio do custo da janela.

O custo por padrão de query é rateio da cobrança diária pelos bytes
faturáveis — nunca fatura por query."""

from __future__ import annotations

from collections import defaultdict

from julius.collection.collectors import metrics
from julius.collection.collectors.athena.evidence import AthenaExecutionEvidence
from julius.collection.collectors.metrics import MetricQuery
from julius.collection.currency import non_usd_gap, usd_amount
from julius.collection.models import AthenaCoverage


def reconcile_cloudwatch(coverage, client, workgroups, start, end, telemetry):
    if client is None:
        telemetry.unavailable(
            "Athena CloudWatch",
            category="not_configured",
            detail="cliente não fornecido",
        )
        return
    telemetry.used("Athena CloudWatch")
    # Um workgroup por chamada era uma ida à AWS por workgroup; em lote é uma só.
    # A reconciliação exige o total íntegro, então qualquer lote que falhe
    # impede a escrita — o rateio parcial afirmaria um total que não foi medido.
    queries = [
        MetricQuery(
            namespace="AWS/Athena",
            metric_name="ProcessedBytes",
            stat="Sum",
            dimensions=(("WorkGroup", workgroup),),
        )
        for workgroup in workgroups
    ]
    problems = metrics.collect(client, queries, start=start, end=end)
    for exc in problems:
        telemetry.failed(
            "Athena CloudWatch", exc, detail=f"{len(queries)} workgroup(s) em lote"
        )
    total = sum(sum(query.values) for query in queries)
    if not problems:
        coverage.cloudwatch_bytes = int(total)
        if coverage.api_scanned_bytes:
            coverage.reconciliation_ratio = round(total / coverage.api_scanned_bytes, 4)


def costs(client, start, end, telemetry):
    """Custo Athena diário em USD, a única moeda aceita."""
    if client is None:
        telemetry.unavailable(
            "Athena Cost Explorer",
            category="not_configured",
            detail="cliente não fornecido",
        )
        return {}, "", "USD", False
    telemetry.used("Athena Cost Explorer")
    for metric in ("NetUnblendedCost", "UnblendedCost"):
        try:
            response = client.get_cost_and_usage(
                TimePeriod={"Start": start.date().isoformat(), "End": end.date().isoformat()},
                Granularity="DAILY",
                Metrics=[metric],
                Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Athena"]}},
                GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
            daily: dict[str, float] = {}
            isolated = True
            for period in response.get("ResultsByTime", []):
                amount = 0.0
                for group in period.get("Groups", []):
                    usage = " ".join(group.get("Keys", [])).lower()
                    value = (group.get("Metrics") or {}).get(metric, {})
                    if any(term in usage for term in ("dpu", "capacity", "spark")):
                        continue
                    if usage and not any(term in usage for term in ("bytes", "tb", "data", "query")):
                        isolated = False
                    reported = usd_amount(value.get("Amount"), value.get("Unit"))
                    if reported is None:
                        telemetry.unavailable(
                            "Athena Cost Explorer",
                            category="unsupported_currency",
                            detail=non_usd_gap(value.get("Unit")),
                        )
                        return {}, "", str(value.get("Unit") or "USD"), False
                    amount += reported
                daily[period["TimePeriod"]["Start"]] = amount
            return daily, metric, "USD", isolated
        except Exception as exc:
            telemetry.failed("Athena Cost Explorer", exc, detail=metric)
    return {}, "", "USD", False


def reconciled(coverage: AthenaCoverage, telemetry) -> bool:
    """Cobertura completa e nenhuma fonte bloqueante em falha.

    A checagem era uma busca por trechos de texto dentro dos gaps; agora
    pergunta à telemetria quais fontes falharam.
    """
    return (
        coverage.workgroups_total > 0
        and coverage.workgroups_covered == coverage.workgroups_total
        and not coverage.truncated
        and not telemetry.blocked()
        and coverage.reconciliation_ratio is not None
        and 0.95 <= coverage.reconciliation_ratio <= 1.05
    )


def allocate(items, daily_cost, allowed):
    if not allowed:
        return False
    by_day: dict[str, list[AthenaExecutionEvidence]] = defaultdict(list)
    for item in items:
        if item.modality == "on_demand" and item.billed_bytes:
            by_day[item.submitted_at.date().isoformat()].append(item)
    for day, executions in by_day.items():
        total = sum(item.billed_bytes for item in executions)
        cost = daily_cost.get(day)
        if total and cost is not None:
            for item in executions:
                item.allocated_cost = cost * item.billed_bytes / total
    return all(
        not amount or day in by_day
        for day, amount in daily_cost.items()
    )
