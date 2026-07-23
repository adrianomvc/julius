"""Coletor de Glue Jobs: get_jobs + get_job_runs → GlueJob.

Extrai config (worker type, autoscaling, FLEX, bookmarks, versão, timeout) do
GetJob e agrega o histórico de execuções (duração, recorrência, taxa de falha)
do GetJobRuns. `avg_cpu_load` fica None aqui (vem do CloudWatch — coletor à parte),
então as regras de capacidade só disparam quando essa métrica for adicionada.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean

from julius.inventory.model import GlueJob, Table

_FAILED_STATES = {"FAILED", "TIMEOUT", "ERROR"}


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def collect_jobs(glue_client, *, lookback_days: int = 90, now: datetime | None = None) -> list[GlueJob]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    months = max(1.0, lookback_days / 30.0)

    jobs: list[GlueJob] = []
    paginator = glue_client.get_paginator("get_jobs")
    for page in paginator.paginate():
        for job in page.get("Jobs", []):
            jobs.append(_build_job(glue_client, job, cutoff, months))
    return jobs


def _build_job(glue_client, job: dict, cutoff: datetime, months: float) -> GlueJob:
    name = job["Name"]
    args = job.get("DefaultArguments", {}) or {}
    runs = _job_runs(glue_client, name, cutoff)

    completed = [r for r in runs if r.get("JobRunState") == "SUCCEEDED"]
    failed = [r for r in runs if r.get("JobRunState") in _FAILED_STATES]
    exec_times = [r.get("ExecutionTime", 0) for r in completed if r.get("ExecutionTime")]
    failed_times = [r.get("ExecutionTime", 0) for r in failed if r.get("ExecutionTime")]

    total = len(runs)
    return GlueJob(
        name=name,
        glue_version=str(job.get("GlueVersion", "0.9")),
        worker_type=job.get("WorkerType", "G.1X"),
        number_of_workers=int(job.get("NumberOfWorkers", 10) or 10),
        auto_scaling=_truthy(args.get("--enable-auto-scaling")),
        execution_class=job.get("ExecutionClass", "STANDARD"),
        job_bookmark=str(args.get("--job-bookmark-option", "")) == "job-bookmark-enable",
        timeout_min=int(job.get("Timeout", 2880) or 2880),
        max_retries=int(job.get("MaxRetries", 0) or 0),
        runs_per_month=round(total / months, 1),
        avg_execution_sec=round(mean(exec_times), 1) if exec_times else 0.0,
        avg_failed_execution_sec=round(mean(failed_times), 1) if failed_times else 0.0,
        failure_rate=round(len(failed) / total, 3) if total else 0.0,
        avg_cpu_load=None,  # requer CloudWatch (coletor à parte)
        observed_runs=total,
        coverage_days=int(months * 30),
        owner_tag=(job.get("Tags", {}) or {}).get("Owner"),
        script_location=(job.get("Command", {}) or {}).get("ScriptLocation"),
        reads_tables=_table_args(
            args, "--source_table", "--input_table", "--reads_table"
        ),
        writes_tables=_table_args(
            args, "--target_table", "--output_table", "--writes_table"
        ),
    )


def _job_runs(glue_client, name: str, cutoff: datetime) -> list[dict]:
    runs: list[dict] = []
    paginator = glue_client.get_paginator("get_job_runs")
    for page in paginator.paginate(JobName=name):
        for run in page.get("JobRuns", []):
            started = run.get("StartedOn")
            if started and started.replace(tzinfo=started.tzinfo or timezone.utc) < cutoff:
                return runs  # execuções vêm mais recentes primeiro
            runs.append(run)
    return runs


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
