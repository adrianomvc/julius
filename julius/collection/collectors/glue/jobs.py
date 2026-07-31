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

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from statistics import mean, median, pstdev

from julius.collection.collectors.paginate import safe_pages
from julius.collection.models import GlueJob, Table
from julius.collection.session import GLUE_RUN_HISTORY_WORKERS
from julius.collection.settings import DPU_PER_WORKER
from julius.collection.window import AnalysisWindow

_FAILED_STATES = {"FAILED", "TIMEOUT", "ERROR"}
_FAILURE_MARKERS = (
    ("out_of_memory", ("outofmemory", "out of memory", "memory limit", "heap space")),
    (
        "permission_denied",
        ("accessdenied", "access denied", "not authorized", "permission denied"),
    ),
    ("dependency_failure", ("dependency", "connection refused", "unavailable")),
    (
        "data_error",
        ("analysisexception", "schema", "malformed", "parse", "invalid data"),
    ),
    (
        "infrastructure_error",
        ("internal service", "internal error", "resource unavailable", "network"),
    ),
)


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def collect_jobs(
    glue_client,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
    workers: int = GLUE_RUN_HISTORY_WORKERS,
) -> list[GlueJob]:
    """Inventário de jobs, cada um com o histórico de execuções da janela.

    O `GetJobRuns` é uma chamada paginada **por job**. Em série, uma conta com
    trezentos jobs paga trezentas latências antes de a fonte terminar, e ela é
    fonte obrigatória — nada mais começa. `workers` pagina vários jobs ao mesmo
    tempo; clientes botocore são seguros entre threads depois de criados, e o
    teto útil é o `max_pool_connections` da sessão.

    `pool.map` preserva a ordem da entrada: dois scans do mesmo inventário
    produzem o mesmo dataset, e o diff entre eles continua legível. Com um job
    só o caminho sequencial evita montar pool para nada.
    """
    resultado = safe_pages(glue_client, "get_jobs", "Jobs")
    if gaps is not None and not resultado.complete:
        gaps.append(f"get_jobs: {resultado.error_category or 'incompleto'}")
    jobs = resultado.items
    if workers <= 1 or len(jobs) <= 1:
        return [_build_job(glue_client, job, window) for job in jobs]

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        return list(
            pool.map(lambda job: _build_job(glue_client, job, window), jobs)
        )


def _build_job(glue_client, job: dict, window: AnalysisWindow) -> GlueJob:
    name = job["Name"]
    args = job.get("DefaultArguments", {}) or {}
    command_type = str((job.get("Command", {}) or {}).get("Name") or "glueetl")
    # O histórico de execução é permissão à parte da listagem, e pode ser negado
    # em **um** job — política de recurso, Lake Formation, tag de restrição. Sem
    # este isolamento esse job derruba `collect_jobs`, que é a fonte obrigatória,
    # e a conta inteira fica sem scan por causa de um recurso.
    runs, historico_lido, last_run_at = _job_runs_isolados(
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
    failed_dpu_seconds = 0.0
    estimated_failed_dpu_hours = 0.0
    failure_categories: dict[str, int] = {}
    worker_type = job.get("WorkerType")
    number_of_workers = _optional_int(job.get("NumberOfWorkers"))
    max_capacity = _optional_float(job.get("MaxCapacity"))
    for run in window_runs:
        is_failed = run.get("JobRunState") in _FAILED_STATES
        if is_failed:
            category = _failure_category(run)
            failure_categories[category] = failure_categories.get(category, 0) + 1
        if command_type == "gluestreaming":
            capacity = _run_capacity(
                run, worker_type, number_of_workers, max_capacity
            )
            run_hours = (
                window.overlap_seconds(run.get("StartedOn"), run.get("CompletedOn"))
                * capacity
                / 3600.0
            )
            estimated_dpu_hours += run_hours
            if is_failed:
                estimated_failed_dpu_hours += run_hours
            continue
        reported = _optional_float(run.get("DPUSeconds"))
        if reported is not None:
            reported = max(0.0, reported)
            dpu_seconds += reported
            if is_failed:
                failed_dpu_seconds += reported
            continue
        execution_sec = _billable_execution_seconds(
            str(job.get("GlueVersion", "0.9")),
            float(run.get("ExecutionTime", 0) or 0),
        )
        capacity = _run_capacity(run, worker_type, number_of_workers, max_capacity)
        run_hours = max(0.0, execution_sec * capacity / 3600.0)
        estimated_dpu_hours += run_hours
        if is_failed:
            estimated_failed_dpu_hours += run_hours

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
            (job.get("ExecutionProperty") or {}).get("MaxConcurrentRuns") or 1
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
        run_history_available=historico_lido,
        created_at=_iso(job.get("CreatedOn")),
        last_modified_at=_iso(job.get("LastModifiedOn")),
        last_run_at=last_run_at,
        observability_enabled=_truthy(
            args.get("--enable-observability-metrics")
        ),
        continuous_logging_enabled=(
            _glue_version(job) >= 5.0
            or _truthy(args.get("--enable-continuous-cloudwatch-log"))
        ),
        metrics_enabled=(
            "--enable-metrics" in args
            and str(args.get("--enable-metrics", "")).lower() != "false"
        ),
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
        failed_runs_in_window=sum(
            1 for run in window_runs if run.get("JobRunState") in _FAILED_STATES
        ),
        failed_retry_runs_in_window=sum(
            1
            for run in window_runs
            if run.get("PreviousRunId") and run.get("JobRunState") in _FAILED_STATES
        ),
        failed_dpu_seconds_window=round(failed_dpu_seconds, 3),
        estimated_failed_dpu_hours_window=round(
            estimated_failed_dpu_hours, 4
        ),
        failure_categories=dict(sorted(failure_categories.items())),
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


def _failure_category(run: dict) -> str:
    """Classifica a causa sem persistir a mensagem potencialmente sensível."""
    if str(run.get("JobRunState") or "").upper() == "TIMEOUT":
        return "timeout"
    message = str(run.get("ErrorMessage") or "").lower()
    for category, markers in _FAILURE_MARKERS:
        if any(marker in message for marker in markers):
            return category
    return "unknown"


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value) -> float | None:
    return float(value) if value is not None else None


def _as_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _iso(value) -> str:
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed is not None else ""


def _glue_version(job: dict) -> float:
    try:
        return float(job.get("GlueVersion") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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


def _job_runs_isolados(
    glue_client,
    name: str,
    cutoff: datetime,
    *,
    include_overlapping: bool = False,
) -> tuple[list[dict], bool, str]:
    """As execuções do job, e se elas puderam ser lidas.

    Devolve `(runs, False, "")` em vez de propagar: a configuração do job veio do
    `GetJobs` e continua válida, só o histórico é que falta. Zero execução
    porque ninguém rodou e zero execução porque não deu para ler são coisas
    diferentes, e o segundo caso precisa chegar ao relatório dizendo isso.
    """
    try:
        runs, last_run_at = _job_runs(
            glue_client, name, cutoff, include_overlapping=include_overlapping
        )
        return runs, True, last_run_at
    except Exception:
        return [], False, ""


def _job_runs(
    glue_client,
    name: str,
    cutoff: datetime,
    *,
    include_overlapping: bool = False,
) -> tuple[list[dict], str]:
    runs: list[dict] = []
    last_run_at = ""
    paginator = glue_client.get_paginator("get_job_runs")
    for page in paginator.paginate(JobName=name):
        for run in page.get("JobRuns", []):
            started = _as_utc(run.get("StartedOn"))
            if not last_run_at and started is not None:
                last_run_at = started.isoformat()
            if started and started < cutoff:
                completed = _as_utc(run.get("CompletedOn"))
                if include_overlapping and (completed is None or completed >= cutoff):
                    runs.append(run)
                    continue
                if include_overlapping:
                    continue
                return runs, last_run_at  # execuções vêm mais recentes primeiro
            runs.append(run)
    return runs, last_run_at


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


def list_database_names(glue_client) -> list[str]:
    """Os nomes dos bancos do catálogo, sem ler tabela nenhuma.

    Separado de `collect_tables` porque é entre os dois que o escopo decide: um
    `get_tables` por banco é o que custa, e o banco de outra conta não deve
    chegar lá.
    """
    return [
        str(database["Name"])
        for database in safe_pages(glue_client, "get_databases", "DatabaseList").items
        if database.get("Name")
    ]


def collect_tables(
    glue_client, databases: Sequence[str], *, gaps: list[str] | None = None
) -> list[Table]:
    """Coleta tabelas dos bancos informados, com ownership/linhagem.

    O `get_tables` é isolado **por banco**: sob Lake Formation é comum a conta
    enxergar o banco no catálogo e não ter permissão de listar as tabelas dele.
    Antes um único banco negado zerava o catálogo inteiro, e todas as regras de
    tabela e de S3 — cujo escopo sai da `location` daqui — ficavam sem inventário.
    """
    tables: list[Table] = []
    for db_name in databases:
        resultado = safe_pages(
            glue_client, "get_tables", "TableList", DatabaseName=db_name
        )
        if gaps is not None and not resultado.complete:
            gaps.append(
                f"get_tables[{db_name}]: {resultado.error_category or 'incompleto'}"
            )
        for raw in resultado.items:
            params = raw.get("Parameters", {}) or {}
            descriptor = raw.get("StorageDescriptor") or {}
            tables.append(
                Table(
                    name=f"{db_name}.{raw['Name']}",
                    location=str(descriptor.get("Location") or ""),
                    written_by=params.get("julius:written_by")
                    or params.get("written_by")
                    or params.get("producer_job"),
                    owner_tag=params.get("Owner") or params.get("owner"),
                    corporate_owner=params.get("corporate_owner"),
                    datawarm_owner=params.get("datawarm_owner"),
                    datawarm_published=_truthy(params.get("datawarm_published")),
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
