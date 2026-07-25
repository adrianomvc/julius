"""Reconciliação com CloudWatch e rateio do custo da janela.

O custo por padrão de query é rateio da cobrança diária pelos bytes
faturáveis — nunca fatura por query."""

from __future__ import annotations

from collections import defaultdict

from julius.collection.collectors.athena.evidence import AthenaExecutionEvidence
from julius.collection.models import AthenaCoverage
from julius.estimation.currency import non_usd_gap, usd_amount


def reconcile_cloudwatch(coverage, client, workgroups, start, end):
    if client is None:
        coverage.gaps.append("CloudWatch não coletado")
        return
    total = 0.0
    complete = True
    for workgroup in workgroups:
        try:
            response = client.get_metric_statistics(
                Namespace="AWS/Athena",
                MetricName="ProcessedBytes",
                Dimensions=[{"Name": "WorkGroup", "Value": workgroup}],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Sum"],
            )
            total += sum(float(point.get("Sum") or 0) for point in response.get("Datapoints", []))
        except Exception as exc:
            complete = False
            coverage.gaps.append(f"CloudWatch {workgroup}: {type(exc).__name__}")
    if complete:
        coverage.cloudwatch_bytes = int(total)
        if coverage.api_scanned_bytes:
            coverage.reconciliation_ratio = round(total / coverage.api_scanned_bytes, 4)


def costs(client, start, end, gaps):
    """Custo Athena diário em USD, a única moeda aceita."""
    if client is None:
        gaps.append("Cost Explorer não coletado")
        return {}, "", "USD", False
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
                        gaps.append(non_usd_gap(value.get("Unit")))
                        return {}, "", str(value.get("Unit") or "USD"), False
                    amount += reported
                daily[period["TimePeriod"]["Start"]] = amount
            return daily, metric, "USD", isolated
        except Exception as exc:
            gaps.append(f"Cost Explorer {metric}: {type(exc).__name__}")
    return {}, "", "USD", False


def reconciled(coverage: AthenaCoverage) -> bool:
    blocking = (
        "list_work_groups",
        "list_query_executions",
        "get_work_group",
        "execuções não processadas",
        "CloudWatch",
        "métricas CloudWatch desabilitadas",
    )
    return (
        coverage.workgroups_total > 0
        and coverage.workgroups_covered == coverage.workgroups_total
        and not coverage.truncated
        and not any(any(marker in gap for marker in blocking) for gap in coverage.gaps)
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
