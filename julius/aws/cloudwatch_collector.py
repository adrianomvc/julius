"""Coletor CloudWatch: enriquece os Glue Jobs com utilização de CPU.

O Glue publica métricas no namespace "Glue". Usamos `glue.ALL.system.cpuSystemLoad`
(carga média de CPU dos executores, 0–1) para preencher `avg_cpu_load` — o que
destrava as regras de capacidade (Auto Scaling / workers superdimensionados) ao vivo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean

from julius.inventory.model import GlueJob

_NAMESPACE = "Glue"
_CPU_METRIC = "glue.ALL.system.cpuSystemLoad"
_OBSERVABILITY_METRICS = {
    "avg_worker_utilization": ("glue.driver.workerUtilization", "Average"),
    "max_memory_used_pct": ("glue.ALL.memory.total.used.percentage", "Maximum"),
    "max_disk_used_pct": ("glue.ALL.disk.used.percentage", "Maximum"),
    "max_task_skew": ("glue.driver.skewness.job", "Maximum"),
    "avg_all_executors": (
        "glue.driver.ExecutorAllocationManager.executors.numberAllExecutors",
        "Average",
    ),
    "avg_max_needed_executors": (
        "glue.driver.ExecutorAllocationManager.executors.numberMaxNeededExecutors",
        "Average",
    ),
}


def enrich_glue_cpu(
    cw_client,
    jobs: list[GlueJob],
    *,
    lookback_days: int = 90,
    now: datetime | None = None,
) -> None:
    """Preenche `avg_cpu_load` (0–1) de cada job in place, best-effort por job."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    for job in jobs:
        avg = _avg_cpu(cw_client, job.name, start, now)
        if avg is not None:
            job.avg_cpu_load = round(avg, 3)


def enrich_glue_observability(
    cw_client,
    jobs: list[GlueJob],
    *,
    lookback_days: int = 90,
    now: datetime | None = None,
) -> None:
    """Coleta sinais que tornam recomendações de capacidade acionáveis.

    Falhas são isoladas por métrica; ausência permanece `None` e nunca é
    interpretada como ausência de pressão.
    """
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    for job in jobs:
        for field, (metric_name, statistic) in _OBSERVABILITY_METRICS.items():
            value = _metric(cw_client, job.name, metric_name, statistic, start, now)
            if value is not None:
                setattr(job, field, round(value, 3))


def _avg_cpu(cw_client, job_name: str, start: datetime, end: datetime) -> float | None:
    try:
        resp = cw_client.get_metric_statistics(
            Namespace=_NAMESPACE,
            MetricName=_CPU_METRIC,
            Dimensions=[
                {"Name": "JobName", "Value": job_name},
                {"Name": "Type", "Value": "gauge"},
            ],
            StartTime=start,
            EndTime=end,
            Period=86400,  # média diária
            Statistics=["Average"],
        )
    except Exception:
        return None
    points = [d["Average"] for d in resp.get("Datapoints", []) if "Average" in d]
    return mean(points) if points else None


def _metric(
    cw_client,
    job_name: str,
    metric_name: str,
    statistic: str,
    start: datetime,
    end: datetime,
) -> float | None:
    dimensions = [
        {"Name": "JobName", "Value": job_name},
        {"Name": "JobRunId", "Value": "ALL"},
        {"Name": "Type", "Value": "gauge"},
    ]
    if metric_name.startswith("glue.driver.worker") or "memory." in metric_name or "disk." in metric_name or "skewness." in metric_name:
        dimensions.append(
            {"Name": "ObservabilityGroup", "Value": (
                "job_performance" if "skewness." in metric_name else "resource_utilization"
            )}
        )
    try:
        response = cw_client.get_metric_statistics(
            Namespace=_NAMESPACE,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=[statistic],
        )
    except Exception:
        return None
    values = [
        float(point[statistic])
        for point in response.get("Datapoints", [])
        if statistic in point
    ]
    if not values:
        return None
    # Não suaviza pressão de memória/disco/skew: para métricas pedidas como
    # Maximum, o gate de capacidade precisa preservar o pior pico da janela.
    return max(values) if statistic == "Maximum" else mean(values)
