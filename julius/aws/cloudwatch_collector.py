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
