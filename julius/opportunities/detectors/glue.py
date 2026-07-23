"""Detectores de Glue Jobs: (1) sem Auto Scaling, (2) workers superdimensionados."""

from __future__ import annotations

from julius.config import Config
from julius.estimation import glue as glue_est
from julius.inventory.model import Account, GlueJob
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors._build import build

_DOC_AUTOSCALING = "https://docs.aws.amazon.com/glue/latest/dg/auto-scaling.html"
_DOC_WORKERS = "https://docs.aws.amazon.com/glue/latest/dg/monitor-spark-ui.html"
_DOC_VERSION = "https://docs.aws.amazon.com/glue/latest/dg/release-notes.html"
_DOC_FLEX = "https://docs.aws.amazon.com/glue/latest/dg/reduced-start-times-spark-etl-jobs.html"
_DOC_BOOKMARK = "https://docs.aws.amazon.com/glue/latest/dg/monitor-continuations.html"
_DOC_MONITORING = "https://docs.aws.amazon.com/glue/latest/dg/monitor-glue.html"

_AUTOSCALE_TYPES = {"G.1X", "G.2X"}
_DOWNGRADE_TYPES = {"G.4X", "G.8X"}


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    for job in account.glue_jobs:
        low_cpu = job.avg_cpu_load is not None and job.avg_cpu_load < config.thresholds.low_cpu

        # Capacidade: Auto Scaling (sem autoscaling) OU redução de workers (demais casos).
        if not job.auto_scaling and job.worker_type in _AUTOSCALE_TYPES and low_cpu:
            out.append(_autoscaling(account, job, config, scan_id))
        elif low_cpu and job.number_of_workers > config.thresholds.session_min_dpu:
            out.append(_overprovisioned(account, job, config, scan_id))

        # Versão antiga: fatura em blocos de 10 min.
        if job.glue_version_num < 2.0:
            out.append(_version(account, job, config, scan_id))

        # FLEX: job em batch, não sensível a tempo, ainda em STANDARD.
        if job.execution_class == "STANDARD" and job.time_sensitive is False and job.runs_per_month > 0:
            out.append(_flex(account, job, config, scan_id))

        # Bookmarks desligados num job recorrente: reprocessa dados antigos.
        if not job.job_bookmark and job.runs_per_month >= 8:
            out.append(_bookmark(account, job, config, scan_id))

        # Execuções que falham (e retries) cobram DPU-hora sem entregar.
        if job.failure_rate >= config.thresholds.high_failure_rate and job.runs_per_month > 0:
            out.append(_failing(account, job, config, scan_id))

        # Timeout muito acima da duração média (risco de job pendurado cobrando).
        th = config.thresholds
        exec_min = job.avg_execution_sec / 60.0
        if exec_min > 0 and job.timeout_min >= th.timeout_excess_min_minutes and job.timeout_min > th.timeout_excess_ratio * exec_min:
            out.append(_timeout(account, job, config, scan_id))

        # Worker type grande (G.4X/G.8X) com CPU baixa → type menor bastaria.
        if job.worker_type in _DOWNGRADE_TYPES and job.avg_cpu_load is not None and job.avg_cpu_load < th.worker_type_low_cpu:
            out.append(_worker_type(account, job, config, scan_id))
    return out


def _timeout(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.timeout_guardrail_saving(job, config)
    exec_min = job.avg_execution_sec / 60
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


def _worker_type(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est, new_type = glue_est.worker_type_downgrade_saving(job, config)
    return build(
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-WORKER-TYPE-OVERSIZED", rule_version="1.0.0", difficulty=2, estimation=est,
        finding=f"Worker type {job.worker_type} maior que a necessidade",
        why=f"CPU média {(job.avg_cpu_load or 0):.0%} com {job.worker_type} — um type menor ({new_type}) bastaria.",
        recommended_action=f"Reduzir o worker type de {job.worker_type} para {new_type}",
        how_to_apply=f"Alterar WorkerType para {new_type} e testar 1 execução (mantendo o nº de workers).",
        how_to_validate="Comparar DPU-h, duração e memória por execução após a mudança.",
        evidence=[f"worker_type={job.worker_type}", f"CPU média {(job.avg_cpu_load or 0):.0%}", "sem contenção de memória observada"],
        risks=["type menor tem menos memória/vCPU por worker"],
        doc_links=[_DOC_WORKERS], data_sources=["Glue GetJob", "CloudWatch"],
        observed_runs=job.observed_runs, coverage_days=job.coverage_days,
        has_optional_metrics=job.avg_cpu_load is not None, owner_tag=job.owner_tag,
        config=config, scan_id=scan_id,
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
        finding=f"Glue {job.glue_version} → 4.0 (faturamento de 10 min)",
        why=f"Job em Glue {job.glue_version} fatura em blocos de 10 min; execuções de ~{exec_min:.0f} min pagam por 10.",
        recommended_action="Atualizar GlueVersion para 4.0",
        how_to_apply="Migrar para Glue 4.0 (billing 1 min, AQE); rodar a suíte de regressão.",
        how_to_validate="Comparar custo/execução e duração; validar saídas com testes.",
        evidence=[f"GlueVersion={job.glue_version}", f"duração média {exec_min:.1f} min", "billing mínimo 10 min", "sem AQE"],
        risks=["mudança de runtime pode exigir ajustes no script"], doc_links=[_DOC_VERSION],
        data_sources=["Glue GetJob", "GetJobRuns"], observed_runs=job.observed_runs,
        coverage_days=job.coverage_days, has_optional_metrics=True, owner_tag=job.owner_tag,
        config=config, scan_id=scan_id,
    )


def _flex(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.flex_saving(job, config)
    return build(
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-FLEX-CANDIDATE", rule_version="1.0.0", difficulty=1, estimation=est,
        finding="Job batch elegível a ExecutionClass FLEX",
        why="Job não sensível a tempo (batch) ainda em STANDARD; FLEX usa capacidade ociosa por ~34% menos.",
        recommended_action="Mudar ExecutionClass para FLEX",
        how_to_apply="Definir --execution-class FLEX no job; validar que o SLA tolera variação de início.",
        how_to_validate="Comparar custo por execução e tempo de conclusão por 2 semanas.",
        evidence=["execution_class=STANDARD", "não sensível a tempo (batch)", f"{job.runs_per_month} execuções/mês"],
        risks=["tempo de início variável"], doc_links=[_DOC_FLEX],
        data_sources=["Glue GetJob"], observed_runs=job.observed_runs,
        coverage_days=job.coverage_days, has_optional_metrics=True, owner_tag=job.owner_tag,
        config=config, scan_id=scan_id,
    )


def _bookmark(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.bookmark_saving(job, config)
    return build(
        account=account.account_id, asset_type="glue_job", asset_name=job.name,
        rule_id="GLUE-BOOKMARK-OFF", rule_version="1.0.0", difficulty=3, estimation=est,
        finding="Job recorrente sem job bookmarks (reprocessa dados)",
        why=f"Job roda {job.runs_per_month}×/mês sem bookmarks — reprocessa dados já processados.",
        recommended_action="Habilitar job bookmarks (processamento incremental)",
        how_to_apply="Ativar --job-bookmark-option job-bookmark-enable e ajustar o script para incremental; testar reprocessamento.",
        how_to_validate="Comparar bytes lidos e DPU-h por execução após ativar bookmarks.",
        evidence=["--job-bookmark-option desligado", f"{job.runs_per_month} execuções/mês", "fonte incremental"],
        risks=["exige ajuste no script", "reprocessamento inicial"], doc_links=[_DOC_BOOKMARK],
        data_sources=["Glue GetJob", "GetJobRuns"], observed_runs=job.observed_runs,
        coverage_days=job.coverage_days, has_optional_metrics=job.observed_runs >= config.thresholds.min_runs,
        owner_tag=job.owner_tag, config=config, scan_id=scan_id,
    )


def _autoscaling(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.autoscaling_saving(job, config)
    has_cpu = job.avg_cpu_load is not None
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
            f"CPU média {job.avg_cpu_load:.0%} com {job.number_of_workers} workers fixos "
            f"({job.worker_type}) e sem --enable-auto-scaling."
        ),
        recommended_action="Habilitar Auto Scaling e revisar o teto de workers",
        how_to_apply=(
            "No job: ativar --enable-auto-scaling e definir max workers ~"
            f"{max(2, job.number_of_workers // 2)}; testar 1 execução controlada."
        ),
        how_to_validate="Comparar DPU-h e duração média por execução após a mudança.",
        evidence=[
            f"CPU load média {job.avg_cpu_load:.0%} em {job.observed_runs} execuções",
            f"workers={job.number_of_workers} fixos ({job.worker_type})",
            "sem --enable-auto-scaling",
            f"~{job.monthly_dpu_hours:.0f} DPU-h/mês (GetJobRuns)",
        ],
        risks=["reprocessamento", "picos de partição em estágio único"],
        doc_links=[_DOC_AUTOSCALING],
        data_sources=["Glue GetJobRuns", "CloudWatch"],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=has_cpu,
        owner_tag=job.owner_tag,
        config=config,
        scan_id=scan_id,
    )


def _overprovisioned(account: Account, job: GlueJob, config: Config, scan_id: str) -> Opportunity:
    est = glue_est.worker_reduction_saving(job, config)
    has_cpu = job.avg_cpu_load is not None
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
            f"Utilização média {job.avg_cpu_load:.0%} com {job.number_of_workers} workers; "
            "sem sinal de contenção de memória."
        ),
        recommended_action="Reduzir o número de workers após teste controlado",
        how_to_apply="Reduzir number_of_workers com teste controlado em 3 execuções.",
        how_to_validate="Comparar duração e DPU-h por execução em 3 execuções pós-mudança.",
        evidence=[
            f"utilização média {job.avg_cpu_load:.0%}",
            f"workers={job.number_of_workers} ({job.worker_type})",
            "sem spill de memória nos logs",
        ],
        risks=["gargalo de I/O", "skew de partição"],
        doc_links=[_DOC_WORKERS],
        data_sources=["Glue GetJobRuns", "CloudWatch"],
        observed_runs=job.observed_runs,
        coverage_days=job.coverage_days,
        has_optional_metrics=has_cpu,
        owner_tag=job.owner_tag,
        config=config,
        scan_id=scan_id,
    )
