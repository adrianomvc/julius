"""Ativos do portfólio Glue, medidos na janela de análise."""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.models.window_math import monthly_factor
from julius.collection.settings import ANALYSIS_WINDOW_DAYS, DPU_PER_WORKER


@dataclass
class GlueJob:
    name: str
    glue_version: str = "3.0"
    job_mode: str = "SCRIPT"
    command_type: str = "glueetl"
    worker_type: str | None = None
    number_of_workers: int | None = None
    max_capacity: float | None = None
    auto_scaling: bool = False
    execution_class: str = "STANDARD"
    job_bookmark: bool = False
    timeout_min: int = 2880
    max_concurrent_runs: int = 1
    job_run_queuing_enabled: bool = False
    avg_execution_sec: float = 0.0
    p50_execution_sec: float = 0.0
    p95_execution_sec: float = 0.0
    max_execution_sec: float = 0.0
    execution_stddev_sec: float = 0.0
    avg_cpu_load: float | None = None
    avg_worker_utilization: float | None = None
    max_memory_used_pct: float | None = None
    max_disk_used_pct: float | None = None
    max_task_skew: float | None = None
    avg_all_executors: float | None = None
    avg_max_needed_executors: float | None = None
    #: Resultado importado de um benchmark humano controlado. O Julius
    #: recomenda candidatos, mas nunca executa nem altera o job.
    rightsize_tested_workers: int | None = None
    rightsize_test_runs: int = 0
    rightsize_output_validated: bool = False
    shuffle_spill_bytes: float | None = None
    shuffle_read_bytes: float | None = None
    shuffle_write_bytes: float | None = None
    bytes_read_window: float | None = None
    #: Parcela comprovadamente relida sem necessidade na janela. Não é
    #: inferida do total: permanece `None` até existir evidência de entrada nova
    #: versus já processada.
    redundant_read_bytes_window: float | None = None
    #: Bytes escritos na janela. Nenhuma regra o lê direto, e isso é correto: o
    #: Glue cobra DPU-hora, não bytes escritos, então não há afirmação
    #: financeira a fazer com ele sozinho. Quem o consome é
    #: `average_output_file_bytes`, logo abaixo, que decide se o job produz
    #: arquivo pequeno — e essa, sim, é cobrança de request a jusante.
    bytes_written_window: float | None = None
    files_written_window: float | None = None
    streaming_records_window: float | None = None
    has_spill_evidence: bool = False
    spark_event_log_objects_scanned: int = 0
    spark_event_log_evidence_complete: bool = False
    spark_event_logs_path: str | None = None
    incremental_source_evidence: bool = False
    dpu_seconds_window: float = 0.0
    estimated_dpu_hours_window: float = 0.0
    runs_in_window: int = 0
    active_seconds_window: float = 0.0
    overlap_seconds_window: float = 0.0
    overlapping_runs_in_window: int = 0
    retry_runs_in_window: int = 0
    # `None` distingue dataset legado/coleta indisponível de zero falhas.
    failed_runs_in_window: int | None = None
    failed_retry_runs_in_window: int | None = None
    failed_dpu_seconds_window: float | None = None
    estimated_failed_dpu_hours_window: float | None = None
    failure_categories: dict[str, int] = field(default_factory=dict)
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    # Custo alocado por rateio da cobrança real do bucket no Cost Explorer.
    # Nunca é fatura por job: o CE não expõe dimensão de recurso para Glue.
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    # Parcela do custo alocado acima consumida por execuções com falha.
    # Continua contida em `allocated_cost`; não deve ser somada ao custo total.
    failed_cost_window: float | None = None
    failure_cost_quality: str = "unavailable"
    trigger_names: list[str] = field(default_factory=list)
    run_ids_in_window: list[str] = field(default_factory=list)
    observed_runs: int = 0
    coverage_days: int = 0
    #: Falso quando `GetJobRuns` foi negado para **este** job — política de
    #: recurso, Lake Formation, tag de restrição. A configuração continua válida
    #: (veio do `GetJobs`); o histórico é que não existe. Sem este campo,
    #: "ninguém rodou" e "não deu para ler" viram o mesmo zero, e o segundo
    #: viraria conclusão sobre um job que não foi medido.
    run_history_available: bool = True
    created_at: str = ""
    last_modified_at: str = ""
    last_run_at: str = ""
    observability_enabled: bool | None = None
    continuous_logging_enabled: bool | None = None
    metrics_enabled: bool | None = None
    owner_tag: str | None = None
    script_location: str | None = None
    default_argument_keys: list[str] = field(default_factory=list)
    connection_names: list[str] = field(default_factory=list)
    # True/False = sensível a tempo (SLA); None = desconhecido. FLEX só p/ False.
    time_sensitive: bool | None = None
    # Confiabilidade: fração de execuções (incl. retries) que falham, e o compute
    # médio gasto até a falha (Glue cobra DPU-hora mesmo em execução que falha).
    failure_rate: float = 0.0
    avg_failed_execution_sec: float = 0.0
    max_retries: int = 0
    reads_tables: list[str] = field(default_factory=list)
    writes_tables: list[str] = field(default_factory=list)

    @property
    def glue_version_num(self) -> float:
        try:
            return float(self.glue_version)
        except (TypeError, ValueError):
            return 99.0

    @property
    def dpu_per_worker(self) -> float:
        return DPU_PER_WORKER.get(self.worker_type or "", 0.0)

    @property
    def configured_dpu(self) -> float:
        if self.max_capacity is not None:
            return max(0.0, self.max_capacity)
        return max(0, self.number_of_workers or 0) * self.dpu_per_worker

    @property
    def capacity_unit(self) -> str:
        return "M-DPU" if self.command_type == "glueray" else "DPU"

    @property
    def actual_dpu_hours_window(self) -> float:
        return max(0.0, self.dpu_seconds_window / 3600.0)

    @property
    def total_dpu_hours_window(self) -> float:
        return self.actual_dpu_hours_window + max(0.0, self.estimated_dpu_hours_window)

    @property
    def actual_failed_dpu_hours_window(self) -> float:
        return max(0.0, (self.failed_dpu_seconds_window or 0.0) / 3600.0)

    @property
    def total_failed_dpu_hours_window(self) -> float:
        return self.actual_failed_dpu_hours_window + max(
            0.0, self.estimated_failed_dpu_hours_window or 0.0
        )

    @property
    def monthly_factor(self) -> float:
        return monthly_factor(self.window_days)

    @property
    def modeled_window_dpu_hours(self) -> float:
        """Consumo por duração × capacidade, quando a AWS não reportou DPU."""
        return self.runs_in_window * self.configured_dpu * (
            self.avg_execution_sec / 3600.0
        )

    @property
    def window_dpu_hours(self) -> float:
        """DPU-hora **realizada** na janela. Medida, nunca extrapolada."""
        if self.total_dpu_hours_window > 0:
            return self.total_dpu_hours_window
        return self.modeled_window_dpu_hours

    @property
    def monthly_dpu_hours(self) -> float:
        """A mesma DPU-hora expressa por mês, para os modelos financeiros."""
        return self.window_dpu_hours * self.monthly_factor

    @property
    def monthly_failed_cost(self) -> float | None:
        if self.failed_cost_window is None:
            return None
        return max(0.0, self.failed_cost_window) * self.monthly_factor

    @property
    def runs_per_month(self) -> float:
        return round(self.runs_in_window * self.monthly_factor, 1)

    @property
    def average_output_file_bytes(self) -> float | None:
        if not self.files_written_window or self.bytes_written_window is None:
            return None
        return self.bytes_written_window / self.files_written_window


@dataclass
class InteractiveSession:
    session_id: str
    dpu: float = 5
    worker_type: str | None = None
    number_of_workers: int | None = None
    max_capacity: float | None = None
    glue_version: str = ""
    idle_timeout_min: int = 2880
    status: str = "READY"
    idle_hours_per_day: float = 0.0
    active_days_per_month: int = 22
    observed_runs: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    created_on: str = ""
    completed_on: str = ""
    execution_time_sec: float = 0.0
    dpu_seconds: float = 0.0
    dpu_seconds_window: float | None = None
    estimated_dpu_hours_window: float = 0.0
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    last_activity_at: str = ""
    activity_evidence: bool = False
    statement_ids: list[str] = field(default_factory=list)

    @property
    def actual_dpu_hours_window(self) -> float:
        seconds = (
            self.dpu_seconds
            if self.dpu_seconds_window is None
            else self.dpu_seconds_window
        )
        return max(0.0, seconds / 3600.0)

    @property
    def dpu_hours(self) -> float:
        return self.actual_dpu_hours_window + max(0.0, self.estimated_dpu_hours_window)

    @property
    def monthly_factor(self) -> float:
        return monthly_factor(self.window_days)

    @property
    def monthly_dpu_hours(self) -> float:
        return self.dpu_hours * self.monthly_factor


@dataclass
class GlueCrawler:
    name: str
    state: str = "READY"
    last_crawl_status: str = ""
    last_crawl_started_at: str = ""
    last_error: str = ""
    schedule_expression: str = ""
    schedule_state: str = "NOT_SCHEDULED"
    database_name: str = ""
    median_runtime_sec: float = 0.0
    last_runtime_sec: float = 0.0
    tables_created: int = 0
    tables_updated: int = 0
    tables_deleted: int = 0
    runs_in_window: int = 0
    failures_in_window: int = 0
    dpu_hours_window: float = 0.0
    owner_tag: str | None = None
    crawl_ids_in_window: list[str] = field(default_factory=list)
    expected_runs_monthly: float | None = None
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    coverage_days: int = 0
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"
    recrawl_behavior: str = "CRAWL_EVERYTHING"

    @property
    def monthly_factor(self) -> float:
        return monthly_factor(self.window_days)

    @property
    def monthly_dpu_hours(self) -> float:
        return self.dpu_hours_window * self.monthly_factor

    @property
    def expected_runs_in_window(self) -> float | None:
        """Execuções esperadas pelo agendamento **na janela**.

        Comparar execuções da janela com uma expectativa mensal media coisas
        diferentes; a expectativa é trazida para o mesmo período.
        """
        if self.expected_runs_monthly is None:
            return None
        return self.expected_runs_monthly / self.monthly_factor


@dataclass
class GlueTrigger:
    name: str
    trigger_type: str = "ON_DEMAND"
    state: str = ""
    schedule_expression: str = ""
    workflow_name: str = ""
    job_names: list[str] = field(default_factory=list)
    crawler_names: list[str] = field(default_factory=list)
    owner_tag: str | None = None
    expected_runs_monthly: float | None = None


@dataclass
class DataBrewJob:
    name: str
    job_type: str = "RECIPE"
    max_capacity: int = 5
    timeout_min: int = 2880
    max_retries: int = 0
    schedule_names: list[str] = field(default_factory=list)
    runs_in_window: int = 0
    failures_in_window: int = 0
    execution_hours_window: float = 0.0
    estimated_node_hours_window: float = 0.0
    owner_tag: str | None = None
    run_ids_in_window: list[str] = field(default_factory=list)
    expected_runs_monthly: float | None = None
    window_end: str = ""
    window_days: int = ANALYSIS_WINDOW_DAYS
    coverage_days: int = 0
    allocated_cost: float | None = None
    cost_quality: str = "unavailable"

    @property
    def monthly_factor(self) -> float:
        return monthly_factor(self.window_days)

    @property
    def monthly_node_hours(self) -> float:
        return self.estimated_node_hours_window * self.monthly_factor

    @property
    def expected_runs_in_window(self) -> float | None:
        """Execuções esperadas pelo agendamento **na janela**."""
        if self.expected_runs_monthly is None:
            return None
        return self.expected_runs_monthly / self.monthly_factor
