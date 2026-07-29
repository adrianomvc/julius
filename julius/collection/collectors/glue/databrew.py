"""Coleta read-only de DataBrew jobs, schedules e consumo mensal estimado."""

from __future__ import annotations

from datetime import datetime, timezone

from julius.collection.collectors.paginate import safe_pages
from julius.collection.models import DataBrewJob
from julius.collection.schedule_frequency import expected_runs_per_month
from julius.collection.window import AnalysisWindow


def collect_jobs(
    databrew_client, *, window: AnalysisWindow, gaps: list[str] | None = None
) -> list[DataBrewJob]:
    schedules = _schedules(databrew_client, gaps)
    listagem = safe_pages(databrew_client, "list_jobs", "Jobs")
    if gaps is not None and not listagem.complete:
        gaps.append(f"list_jobs: {listagem.error_category or 'incompleto'}")
    out: list[DataBrewJob] = []
    for raw in listagem.items:
        name = str(raw.get("Name") or "")
        runs = _runs(databrew_client, name, window, gaps)
        capacity = int(raw.get("MaxCapacity", 5) or 5)
        execution_hours = sum(
            float(run.get("ExecutionTime", 0) or 0) / 3600.0 for run in runs
        )
        out.append(
            DataBrewJob(
                name=name,
                job_type=str(raw.get("Type") or "RECIPE"),
                max_capacity=capacity,
                timeout_min=int(raw.get("Timeout", 2880) or 2880),
                max_retries=int(raw.get("MaxRetries", 0) or 0),
                schedule_names=sorted(
                    schedule_name
                    for schedule_name, schedule in schedules.items()
                    if name in schedule["jobs"]
                ),
                runs_in_window=len(runs),
                failures_in_window=sum(
                    1
                    for run in runs
                    if str(run.get("State") or "") in {"FAILED", "TIMEOUT"}
                ),
                execution_hours_window=round(execution_hours, 4),
                # A API informa capacidade máxima, não utilização por node.
                estimated_node_hours_window=round(execution_hours * capacity, 4),
                owner_tag=(raw.get("Tags", {}) or {}).get("Owner"),
                run_ids_in_window=sorted(
                    str(run["RunId"]) for run in runs if run.get("RunId")
                ),
                expected_runs_monthly=sum(
                    float(schedule["expected"] or 0.0)
                    for schedule in schedules.values()
                    if name in schedule["jobs"]
                )
                or None,
                window_end=window.data_through.isoformat(),
                coverage_days=window.days,
                window_days=window.days,
            )
        )
    return out


def _schedules(databrew_client, gaps: list[str] | None = None) -> dict[str, dict]:
    resultado = safe_pages(databrew_client, "list_schedules", "Schedules")
    if gaps is not None and not resultado.complete:
        gaps.append(f"list_schedules: {resultado.error_category or 'incompleto'}")
    return {
        str(raw.get("Name") or ""): {
            "jobs": {str(name) for name in raw.get("JobNames", []) if name},
            "expected": expected_runs_per_month(str(raw.get("CronExpression") or "")),
        }
        for raw in resultado.items
    }


def _runs(
    databrew_client, name: str, window: AnalysisWindow, gaps: list[str] | None = None
) -> list[dict]:
    """Execuções de **um** job. Negado aqui não zera os outros jobs."""
    resultado = safe_pages(databrew_client, "list_job_runs", "JobRuns", Name=name)
    if gaps is not None and not resultado.complete:
        gaps.append(f"list_job_runs: {resultado.error_category or 'incompleto'}")
    out: list[dict] = []
    for run in resultado.items:
        created = run.get("CreatedOn")
        if isinstance(created, datetime):
            normalized = created.replace(tzinfo=created.tzinfo or timezone.utc)
            if not window.contains(normalized):
                continue
        out.append(run)
    return out
