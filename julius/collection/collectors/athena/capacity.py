"""Inventário read-only de Capacity Reservations do Athena."""

from __future__ import annotations

from statistics import quantiles

from julius.collection.collectors import metrics
from julius.collection.collectors.metrics import MetricQuery
from julius.collection.collectors.paginate import safe_call, safe_pages
from julius.collection.models import AthenaCapacityReservation

_METRICS = {
    "DPUAllocated": ("allocated_dpus_p95", "Average"),
    "DPUConsumed": ("consumed_dpus_p95", "Average"),
    "QueryQueueTime": ("query_queue_p95_ms", "Average"),
    "QueryPlanningTime": ("planning_p95_ms", "Average"),
    "EngineExecutionTime": ("engine_p95_ms", "Average"),
}


def collect_capacity_reservations(
    athena_client,
    cloudwatch_client,
    ce_client=None,
    *,
    window,
    gaps: list[str] | None = None,
) -> list[AthenaCapacityReservation]:
    listing = safe_pages(
        athena_client, "list_capacity_reservations", "CapacityReservations"
    )
    if gaps is not None and not listing.complete:
        gaps.append(
            f"list_capacity_reservations: {listing.error_category or 'incompleto'}"
        )
    out = []
    for summary in listing.items:
        name = str(summary.get("Name") or "")
        detail, detail_error = safe_call(
            athena_client, "get_capacity_reservation", Name=name
        )
        assignment, assignment_error = safe_call(
            athena_client,
            "get_capacity_assignment_configuration",
            CapacityReservationName=name,
        )
        if gaps is not None:
            if detail_error:
                gaps.append(f"get_capacity_reservation:{name}: {detail_error}")
            if assignment_error:
                gaps.append(f"get_capacity_assignment:{name}: {assignment_error}")
        values = detail.get("CapacityReservation") or summary
        item = AthenaCapacityReservation(
            name=name,
            status=str(values.get("Status") or ""),
            target_dpus=int(values.get("TargetDpus") or 0),
            workgroups=[
                str(name)
                for item_assignment in (
                    assignment.get("CapacityAssignmentConfiguration") or {}
                ).get("CapacityAssignments", [])
                for name in item_assignment.get("WorkGroupNames", [])
            ],
            coverage_days=window.days,
        )
        out.append(item)
    _metrics(cloudwatch_client, out, window, gaps)
    if ce_client is not None and out:
        _allocate_cost(out, _capacity_cost(ce_client, window, gaps))
    return out


def _capacity_cost(client, window, gaps) -> float | None:
    total = 0.0
    token = None
    try:
        while True:
            kwargs = {
                "TimePeriod": {
                    "Start": window.start_date.isoformat(),
                    "End": window.end_date.isoformat(),
                },
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "Filter": {
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": ["Amazon Athena"],
                    }
                },
                "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            }
            if token:
                kwargs["NextPageToken"] = token
            response = client.get_cost_and_usage(**kwargs)
            for period in response.get("ResultsByTime", []):
                for group in period.get("Groups", []):
                    usage_type = " ".join(group.get("Keys", [])).lower()
                    if "capacity" not in usage_type:
                        continue
                    total += float(
                        (group.get("Metrics", {}).get("UnblendedCost") or {}).get(
                            "Amount", 0
                        )
                        or 0
                    )
            token = response.get("NextPageToken")
            if not token:
                break
    except Exception:
        if gaps is not None:
            gaps.append("cost_explorer_athena_capacity")
        return None
    return total if total > 0 else None


def _allocate_cost(reservations, total_cost) -> None:
    if total_cost is None:
        return
    total_dpus = sum(item.target_dpus for item in reservations)
    if total_dpus <= 0:
        return
    for item in reservations:
        item.allocated_cost = round(total_cost * item.target_dpus / total_dpus, 4)
        item.cost_quality = "allocated"


def _metrics(client, reservations, window, gaps) -> None:
    """Cinco métricas de cada reserva, em lote.

    O período é horário, não diário: `idle_hours` conta horas com consumo zero, e
    um ponto por dia não responde isso. Passar do teto de pontos por chamada é
    resolvido por paginação dentro de `metrics.collect`.
    """
    if client is None or not reservations:
        return
    pedidos = [
        (reservation, field, metric_name, MetricQuery(
            namespace="AWS/Athena",
            metric_name=metric_name,
            stat=statistic,
            dimensions=(("CapacityReservation", reservation.name),),
            period=3600,
        ))
        for reservation in reservations
        for metric_name, (field, statistic) in _METRICS.items()
    ]
    problems = metrics.collect(
        client,
        [query for *_resto, query in pedidos],
        start=window.start,
        end=window.end,
    )
    if problems and gaps is not None:
        gaps.append("cloudwatch_athena_capacity")

    for reservation, field, metric_name, query in pedidos:
        samples = query.values
        if not samples:
            continue
        setattr(reservation, field, round(_p95(samples), 3))
        if metric_name == "DPUConsumed":
            reservation.consumed_dpu_hours = round(sum(samples), 3)
            reservation.idle_hours = float(sum(value == 0 for value in samples))


def _p95(values: list[float]) -> float:
    if len(values) < 2:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[94]
