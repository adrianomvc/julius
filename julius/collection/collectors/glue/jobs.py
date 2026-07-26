"""Coletor de Glue Jobs: get_jobs + get_job_runs → GlueJob.

Extrai config (worker type, autoscaling, FLEX, bookmarks, versão, timeout) do
GetJob e agrega o histórico de execuções (duração, recorrência, taxa de falha)
do GetJobRuns. `avg_cpu_load` fica None aqui (vem do CloudWatch — coletor à parte),
então as regras de capacidade só disparam quando essa métrica for adicionada.

Comportamento e consumo saem da mesma janela de análise: antes as métricas de
duração vinham de 90 dias enquanto a DPU-hora vinha do mês corrente, e as duas
eram usadas lado a lado como se cobrissem o mesmo período.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, median, pstdev

from julius.collection.models import GlueJob, Table
from julius.collection.settings import DPU_PER_WORKER
from julius.collection.window import AnalysisWindow

_FAILED_STATES = {"FAILED", "TIMEOUT", "ERROR"}


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def collect_jobs(glue_client, *, window: AnalysisWindow) -> list[GlueJob]:
    jobs: list[GlueJob] = []
    paginator = glue_client.get_paginator("get_jobs")
    for page in paginator.paginate():
        for job in page.get("Jobs", []):
            jobs.append(_build_job(glue_client, job, window))
    return jobs


def _build_job(glue_client, job: dict, window: AnalysisWindow) -> GlueJob:
    name = job["Name"]
    args = job.get("DefaultArguments", {}) or {}
    command_type = str((job.get("Command", {}) or {}).get("Name") or "glueetl")
    runs = _job_runs(
        glue_client,
        name,
        window.start,
        include_overlapping=command_type == "gluestreaming",
    )

    completed = [r for r in runs if r.get("JobRunState") == "SUCCEEDED"]
    failed = [r for r in runs if r.get("JobRunState") in _FAILED_STATES]
    exec_times = [r.get("ExecutionTime", 0) for r in completed if r.get("ExecutionTime")]
    failed_times = [r.get("ExecutionTime", 0) for r in failed if r.get("ExecutionTime")]
    if command_type == "gluestreaming":
        # Job contínuo: o que conta é a sobreposição com a janela, não o início.
        window_runs = [
            r
            for r in runs
            if window.overlap_seconds(r.get("StartedOn"), r.get("CompletedOn")) > 0
        ]
    else:
        window_runs = [r for r in runs if window.contains(_as_utc(r.get("StartedOn")))]
    active_seconds, overlap_seconds, overlapping_runs = _interval_stats(
        window_runs, window
    )
    dpu_seconds = 0.0
    estimated_dpu_hours = 0.0
    worker_type = job.get("WorkerType")
    number_of_workers = _optional_int(job.get("NumberOfWorkers"))
    max_capacity = _optional_float(job.get("MaxCapacity"))
    for run in window_runs:
        if command_type == "gluestreaming":
            capacity = _run_capacity(
                run, worker_type, number_of_workers, max_capacity
            )
            estimated_dpu_hours += (
                window.overlap_seconds(run.get("StartedOn"), run.get("CompletedOn"))
                * capacity
                / 3600.0
            )
            continue
        reported = _optional_float(run.get("DPUSeconds"))
        if reported is not None:
            dpu_seconds += max(0.0, reported)
            continue
        execution_sec = _billable_execution_seconds(
            str(job.get("GlueVersion", "0.9")),
            float(run.get("ExecutionTime", 0) or 0),
        )
        capacity = _run_capacity(run, worker_type, number_of_workers, max_capacity)
        estimated_dpu_hours += max(0.0, execution_sec * capacity / 3600.0)

    total = len(runs)
    return GlueJob(
        name=name,
        glue_version=str(job.get("GlueVersion", "0.9")),
        job_mode=str(job.get("JobMode") or "SCRIPT"),
        command_type=command_type,
        worker_type=worker_type,
        number_of_workers=number_of_workers,
        max_capacity=max_capacity,
        auto_scaling=_truthy(args.get("--enable-auto-scaling")),
        execution_class=job.get("ExecutionClass", "STANDARD"),
        job_bookmark=str(args.get("--job-bookmark-option", "")) == "job-bookmark-enable",
        timeout_min=int(job.get("Timeout", 2880) or 2880),
        max_retries=int(job.get("MaxRetries", 0) or 0),
        max_concurrent_runs=int(
            ((job.get("ExecutionProperty") or {}).get("MaxConcurrentRuns") or 1)
        ),
        job_run_queuing_enabled=bool(
            job.get("JobRunQueuingEnabled")
            or any(run.get("JobRunQueuingEnabled") for run in window_runs)
        ),
        avg_execution_sec=round(mean(exec_times), 1) if exec_times else 0.0,
        p50_execution_sec=round(median(exec_times), 1) if exec_times else 0.0,
        p95_execution_sec=round(_percentile(exec_times, 0.95), 1) if exec_times else 0.0,
        max_execution_sec=round(max(exec_times), 1) if exec_times else 0.0,
        execution_stddev_sec=round(pstdev(exec_times), 1) if len(exec_times) > 1 else 0.0,
        avg_failed_execution_sec=round(mean(failed_times), 1) if failed_times else 0.0,
        failure_rate=round(len(failed) / total, 3) if total else 0.0,
        avg_cpu_load=None,  # requer CloudWatch (coletor à parte)
        observed_runs=total,
        coverage_days=window.days,
        window_days=window.days,
        dpu_seconds_window=round(dpu_seconds, 3),
        estimated_dpu_hours_window=round(estimated_dpu_hours, 4),
        runs_in_window=len(window_runs),
        active_seconds_window=round(active_seconds, 3),
        overlap_seconds_window=round(overlap_seconds, 3),
        overlapping_runs_in_window=overlapping_runs,
        retry_runs_in_window=sum(
            1 for run in window_runs if run.get("PreviousRunId")
        ),
        window_end=window.data_through.isoformat(),
        trigger_names=sorted(
            {
                str(r["TriggerName"])
                for r in runs
                if r.get("TriggerName")
            }
        ),
        run_ids_in_window=sorted(
            str(r["Id"]) for r in window_runs if r.get("Id")
        ),
        owner_tag=(job.get("Tags", {}) or {}).get("Owner"),
            script_location=(job.get("Command", {}) or {}).get("ScriptLocation"),
            default_argument_keys=sorted(
                str(key) for key in (job.get("DefaultArguments") or {})
            ),
            connection_names=sorted(
                str(name)
                for name in ((job.get("Connections") or {}).get("Connections") or [])
            ),
        spark_event_logs_path=args.get("--spark-event-logs-path"),
        reads_tables=_table_args(
            args, "--source_table", "--input_table", "--reads_table"
        ),
        writes_tables=_table_args(
            args, "--target_table", "--output_table", "--writes_table"
        ),
    )


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value) -> float | None:
    return float(value) if value is not None else None


def _as_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _billable_execution_seconds(glue_version: str, execution_sec: float) -> float:
    try:
        version = float(glue_version)
    except (TypeError, ValueError):
        version = 99.0
    minimum = 600.0 if version < 2.0 else 60.0
    return max(minimum, execution_sec)


def _run_capacity(
    run: dict,
    default_worker_type: str | None,
    default_workers: int | None,
    default_max_capacity: float | None,
) -> float:
    max_capacity = _optional_float(run.get("MaxCapacity"))
    if max_capacity is not None:
        return max(0.0, max_capacity)
    worker_type = run.get("WorkerType") or default_worker_type
    workers = _optional_int(run.get("NumberOfWorkers"))
    if workers is None:
        workers = default_workers
    if worker_type and workers is not None:
        return max(0.0, DPU_PER_WORKER.get(str(worker_type), 0.0) * workers)
    return max(0.0, default_max_capacity or 0.0)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def _job_runs(
    glue_client,
    name: str,
    cutoff: datetime,
    *,
    include_overlapping: bool = False,
) -> list[dict]:
    runs: list[dict] = []
    paginator = glue_client.get_paginator("get_job_runs")
    for page in paginator.paginate(JobName=name):
        for run in page.get("JobRuns", []):
            started = _as_utc(run.get("StartedOn"))
            if started and started < cutoff:
                completed = _as_utc(run.get("CompletedOn"))
                if include_overlapping and (completed is None or completed >= cutoff):
                    runs.append(run)
                    continue
                if include_overlapping:
                    continue
                return runs  # execuções vêm mais recentes primeiro
            runs.append(run)
    return runs


def _interval_stats(
    runs: list[dict],
    window: AnalysisWindow,
) -> tuple[float, float, int]:
    """Tempo ativo, tempo com concorrência e nº de runs que se sobrepõem."""
    intervals: list[tuple[datetime, datetime, str]] = []
    for index, run in enumerate(runs):
        started = _as_utc(run.get("StartedOn"))
        if started is None:
            continue
        completed = _as_utc(run.get("CompletedOn")) or window.end
        start = max(started, window.start)
        end = min(completed, window.end)
        if end <= start:
            continue
        intervals.append((start, end, str(run.get("Id") or index)))

    participants: set[str] = set()
    for index, (start, end, run_id) in enumerate(intervals):
        for other_start, other_end, other_id in intervals[index + 1 :]:
            if max(start, other_start) < min(end, other_end):
                participants.update((run_id, other_id))

    events = sorted(
        [
            event
            for start, end, _run_id in intervals
            for event in ((start, 1), (end, -1))
        ],
        key=lambda item: (item[0], item[1]),
    )
    active = 0
    previous: datetime | None = None
    active_seconds = 0.0
    overlap_seconds = 0.0
    for instant, delta in events:
        if previous is not None and instant > previous:
            seconds = (instant - previous).total_seconds()
            if active > 0:
                active_seconds += seconds
            if active > 1:
                overlap_seconds += seconds
        active += delta
        previous = instant
    return active_seconds, overlap_seconds, len(participants)


def collect_tables(glue_client) -> list[Table]:
    """Coleta tabelas do Glue Catalog e metadados de ownership/linhagem."""
    tables: list[Table] = []
    databases = glue_client.get_paginator("get_databases")
    for db_page in databases.paginate():
        for database in db_page.get("DatabaseList", []):
            db_name = database["Name"]
            paginator = glue_client.get_paginator("get_tables")
            for page in paginator.paginate(DatabaseName=db_name):
                for raw in page.get("TableList", []):
                    params = raw.get("Parameters", {}) or {}
                    name = f"{db_name}.{raw['Name']}"
                    tables.append(
                        Table(
                            name=name,
                            written_by=params.get("julius:written_by")
                            or params.get("written_by")
                            or params.get("producer_job"),
                            owner_tag=params.get("Owner") or params.get("owner"),
                            corporate_owner=params.get("corporate_owner"),
                            datawarm_owner=params.get("datawarm_owner"),
                            datawarm_published=_truthy(
                                params.get("datawarm_published")
                            ),
                        )
                    )
    return tables


def _table_args(args: dict, *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = args.get(key)
        if not raw:
            continue
        values.extend(part.strip() for part in str(raw).split(",") if part.strip())
    return list(dict.fromkeys(values))
