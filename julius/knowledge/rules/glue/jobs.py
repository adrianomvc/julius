"""Detectores de Glue Jobs: (1) sem Auto Scaling, (2) workers superdimensionados."""

from __future__ import annotations

from julius.collection.models import Account, GlueJob
from julius.config import UNATTRIBUTED_GLUE_BUCKETS, Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal
from julius.knowledge.rules.glue import estimation as glue_est

_DOC_AUTOSCALING = "https://docs.aws.amazon.com/glue/latest/dg/auto-scaling.html"
_DOC_WORKERS = "https://docs.aws.amazon.com/glue/latest/dg/monitor-spark-ui.html"
_DOC_VERSION = "https://docs.aws.amazon.com/glue/latest/dg/release-notes.html"
_DOC_FLEX = "https://docs.aws.amazon.com/glue/latest/dg/aws-glue-api-jobs-runs.html"
_DOC_BOOKMARK = "https://docs.aws.amazon.com/glue/latest/dg/monitor-continuations.html"
_DOC_MONITORING = "https://docs.aws.amazon.com/glue/latest/dg/monitor-glue.html"
_DOC_STREAMING = "https://docs.aws.amazon.com/glue/latest/dg/glue-streaming-monitoring-metrics.html"
_DOC_JOB_PROPERTIES = "https://docs.aws.amazon.com/glue/latest/dg/add-job.html"
_DOC_COST_EXPLORER = (
    "https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html"
)

_AUTOSCALE_TYPES = {
    "G.1X",
    "G.2X",
    "G.4X",
    "G.8X",
    "G.12X",
    "G.16X",
    "R.1X",
    "R.2X",
    "R.4X",
    "R.8X",
}
_DOWNGRADE_TYPES = {"G.4X", "G.8X", "G.12X", "G.16X", "R.2X", "R.4X", "R.8X"}


def _flex_candidate(job: GlueJob) -> bool:
    """O que é fato num candidato a FLEX.

    `time_sensitive` ficava aqui e matava a regra: nada no código escreve esse
    campo, e o gate exigia `is False`. Tolerar início adiado é propriedade do
    SLA, não do recurso — a AWS não tem como informar. O que sobra é fato, e a
    economia é diferença de tarifa, calculável.
    """
    return (
        job.execution_class == "STANDARD"
        and job.runs_per_month > 0
        and job.glue_version_num >= 3.0
        and job.command_type == "glueetl"
    )


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    for job in account.glue_jobs:
        utilization = (
            job.avg_worker_utilization
            if job.avg_worker_utilization is not None
            else job.avg_cpu_load
        )
        low_cpu = utilization is not None and utilization < config.thresholds.low_cpu
        capacity_evidence = _capacity_evidence(job, config)

        # Capacidade: Auto Scaling (sem autoscaling) OU redução de workers (demais casos).
        autoscaling_eligible = (
            job.glue_version_num >= 3.0
            and job.command_type in {"glueetl", "gluestreaming"}
            and job.worker_type in _AUTOSCALE_TYPES
        )
        if not job.auto_scaling and autoscaling_eligible and low_cpu:
            out.append(_autoscaling(account, job, config, scan_id, capacity_evidence))
        elif low_cpu and (job.number_of_workers or 0) > config.thresholds.session_min_dpu:
            out.append(_overprovisioned(account, job, config, scan_id, capacity_evidence))

        # Versão antiga: fatura em blocos de 10 min. Abaixo de 2.0 o desperdício
        # é aritmético; entre 2.0 e a preferencial só o script diz se migrar
        # compensa, então aquele caso vira sinal em `signals()`.
        if job.command_type == "glueetl" and job.glue_version_num < 2.0:
            out.append(_version(account, job, config, scan_id))

        # FLEX: job em batch ainda em STANDARD. A tolerância a início adiado
        # não sai do plano de controle — ver `_flex_candidate`.
        if _flex_candidate(job):
            out.append(_flex(account, job, config, scan_id))

        # Bookmarks desligados num job recorrente: reprocessa dados antigos.
        if not job.job_bookmark and job.runs_per_month >= 8 and job.command_type == "glueetl":
            out.append(
                _bookmark(
                    account,
                    job,
                    config,
                    scan_id,
                    job.incremental_source_evidence,
                )
            )

        # Execuções que falham (e retries) cobram DPU-hora sem entregar.
        if job.failure_rate >= config.thresholds.high_failure_rate and job.runs_per_month > 0:
            out.append(_failing(account, job, config, scan_id))

        # Timeout muito acima da duração média (risco de job pendurado cobrando).
        th = config.thresholds
        exec_min = (job.p95_execution_sec or job.avg_execution_sec) / 60.0
        if (
            exec_min > 0
            and job.timeout_min >= th.timeout_excess_min_minutes
            and job.timeout_min > th.timeout_excess_ratio * exec_min
        ):
            out.append(_timeout(account, job, config, scan_id))

        if (
            job.command_type != "gluestreaming"
            and job.overlapping_runs_in_window >= th.glue_overlapping_runs_min
            and job.overlap_seconds_window > 0
        ):
            out.append(
                _investigation(
                    account,
                    job,
                    config,
                    scan_id,
                    "GLUE-OVERLAPPING-RUNS",
                    "Execuções do mesmo job se sobrepõem",
                    (
                        "Confirmar se a concorrência é intencional e eliminar "
                        "disparos duplicados antes de alterar MaxConcurrentRuns"
                    ),
                    [
                        f"{job.overlapping_runs_in_window} runs participaram de sobreposição",
                        f"{job.overlap_seconds_window / 60:.1f} min com concorrência",
                        f"MaxConcurrentRuns={job.max_concurrent_runs}",
                        f"retries ligados a run anterior={job.retry_runs_in_window}",
                    ],
                    doc_link=_DOC_JOB_PROPERTIES,
                )
            )

        if (
            job.command_type == "gluestreaming"
            and job.streaming_records_window == 0
            and job.active_seconds_window
            >= th.glue_streaming_no_input_min_hours * 3600
        ):
            rate, _quality, _source = glue_est.billing_rate(job, config.pricing)
            baseline = job.monthly_dpu_hours * rate
            out.append(
                _investigation(
                    account,
                    job,
                    config,
                    scan_id,
                    "GLUE-STREAMING-NO-INPUT",
                    "Job streaming permaneceu ativo sem registros observados",
                    (
                        "Validar SLA, fonte e janela; avaliar Auto Scaling, "
                        "capacidade menor ou processamento batch"
                    ),
                    [
                        f"atividade observada={job.active_seconds_window / 3600:.1f}h",
                        "registros de streaming observados=0",
                        f"consumo={job.window_dpu_hours:.2f} DPU-h na janela",
                    ],
                    baseline_cost=baseline,
                    doc_link=_DOC_STREAMING,
                )
            )

        if (
            job.command_type != "gluestreaming"
            and job.bytes_read_window == 0
            and job.window_dpu_hours > 0
        ):
            out.append(
                _investigation(
                    account,
                    job,
                    config,
                    scan_id,
                    "GLUE-NO-INPUT-WASTE",
                    "Job consumiu DPU sem bytes de entrada observados",
                    (
                        "Confirmar fonte vazia, filtros, bookmark e métricas antes "
                        "de revisar o schedule"
                    ),
                    [
                        "bytes lidos observados=0",
                        f"consumo={job.window_dpu_hours:.2f} DPU-h na janela",
                        f"runs na janela={job.runs_in_window}",
                    ],
                    doc_link=_DOC_MONITORING,
                )
            )

        # Worker type grande (G.4X/G.8X) com CPU baixa → type menor bastaria.
        if (
            job.worker_type in _DOWNGRADE_TYPES
            and job.avg_cpu_load is not None
            and job.avg_cpu_load < th.worker_type_low_cpu
        ):
            out.append(_worker_type(account, job, config, scan_id, capacity_evidence))

        duration_cv = (
            job.execution_stddev_sec / job.avg_execution_sec if job.avg_execution_sec > 0 else 0.0
        )
        if (
            job.max_task_skew is not None and job.max_task_skew >= th.task_skew_high
        ) or duration_cv >= th.task_duration_cv_high:
            variance_evidence = []
            if job.max_task_skew is not None:
                variance_evidence.append(f"skewness.job máximo={job.max_task_skew:.2f}")
            variance_evidence.extend(
                [
                    f"duração p50={job.p50_execution_sec:.0f}s; p95={job.p95_execution_sec:.0f}s",
                    f"coeficiente de variação={duration_cv:.0%}",
                ]
            )
            out.append(
                _investigation(
                    account,
                    job,
                    config,
                    scan_id,
                    "GLUE-TASK-SKEW",
                    "Tasks com duração muito desigual",
                    "Revisar stages, joins e particionamento antes de alterar capacidade",
                    variance_evidence,
                )
            )

        if (
            job.avg_all_executors
            and job.avg_max_needed_executors is not None
            and job.avg_all_executors > 0
        ):
            gap = max(
                0.0,
                (job.avg_all_executors - job.avg_max_needed_executors) / job.avg_all_executors,
            )
            if gap >= th.executor_gap_high:
                out.append(
                    _investigation(
                        account,
                        job,
                        config,
                        scan_id,
                        "GLUE-EXECUTOR-CAPACITY-GAP",
                        "Executores disponíveis acima da demanda observada",
                        "Revisar teto de workers e paralelismo por stage",
                        [
                            f"executores totais médios={job.avg_all_executors:.1f}",
                            f"máximo necessário médio={job.avg_max_needed_executors:.1f}",
                            f"gap={gap:.0%}",
                        ],
                    )
                )

        if job.has_spill_evidence and (job.shuffle_spill_bytes or 0) > 0:
            out.append(
                _investigation(
                    account,
                    job,
                    config,
                    scan_id,
                    "GLUE-SHUFFLE-SPILL",
                    "Shuffle com spill para disco",
                    "Otimizar joins/particionamento e avaliar capacidade de memória",
                    [f"shuffle spill={job.shuffle_spill_bytes:.0f} bytes"],
                )
            )

        expected_frequency = sum(
            trigger.expected_runs_monthly or 0.0
            for trigger in account.glue_triggers
            if trigger.name in job.trigger_names or job.name in trigger.job_names
        )
        if expected_frequency > 0:
            deviation = abs(job.runs_per_month - expected_frequency) / expected_frequency
            if deviation >= 0.5:
                out.append(
                    _investigation(
                        account,
                        job,
                        config,
                        scan_id,
                        "GLUE-SCHEDULE-RUN-MISMATCH",
                        "Execuções observadas divergem da frequência agendada",
                        "Reconciliar cron, retries, disparos manuais e execuções reais",
                        [
                            f"esperadas pelo schedule ~{expected_frequency:.1f}/mês",
                            f"observadas ~{job.runs_per_month:.1f}/mês",
                        ],
                        category="inventory_integrity",
                    )
                )
    # A cobrança comparável é a da janela de análise, não a do painel de
    # fatura: aquela cobre o mês-calendário parcial, e a diferença de período
    # apareceria aqui como custo não atribuído.
    coverage = account.glue_cost_coverage
    modeled = sum(row.total_cost_window for row in account.process_costs)
    modeled_currency = (
        account.process_costs[0].currency if account.process_costs else config.pricing.currency
    )
    if coverage is not None and coverage.net_cost and coverage.currency == modeled_currency:
        delta = coverage.net_cost - modeled
        if delta > max(10.0, coverage.net_cost * 0.20):
            out.append(
                _unattributed_cost(
                    account,
                    config,
                    scan_id,
                    coverage.net_cost,
                    modeled,
                    coverage.data_through,
                )
            )
    return out


def _unattributed_buckets(account: Account) -> list[str]:
    """Nomeia a cobrança que não é rateada a nenhum ativo coletado."""
    coverage = account.glue_cost_coverage
    if coverage is None:
        return []
    evidence = [
        f"{bucket}={value:.2f} USD"
        for bucket, value in sorted(coverage.buckets.items())
        if bucket in UNATTRIBUTED_GLUE_BUCKETS and value
    ]
    if coverage.unknown_usage_types:
        evidence.append("usage types não classificados: " + ", ".join(coverage.unknown_usage_types))
    return evidence


def _unattributed_cost(
    account: Account,
    config: Config,
    scan_id: str,
    billing_window: float,
    modeled_window: float,
    data_through: str,
) -> Opportunity:
    delta = max(0.0, billing_window - modeled_window)
    buckets = _unattributed_buckets(account)
    return build(
        Finding(
            asset_type="glue_service",
            asset_name="AWS Glue",
            rule_id="GLUE-UNATTRIBUTED-COST",
            rule_version="1.0.0",
            title="Cobrança Glue relevante ainda não atribuída aos processos",
            why=(
                f"Cost Explorer na janela={billing_window:.2f} e modelo por processo="
                f"{modeled_window:.2f}; diferença={delta:.2f} {config.pricing.currency}."
                + (
                    " Buckets sem rateio: " + "; ".join(buckets) + "."
                    if buckets
                    else " A cobrança por usage type ainda não foi coletada."
                )
            ),
            category="inventory_integrity",
        ),
        Recommendation(
            difficulty=2,
            action=("Detalhar Cost Explorer/CUR por usage type, operação e tag de alocação"),
            how_to_apply=(
                "Investigar somente por consultas read-only; ativação de tags ou CUR "
                "exige aprovação administrativa separada."
            ),
            how_to_validate=(
                "Reconciliar jobs, sessions, crawlers, DataBrew, Data Catalog, "
                "Data Quality e table optimizers até explicar a diferença."
            ),
            risks=["diferença pode conter atraso ou modalidades ainda não modeladas"],
            docs=[_DOC_COST_EXPLORER],
            blocked=True,
        ),
        Evidence(
            items=[
                f"Cost Explorer Glue na janela={billing_window:.2f}",
                f"modelo atribuído na janela={modeled_window:.2f}",
                f"data_through={data_through or 'não informada'}",
            ]
            + buckets,
            sources=["Cost Explorer GetCostAndUsage", "modelo de processos Julius"],
            observed_runs=1,
            coverage_days=min(account.lookback_days, 31),
            has_optional_metrics=True,
            owner_tag=None,
        ),
        Estimation(
            method="glue_unattributed_cost_v1",
            baseline_cost=round(delta, 2),
            projected_cost=round(delta, 2),
            estimated_saving=0.0,
            assumptions=[
                "diferença não é economia até identificar os tipos de uso",
                "Cost Explorer e modelo usam a mesma moeda",
            ],
            pricing_region=config.pricing.region,
            estimation_version=config.pricing.version,
        ),
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _timeout(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.timeout_guardrail_saving(job, config)
    exec_min = (job.p95_execution_sec or job.avg_execution_sec) / 60
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-TIMEOUT-EXCESSIVE",
            rule_version="1.0.0",
            title="Timeout muito acima da duração média",
            why=f"Timeout de {job.timeout_min} min para um job que roda ~{exec_min:.0f} min — se travar, cobra DPU-hora até o timeout.",
        ),
        Recommendation(
            difficulty=1,
            action="Alinhar o timeout à duração real (com folga)",
            how_to_apply=f"Definir Timeout ~{max(30, round(exec_min * 2))} min (2× a duração média).",
            how_to_validate="Confirmar que execuções normais não são cortadas e que travadas param cedo.",
            risks=["timeout curto demais pode cortar picos legítimos"],
            docs=[_DOC_MONITORING],
            risk=0.7,
        ),
        Evidence(
            items=[f"timeout={job.timeout_min} min", f"duração média {exec_min:.0f} min"],
            sources=["Glue GetJob", "GetJobRuns"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=True,
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _worker_type(
    account: Account,
    job: GlueJob,
    config: Config,
    scan_id: str,
    capacity_evidence: bool,
) -> Opportunity:
    est, new_type = glue_est.worker_type_downgrade_saving(job, config)
    if not capacity_evidence:
        _zero_unproven(est)
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-WORKER-TYPE-OVERSIZED",
            rule_version="1.0.0",
            title=f"Worker type {job.worker_type} maior que a necessidade",
            why=f"CPU média {(job.avg_cpu_load or 0):.0%} com {job.worker_type} — um type menor ({new_type}) bastaria.",
        ),
        Recommendation(
            difficulty=2,
            action=f"Reduzir o worker type de {job.worker_type} para {new_type}",
            how_to_apply=f"Alterar WorkerType para {new_type} e testar 1 execução (mantendo o nº de workers).",
            how_to_validate="Comparar DPU-h, duração e memória por execução após a mudança.",
            risks=["type menor tem menos memória/vCPU por worker"],
            docs=[_DOC_WORKERS],
            blocked=not capacity_evidence,
        ),
        Evidence(
            items=[
                f"worker_type={job.worker_type}",
                f"utilização média {_utilization(job):.0%}",
                (
                    f"memória máx. {job.max_memory_used_pct:.0%}; disco máx. {job.max_disk_used_pct:.0%}"
                    if job.max_memory_used_pct is not None and job.max_disk_used_pct is not None
                    else "memória/disco não coletados"
                ),
                "spill coletado" if job.has_spill_evidence else "spill não coletado",
            ],
            sources=["Glue GetJob", "CloudWatch"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=capacity_evidence,
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _failing(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.failure_waste_saving(job, config)
    failed = round(job.runs_per_month * job.failure_rate)
    failed_dpu_monthly = job.total_failed_dpu_hours_window * job.monthly_factor
    categories = ", ".join(
        f"{name}={count}" for name, count in sorted(job.failure_categories.items())
    )
    sources = ["Glue GetJobRuns"]
    if job.failed_cost_window is not None:
        sources.append("Cost Explorer")
    evidence_items = [
        f"taxa de falha {job.failure_rate:.0%} em {job.observed_runs} execuções observadas",
        f"~{failed} execuções falhas/mês cobrando DPU-hora",
    ]
    if job.total_failed_dpu_hours_window > 0:
        evidence_items.extend(
            [
                f"{failed_dpu_monthly:.2f} DPU-h/mês atribuídas às falhas",
                f"{job.failed_retry_runs_in_window or 0} retries com falha na janela",
                f"causas sanitizadas na janela: {categories or 'não classificadas'}",
            ]
        )
    evidence_items.append(f"max_retries={job.max_retries}")
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-FAILING-JOB",
            rule_version="1.0.0",
            title="Job falha com frequência e cobra DPU-hora à toa",
            why=(
                f"{job.failure_rate:.0%} das execuções falham (~{failed}/mês); o Glue cobra o compute "
                "até a falha, e cada retry cobra de novo."
            ),
        ),
        Recommendation(
            difficulty=3,
            action="Corrigir a causa das falhas/retries",
            how_to_apply=(
                "Investigar os logs/Spark UI das execuções que falham, corrigir a causa (dados/OOM/"
                "permissão) e revisar MaxRetries."
            ),
            how_to_validate="Comparar taxa de falha e DPU-h desperdiçadas por mês após a correção.",
            risks=["a causa pode estar em dados/dependência externa"],
            docs=[_DOC_MONITORING],
            risk=0.8,
        ),
        Evidence(
            items=evidence_items,
            sources=sources,
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=job.observed_runs >= config.thresholds.min_runs,
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _version(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.version_upgrade_saving(job, config)
    exec_min = job.avg_execution_sec / 60
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-VERSION-OLD",
            rule_version="1.0.0",
            title=(
                f"Glue {job.glue_version} desatualizado (avaliar {config.preferred_glue_version})"
            ),
            why=f"Job em Glue {job.glue_version} fatura em blocos de 10 min; execuções de ~{exec_min:.0f} min pagam por 10.",
        ),
        Recommendation(
            difficulty=2,
            action=(
                "Avaliar migração para a versão preferencial configurada "
                f"({config.preferred_glue_version})"
            ),
            how_to_apply="Validar compatibilidade de runtime/bibliotecas e rodar a suíte de regressão antes da migração.",
            how_to_validate="Comparar custo/execução e duração; validar saídas com testes.",
            risks=["mudança de runtime pode exigir ajustes no script"],
            docs=[_DOC_VERSION],
        ),
        Evidence(
            items=[
                f"GlueVersion={job.glue_version}",
                f"duração média {exec_min:.1f} min",
                "billing mínimo 10 min",
                "sem AQE",
            ],
            sources=["Glue GetJob", "GetJobRuns"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=True,
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def signals(account: Account, config: Config) -> list[Signal]:
    """O que a config de um job levanta mas não conclui.

    Nos dois casos o gatilho é fato — a versão está declarada, a frequência é
    contada — e a conclusão não é. Migrar de runtime depende de bibliotecas e
    formatos que só o script revela; rodar 720 vezes por mês pode ser exatamente
    o certo para a fonte que o job lê.
    """
    out: list[Signal] = []
    target = config.preferred_glue_version
    th = config.thresholds
    for job in account.glue_jobs:
        if (
            job.command_type == "glueetl"
            and 2.0 <= job.glue_version_num < _version_number(target)
        ):
            out.append(
                Signal(
                    kind="config",
                    rule_id="GLUE-VERSION-REVIEW",
                    asset_type="glue_job",
                    asset_name=job.name,
                    observation=(
                        f"Job em Glue {job.glue_version}, abaixo da versão "
                        f"preferencial {target}."
                    ),
                    question=(
                        "O script usa bibliotecas, formatos ou APIs que impedem a "
                        f"migração para Glue {target}? Se não impedem, o que a migração "
                        "muda em duração e DPU-h neste job?"
                    ),
                    missing_evidence=[
                        "compatibilidade de bibliotecas e formatos usados pelo script",
                        "benchmark de duração e DPU-h na versão alvo",
                    ],
                    doc_links=[_DOC_VERSION],
                )
            )
        if _flex_candidate(job) and job.time_sensitive is None:
            out.append(
                Signal(
                    kind="config",
                    rule_id="GLUE-FLEX-TOLERANCE",
                    asset_type="glue_job",
                    asset_name=job.name,
                    observation=(
                        f"'{job.name}' é batch Spark em STANDARD com "
                        f"{job.runs_per_month:.0f} execuções/mês; FLEX custa menos "
                        "por DPU-hora."
                    ),
                    question=(
                        "O SLA deste job tolera início adiado e capacidade não "
                        "garantida? Quem espera a saída, e até quando?"
                    ),
                    missing_evidence=[
                        "prazo de entrega acordado com quem consome a saída",
                        "dependência a jusante que trava esperando este job",
                    ],
                    doc_links=[_DOC_FLEX],
                )
            )
        if (
            job.runs_per_month >= th.high_job_frequency_monthly
            and not job.incremental_source_evidence
        ):
            out.append(
                Signal(
                    kind="config",
                    rule_id="GLUE-FREQUENCY-REVIEW",
                    asset_type="glue_job",
                    asset_name=job.name,
                    observation=(
                        f"Job roda {job.runs_per_month:.1f}×/mês sem evidência "
                        "coletada de volume incremental."
                    ),
                    question=(
                        "A frequência é compatível com a natureza da fonte e com o "
                        "consumo a jusante, ou há execuções que não encontram dado novo?"
                    ),
                    missing_evidence=[
                        "volume de entrada por execução",
                        "alteração de saída entre execuções consecutivas",
                    ],
                    doc_links=[_DOC_MONITORING],
                )
            )
    return out


def _version_number(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 99.0


def _flex(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.flex_saving(job, config)
    discount = 1 - config.pricing.glue_flex_dpu_hour / max(config.pricing.glue_dpu_hour, 0.000001)
    tolerates = job.time_sensitive is False
    opportunity = build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-FLEX-CANDIDATE",
            rule_version="1.0.0",
            title="Job batch elegível a ExecutionClass FLEX",
            why=(
                "Job Spark batch ainda usa STANDARD; a tarifa FLEX versionada é "
                f"{discount:.0%} menor, se o SLA tolerar início adiado."
            ),
        ),
        Recommendation(
            difficulty=1,
            action="Mudar ExecutionClass para FLEX",
            how_to_apply="Definir --execution-class FLEX no job; validar que o SLA tolera variação de início.",
            how_to_validate="Comparar custo por execução e tempo de conclusão por 2 semanas.",
            risks=["tempo de início variável", "capacidade não garantida"],
            docs=[_DOC_FLEX],
            # A troca não pode ser feita antes de alguém afirmar que o SLA
            # aguenta esperar; a afirmação explícita no dataset conta.
            blocked=not tolerates,
        ),
        Evidence(
            items=[
                "execution_class=STANDARD",
                (
                    "tolerância a início adiado declarada"
                    if tolerates
                    else "tolerância a início adiado não confirmada"
                ),
                f"{job.runs_per_month} execuções/mês",
            ],
            sources=["Glue GetJob"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=tolerates,
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )
    if not tolerates:
        opportunity.missing_evidence = [
            "confirmação de que o SLA tolera início adiado e capacidade não garantida",
        ]
    return opportunity


def _bookmark(
    account: Account,
    job: GlueJob,
    config: Config,
    scan_id: str,
    incremental_evidence: bool,
) -> Opportunity:
    est = glue_est.bookmark_saving(job, config)
    if not incremental_evidence:
        _zero_unproven(est, "incrementalidade da fonte não comprovada")
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-BOOKMARK-OFF",
            rule_version="1.0.0",
            title="Job recorrente sem job bookmarks (reprocessa dados)",
            why=f"Job roda {job.runs_per_month}×/mês sem bookmarks — reprocessa dados já processados.",
        ),
        Recommendation(
            difficulty=3,
            action="Habilitar job bookmarks (processamento incremental)",
            how_to_apply="Ativar --job-bookmark-option job-bookmark-enable e ajustar o script para incremental; testar reprocessamento.",
            how_to_validate="Comparar bytes lidos e DPU-h por execução após ativar bookmarks.",
            risks=["exige ajuste no script", "reprocessamento inicial"],
            docs=[_DOC_BOOKMARK],
            blocked=not incremental_evidence,
        ),
        Evidence(
            items=[
                "--job-bookmark-option desligado",
                f"{job.runs_per_month} execuções/mês",
                "fonte incremental comprovada"
                if incremental_evidence
                else "incrementalidade da fonte não comprovada",
            ],
            sources=["Glue GetJob", "GetJobRuns"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=(
                job.observed_runs >= config.thresholds.min_runs and incremental_evidence
            ),
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _autoscaling(
    account: Account,
    job: GlueJob,
    config: Config,
    scan_id: str,
    capacity_evidence: bool,
) -> Opportunity:
    est = glue_est.autoscaling_saving(job, config)
    if not capacity_evidence:
        _zero_unproven(est)
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-AUTOSCALING",
            rule_version="1.0.0",
            title="Job com baixa utilização e capacidade fixa (sem Auto Scaling)",
            why=(
                f"Utilização média {_utilization(job):.0%} com {job.number_of_workers} workers fixos "
                f"({job.worker_type}) e sem --enable-auto-scaling."
            ),
        ),
        Recommendation(
            difficulty=1,
            action="Habilitar Auto Scaling e revisar o teto de workers",
            how_to_apply=(
                "No job: ativar --enable-auto-scaling e definir max workers ~"
                f"{max(2, (job.number_of_workers or 0) // 2)}; testar 1 execução controlada."
            ),
            how_to_validate="Comparar DPU-h e duração média por execução após a mudança.",
            risks=["reprocessamento", "picos de partição em estágio único"],
            docs=[_DOC_AUTOSCALING],
            blocked=not capacity_evidence,
        ),
        Evidence(
            items=[
                f"utilização média {_utilization(job):.0%} em {job.observed_runs} execuções",
                f"workers={job.number_of_workers} fixos ({job.worker_type})",
                "sem --enable-auto-scaling",
                f"~{job.window_dpu_hours:.0f} DPU-h/mês (GetJobRuns)",
            ],
            sources=["Glue GetJobRuns", "CloudWatch"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=capacity_evidence,
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _overprovisioned(
    account: Account,
    job: GlueJob,
    config: Config,
    scan_id: str,
    capacity_evidence: bool,
) -> Opportunity:
    est = glue_est.worker_reduction_saving(job, config)
    if not capacity_evidence:
        _zero_unproven(est)
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id="GLUE-OVERPROVISIONED",
            rule_version="1.0.0",
            title="Capacidade superdimensionada",
            why=(
                f"Utilização média {_utilization(job):.0%} com {job.number_of_workers} workers; "
                "a redução exige memória, disco e spill observados."
            ),
        ),
        Recommendation(
            difficulty=2,
            action="Reduzir o número de workers após teste controlado",
            how_to_apply="Reduzir number_of_workers com teste controlado em 3 execuções.",
            how_to_validate="Comparar duração e DPU-h por execução em 3 execuções pós-mudança.",
            risks=["gargalo de I/O", "skew de partição"],
            docs=[_DOC_WORKERS],
            blocked=not capacity_evidence,
        ),
        Evidence(
            items=[
                f"utilização média {_utilization(job):.0%}",
                f"workers={job.number_of_workers} ({job.worker_type})",
                (
                    f"spill observado={job.shuffle_spill_bytes or 0:.0f} bytes"
                    if job.has_spill_evidence
                    else "spill não coletado"
                ),
            ],
            sources=["Glue GetJobRuns", "CloudWatch"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=capacity_evidence,
            owner_tag=job.owner_tag,
        ),
        est,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )


def _utilization(job: GlueJob) -> float:
    value = (
        job.avg_worker_utilization if job.avg_worker_utilization is not None else job.avg_cpu_load
    )
    return float(value or 0.0)


def _capacity_evidence(job: GlueJob, config: Config) -> bool:
    th = config.thresholds
    return (
        job.max_memory_used_pct is not None
        and job.max_memory_used_pct < th.worker_memory_pressure_high
        and job.max_disk_used_pct is not None
        and job.max_disk_used_pct < th.worker_disk_pressure_high
        and job.has_spill_evidence
        and job.spark_event_log_evidence_complete
        and (job.shuffle_spill_bytes or 0) <= 0
    )


def _zero_unproven(
    estimation,
    reason: str = "evidência de memória, disco e spill incompleta",
) -> None:
    estimation.estimated_saving = 0.0
    estimation.projected_cost = estimation.baseline_cost
    estimation.assumptions.append(f"economia não quantificada: {reason}")


def _investigation(
    account: Account,
    job: GlueJob,
    config: Config,
    scan_id: str,
    rule_id: str,
    finding: str,
    action: str,
    evidence: list[str],
    *,
    baseline_cost: float = 0.0,
    doc_link: str = _DOC_WORKERS,
    category: str = "cost_optimization",
) -> Opportunity:
    estimation = Estimation(
        method=rule_id.lower().replace("-", "_") + "_v1",
        baseline_cost=round(baseline_cost, 2),
        projected_cost=round(baseline_cost, 2),
        estimated_saving=0.0,
        assumptions=["economia não quantificada sem teste controlado"],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
    )
    return build(
        Finding(
            asset_type="glue_job",
            asset_name=job.name,
            rule_id=rule_id,
            rule_version="1.0.0",
            title=finding,
            why=finding,
            category=category,
        ),
        Recommendation(
            difficulty=3,
            action=action,
            how_to_apply="Analisar as execuções referenciadas e testar uma mudança isolada.",
            how_to_validate="Comparar duração p95, DPU-h e saída antes/depois.",
            risks=["não alterar capacidade antes de confirmar a causa"],
            docs=[doc_link],
            blocked=True,
        ),
        Evidence(
            items=evidence,
            sources=["CloudWatch Glue Observability", "Glue GetJobRuns"],
            observed_runs=job.observed_runs,
            coverage_days=job.coverage_days,
            has_optional_metrics=True,
            owner_tag=job.owner_tag,
        ),
        estimation,
        RuleContext(
            account=account.account_id,
            config=config,
            scan_id=scan_id,
        ),
    )
