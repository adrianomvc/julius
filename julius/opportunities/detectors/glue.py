"""Detectores de Glue Jobs: (1) sem Auto Scaling, (2) workers superdimensionados."""

from __future__ import annotations

from julius.config import UNATTRIBUTED_GLUE_BUCKETS, Config
from julius.estimation import glue as glue_est
from julius.inventory.model import Account, GlueJob
from julius.opportunities.base import Estimation, Opportunity
from julius.opportunities.detectors._build import build

_DOC_AUTOSCALING = "https://docs.aws.amazon.com/glue/latest/dg/auto-scaling.html"
_DOC_WORKERS = "https://docs.aws.amazon.com/glue/latest/dg/monitor-spark-ui.html"
_DOC_VERSION = "https://docs.aws.amazon.com/glue/latest/dg/release-notes.html"
_DOC_FLEX = "https://docs.aws.amazon.com/glue/latest/dg/aws-glue-api-jobs-runs.html"
_DOC_BOOKMARK = "https://docs.aws.amazon.com/glue/latest/dg/monitor-continuations.html"
_DOC_MONITORING = "https://docs.aws.amazon.com/glue/latest/dg/monitor-glue.html"
_DOC_COST_EXPLORER = (
    "https://docs.aws.amazon.com/aws-cost-management/latest/"
    "APIReference/API_GetCostAndUsage.html"
)

_AUTOSCALE_TYPES = {
    "G.1X", "G.2X", "G.4X", "G.8X", "G.12X", "G.16X",
    "R.1X", "R.2X", "R.4X", "R.8X",
}
_DOWNGRADE_TYPES = {
    "G.4X", "G.8X", "G.12X", "G.16X", "R.2X", "R.4X", "R.8X"
}


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
        elif (
            low_cpu
            and (job.number_of_workers or 0) > config.thresholds.session_min_dpu
        ):
            out.append(_overprovisioned(account, job, config, scan_id, capacity_evidence))

        # Versão antiga: fatura em blocos de 10 min.
        if job.command_type == "glueetl":
            if job.glue_version_num < 2.0:
                out.append(_version(account, job, config, scan_id))
            elif job.glue_version_num < _version_number(
                config.preferred_glue_version
            ):
                out.append(_version_review(account, job, config, scan_id))

        # FLEX: job em batch, não sensível a tempo, ainda em STANDARD.
        if (
            job.execution_class == "STANDARD"
            and job.time_sensitive is False
            and job.runs_per_month > 0
            and job.glue_version_num >= 3.0
            and job.command_type == "glueetl"
        ):
            out.append(_flex(account, job, config, scan_id))

        # Bookmarks desligados num job recorrente: reprocessa dados antigos.
        if (
            not job.job_bookmark
            and job.runs_per_month >= 8
            and job.command_type == "glueetl"
        ):
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
        if exec_min > 0 and job.timeout_min >= th.timeout_excess_min_minutes and job.timeout_min > th.timeout_excess_ratio * exec_min:
            out.append(_timeout(account, job, config, scan_id))

        # Worker type grande (G.4X/G.8X) com CPU baixa → type menor bastaria.
        if job.worker_type in _DOWNGRADE_TYPES and job.avg_cpu_load is not None and job.avg_cpu_load < th.worker_type_low_cpu:
            out.append(
                _worker_type(
                    account, job, config, scan_id, capacity_evidence
                )
            )

        duration_cv = (
            job.execution_stddev_sec / job.avg_execution_sec
            if job.avg_execution_sec > 0
            else 0.0
        )
        if (
            job.max_task_skew is not None
            and job.max_task_skew >= th.task_skew_high
        ) or duration_cv >= th.task_duration_cv_high:
            variance_evidence = []
            if job.max_task_skew is not None:
                variance_evidence.append(
                    f"skewness.job máximo={job.max_task_skew:.2f}"
                )
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
                (job.avg_all_executors - job.avg_max_needed_executors)
                / job.avg_all_executors,
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

        if (
            job.runs_per_month >= th.high_job_frequency_monthly
            and not job.incremental_source_evidence
        ):
            out.append(
                _investigation(
                    account,
                    job,
                    config,
                    scan_id,
                    "GLUE-FREQUENCY-REVIEW",
                    "Frequência alta sem evidência de volume incremental",
                    "Relacionar cron, volume de entrada e alterações de saída",
                    [f"{job.runs_per_month:.1f} execuções/mês"],
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
    if (
        coverage is not None
        and coverage.net_cost
        and coverage.currency == modeled_currency
    ):
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
        evidence.append(
            "usage types não classificados: "
            + ", ".join(coverage.unknown_usage_types)
        )
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
        account=account.account_id,
        asset_type="glue_service",
        asset_name="AWS Glue",
        rule_id="GLUE-UNATTRIBUTED-COST",
        rule_version="1.0.0",
        difficulty=2,
        estimation=Estimation(
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
        finding="Cobrança Glue relevante ainda não atribuída aos processos",
        why=(
            f"Cost Explorer na janela={billing_window:.2f} e modelo por processo="
            f"{modeled_window:.2f}; diferença={delta:.2f} {config.pricing.currency}."
            + (
                " Buckets sem rateio: " + "; ".join(buckets) + "."
                if buckets
                else " A cobrança por usage type ainda não foi coletada."
            )
        ),
        recommended_action=(
            "Detalhar Cost Explorer/CUR por usage type, operação e tag de alocação"
        ),
        how_to_apply=(
            "Investigar somente por consultas read-only; ativação de tags ou CUR "
            "exige aprovação administrativa separada."
        ),
        how_to_validate=(
            "Reconciliar jobs, sessions, crawlers, DataBrew, Data Catalog, "
            "Data Quality e table optimizers até explicar a diferença."
        ),
        evidence=[
            f"Cost Explorer Glue na janela={billing_window:.2f}",
            f"modelo atribuído na janela={modeled_window:.2f}",
            f"data_through={data_through or 'não informada'}",
        ] + buckets,
        risks=["diferença pode conter atraso ou modalidades ainda não modeladas"],
        doc_links=[_DOC_COST_EXPLORER],
        data_sources=["Cost Explorer GetCostAndUsage", "modelo de processos Julius"],
        observed_runs=1,
        coverage_days=min(account.lookback_days, 31),
        has_optional_metrics=True,
        owner_tag=None,
        config=config,
        scan_id=scan_id,
        blocked=True,
    )


def _timeout(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.timeout_guardrail_saving(job, config)
    exec_min = (job.p95_execution_sec or job.avg_execution_sec) / 60
    return build(
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-TIMEOUT-EXCESSIVE", rule_version="1.0.0", difficulty=1, estimation=est,
        finding="Timeout muito acima da duração média",
        why=f"Timeout de {job.timeout_min} min para um job que roda ~{exec_min:.0f} min — se travar, cobra DPU-hora até o timeout.",
        recommended_action="Alinhar o timeout à duração real (com folga)",
        how_to_apply=f"Definir Timeout ~{max(30, round(exec_min * 2))} min (2× a duração média).",
        how_to_validate="Confirmar que execuções normais não são cortadas e que travadas param cedo.",
        evidence=[f"timeout={job.timeout_min} min", f"duração média {exec_min:.0f} min"],
        risks=["timeout curto demais pode cortar picos legítimos"],
        doc_links=[_DOC_MONITORING], data_sources=["Glue GetJob", "GetJobRuns"],
        observed_runs=job.observed_runs, coverage_days=job.coverage_days,
        has_optional_metrics=True, owner_tag=job.owner_tag, config=config, scan_id=scan_id, risk=0.7,
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
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-WORKER-TYPE-OVERSIZED", rule_version="1.0.0", difficulty=2, estimation=est,
        finding=f"Worker type {job.worker_type} maior que a necessidade",
        why=f"CPU média {(job.avg_cpu_load or 0):.0%} com {job.worker_type} — um type menor ({new_type}) bastaria.",
        recommended_action=f"Reduzir o worker type de {job.worker_type} para {new_type}",
        how_to_apply=f"Alterar WorkerType para {new_type} e testar 1 execução (mantendo o nº de workers).",
        how_to_validate="Comparar DPU-h, duração e memória por execução após a mudança.",
        evidence=[
            f"worker_type={job.worker_type}",
            f"utilização média {_utilization(job):.0%}",
            (
                f"memória máx. {job.max_memory_used_pct:.0%}; disco máx. {job.max_disk_used_pct:.0%}"
                if job.max_memory_used_pct is not None and job.max_disk_used_pct is not None
                else "memória/disco não coletados"
            ),
            "spill coletado" if job.has_spill_evidence else "spill não coletado",
        ],
        risks=["type menor tem menos memória/vCPU por worker"],
        doc_links=[_DOC_WORKERS], data_sources=["Glue GetJob", "CloudWatch"],
        observed_runs=job.observed_runs, coverage_days=job.coverage_days,
        has_optional_metrics=capacity_evidence, owner_tag=job.owner_tag,
        config=config, scan_id=scan_id, blocked=not capacity_evidence,
    )


def _failing(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.failure_waste_saving(job, config)
    failed = round(job.runs_per_month * job.failure_rate)
    return build(
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-FAILING-JOB", rule_version="1.0.0", difficulty=3, estimation=est,
        finding="Job falha com frequência e cobra DPU-hora à toa",
        why=(
            f"{job.failure_rate:.0%} das execuções falham (~{failed}/mês); o Glue cobra o compute "
            "até a falha, e cada retry cobra de novo."
        ),
        recommended_action="Corrigir a causa das falhas/retries",
        how_to_apply=(
            "Investigar os logs/Spark UI das execuções que falham, corrigir a causa (dados/OOM/"
            "permissão) e revisar MaxRetries."
        ),
        how_to_validate="Comparar taxa de falha e DPU-h desperdiçadas por mês após a correção.",
        evidence=[
            f"taxa de falha {job.failure_rate:.0%} em {job.observed_runs} execuções observadas",
            f"~{failed} execuções falhas/mês cobrando DPU-hora",
            f"max_retries={job.max_retries}",
        ],
        risks=["a causa pode estar em dados/dependência externa"],
        doc_links=[_DOC_MONITORING], data_sources=["Glue GetJobRuns", "CloudWatch"],
        observed_runs=job.observed_runs, coverage_days=job.coverage_days,
        has_optional_metrics=job.observed_runs >= config.thresholds.min_runs,
        owner_tag=job.owner_tag, config=config, scan_id=scan_id, risk=0.8,
    )


def _version(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.version_upgrade_saving(job, config)
    exec_min = job.avg_execution_sec / 60
    return build(
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-VERSION-OLD", rule_version="1.0.0", difficulty=2, estimation=est,
        finding=(
            f"Glue {job.glue_version} desatualizado "
            f"(avaliar {config.preferred_glue_version})"
        ),
        why=f"Job em Glue {job.glue_version} fatura em blocos de 10 min; execuções de ~{exec_min:.0f} min pagam por 10.",
        recommended_action=(
            "Avaliar migração para a versão preferencial configurada "
            f"({config.preferred_glue_version})"
        ),
        how_to_apply="Validar compatibilidade de runtime/bibliotecas e rodar a suíte de regressão antes da migração.",
        how_to_validate="Comparar custo/execução e duração; validar saídas com testes.",
        evidence=[f"GlueVersion={job.glue_version}", f"duração média {exec_min:.1f} min", "billing mínimo 10 min", "sem AQE"],
        risks=["mudança de runtime pode exigir ajustes no script"], doc_links=[_DOC_VERSION],
        data_sources=["Glue GetJob", "GetJobRuns"], observed_runs=job.observed_runs,
        coverage_days=job.coverage_days, has_optional_metrics=True, owner_tag=job.owner_tag,
        config=config, scan_id=scan_id,
    )


def _version_review(
    account: Account, job: GlueJob, config: Config, scan_id: str
) -> Opportunity:
    target = config.preferred_glue_version
    return build(
        account=account.account_id,
        asset_type="glue_job",
        asset_name=job.name,
        rule_id="GLUE-VERSION-REVIEW",
        rule_version="1.0.0",
        difficulty=3,
        estimation=Estimation(
            method="glue_version_review_v1",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=["migração não recebe economia sem benchmark"],
            pricing_region=config.pricing.region,
            estimation_version=config.pricing.version,
        ),
        finding=f"Glue {job.glue_version} abaixo da versão preferencial {target}",
        why="Runtime antigo requer avaliação de compatibilidade e suporte.",
        recommended_action=f"Avaliar migração controlada para Glue {target}",
        how_to_apply="Validar bibliotecas, formatos e testes de regressão em ambiente controlado.",
        how_to_validate="Comparar saída, duração p95, falhas e DPU-h.",
        evidence=[
            f"GlueVersion={job.glue_version}",
            f"versão preferencial configurada={target}",
        ],
        risks=["mudanças de Spark/Python/Java podem exigir ajustes"],
        doc_links=[_DOC_VERSION],
        data_sources=["Glue GetJob"],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=True,
        owner_tag=job.owner_tag,
        config=config,
        scan_id=scan_id,
        blocked=True,
    )


def _version_number(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 99.0


def _flex(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.flex_saving(job, config)
    discount = (
        1
        - config.pricing.glue_flex_dpu_hour
        / max(config.pricing.glue_dpu_hour, 0.000001)
    )
    return build(
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-FLEX-CANDIDATE", rule_version="1.0.0", difficulty=1, estimation=est,
        finding="Job batch elegível a ExecutionClass FLEX",
        why=(
            "Job Spark batch não sensível a tempo ainda usa STANDARD; "
            f"a tarifa FLEX versionada é {discount:.0%} menor."
        ),
        recommended_action="Mudar ExecutionClass para FLEX",
        how_to_apply="Definir --execution-class FLEX no job; validar que o SLA tolera variação de início.",
        how_to_validate="Comparar custo por execução e tempo de conclusão por 2 semanas.",
        evidence=["execution_class=STANDARD", "não sensível a tempo (batch)", f"{job.runs_per_month} execuções/mês"],
        risks=["tempo de início variável"], doc_links=[_DOC_FLEX],
        data_sources=["Glue GetJob"], observed_runs=job.observed_runs,
        coverage_days=job.coverage_days, has_optional_metrics=True, owner_tag=job.owner_tag,
        config=config, scan_id=scan_id,
    )


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
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-BOOKMARK-OFF", rule_version="1.0.0", difficulty=3, estimation=est,
        finding="Job recorrente sem job bookmarks (reprocessa dados)",
        why=f"Job roda {job.runs_per_month}×/mês sem bookmarks — reprocessa dados já processados.",
        recommended_action="Habilitar job bookmarks (processamento incremental)",
        how_to_apply="Ativar --job-bookmark-option job-bookmark-enable e ajustar o script para incremental; testar reprocessamento.",
        how_to_validate="Comparar bytes lidos e DPU-h por execução após ativar bookmarks.",
        evidence=[
            "--job-bookmark-option desligado",
            f"{job.runs_per_month} execuções/mês",
            "fonte incremental comprovada"
            if incremental_evidence
            else "incrementalidade da fonte não comprovada",
        ],
        risks=["exige ajuste no script", "reprocessamento inicial"], doc_links=[_DOC_BOOKMARK],
        data_sources=["Glue GetJob", "GetJobRuns"], observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=(
            job.observed_runs >= config.thresholds.min_runs
            and incremental_evidence
        ),
        owner_tag=job.owner_tag, config=config, scan_id=scan_id,
        blocked=not incremental_evidence,
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
        account=account.account_id,
        asset_type="glue_job",
        asset_name=job.name,
        rule_id="GLUE-AUTOSCALING",
        rule_version="1.0.0",
        difficulty=1,
        estimation=est,
        finding="Job com baixa utilização e capacidade fixa (sem Auto Scaling)",
        why=(
            f"Utilização média {_utilization(job):.0%} com {job.number_of_workers} workers fixos "
            f"({job.worker_type}) e sem --enable-auto-scaling."
        ),
        recommended_action="Habilitar Auto Scaling e revisar o teto de workers",
        how_to_apply=(
            "No job: ativar --enable-auto-scaling e definir max workers ~"
            f"{max(2, (job.number_of_workers or 0) // 2)}; testar 1 execução controlada."
        ),
        how_to_validate="Comparar DPU-h e duração média por execução após a mudança.",
        evidence=[
            f"utilização média {_utilization(job):.0%} em {job.observed_runs} execuções",
            f"workers={job.number_of_workers} fixos ({job.worker_type})",
            "sem --enable-auto-scaling",
            f"~{job.window_dpu_hours:.0f} DPU-h/mês (GetJobRuns)",
        ],
        risks=["reprocessamento", "picos de partição em estágio único"],
        doc_links=[_DOC_AUTOSCALING],
        data_sources=["Glue GetJobRuns", "CloudWatch"],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=capacity_evidence,
        owner_tag=job.owner_tag,
        config=config,
        scan_id=scan_id, blocked=not capacity_evidence,
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
        account=account.account_id,
        asset_type="glue_job",
        asset_name=job.name,
        rule_id="GLUE-OVERPROVISIONED",
        rule_version="1.0.0",
        difficulty=2,
        estimation=est,
        finding="Capacidade superdimensionada",
        why=(
            f"Utilização média {_utilization(job):.0%} com {job.number_of_workers} workers; "
            "a redução exige memória, disco e spill observados."
        ),
        recommended_action="Reduzir o número de workers após teste controlado",
        how_to_apply="Reduzir number_of_workers com teste controlado em 3 execuções.",
        how_to_validate="Comparar duração e DPU-h por execução em 3 execuções pós-mudança.",
        evidence=[
            f"utilização média {_utilization(job):.0%}",
            f"workers={job.number_of_workers} ({job.worker_type})",
            (
                f"spill observado={job.shuffle_spill_bytes or 0:.0f} bytes"
                if job.has_spill_evidence
                else "spill não coletado"
            ),
        ],
        risks=["gargalo de I/O", "skew de partição"],
        doc_links=[_DOC_WORKERS],
        data_sources=["Glue GetJobRuns", "CloudWatch"],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=capacity_evidence,
        owner_tag=job.owner_tag,
        config=config, scan_id=scan_id, blocked=not capacity_evidence,
    )


def _utilization(job: GlueJob) -> float:
    value = (
        job.avg_worker_utilization
        if job.avg_worker_utilization is not None
        else job.avg_cpu_load
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
    estimation.assumptions.append(
        f"economia não quantificada: {reason}"
    )


def _investigation(
    account: Account,
    job: GlueJob,
    config: Config,
    scan_id: str,
    rule_id: str,
    finding: str,
    action: str,
    evidence: list[str],
) -> Opportunity:
    estimation = Estimation(
        method=rule_id.lower().replace("-", "_") + "_v1",
        baseline_cost=0.0,
        projected_cost=0.0,
        estimated_saving=0.0,
        assumptions=["economia não quantificada sem teste controlado"],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
    )
    return build(
        account=account.account_id,
        asset_type="glue_job",
        asset_name=job.name,
        rule_id=rule_id,
        rule_version="1.0.0",
        difficulty=3,
        estimation=estimation,
        finding=finding,
        why=finding,
        recommended_action=action,
        how_to_apply="Analisar as execuções referenciadas e testar uma mudança isolada.",
        how_to_validate="Comparar duração p95, DPU-h e saída antes/depois.",
        evidence=evidence,
        risks=["não alterar capacidade antes de confirmar a causa"],
        doc_links=[_DOC_WORKERS],
        data_sources=["CloudWatch Glue Observability", "Glue GetJobRuns"],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=True,
        owner_tag=job.owner_tag,
        config=config,
        scan_id=scan_id,
        blocked=True,
    )
