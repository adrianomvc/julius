"""Modelos financeiros para Glue Jobs (Auto Scaling e redução de workers)."""

from __future__ import annotations

import math

from julius.config import Config
from julius.inventory.model import GlueJob
from julius.opportunities.base import Estimation


def _monthly_cost(job: GlueJob, pricing) -> float:
    return job.monthly_dpu_hours * pricing.glue_dpu_hour


def autoscaling_saving(job: GlueJob, config: Config) -> Estimation:
    """Auto Scaling remove workers ociosos: saving ∝ (1 − util/alvo)."""
    pricing = config.pricing
    util = job.avg_cpu_load if job.avg_cpu_load is not None else config.thresholds.low_cpu
    ratio = max(0.0, min(0.6, 1 - util / config.thresholds.utilization_target))
    baseline = _monthly_cost(job, pricing)
    saving = baseline * ratio
    return Estimation(
        method="glue_autoscaling_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            "mesmo volume e frequência de execução",
            f"utilização observada {util:.0%} vs. alvo {config.thresholds.utilization_target:.0%}",
            "Auto Scaling reduz workers ociosos (redução limitada a 60%)",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def version_upgrade_saving(job: GlueJob, config: Config) -> Estimation:
    """Glue <2.0 fatura em blocos de 10 min; 2.0+ em 1 min. Ganho no arredondamento."""
    pricing = config.pricing
    exec_min = job.avg_execution_sec / 60.0
    billed_old = max(10.0, exec_min)
    billed_new = max(1.0, exec_min)
    total_dpu = job.number_of_workers * job.dpu_per_worker
    per_run = (billed_old - billed_new) / 60.0 * total_dpu * pricing.glue_dpu_hour
    saving = max(0.0, per_run * job.runs_per_month)
    baseline = billed_old / 60.0 * total_dpu * pricing.glue_dpu_hour * job.runs_per_month
    return Estimation(
        method="glue_version_upgrade_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"GlueVersion {job.glue_version} fatura mín. 10 min; 2.0+ fatura mín. 1 min",
            f"duração média {exec_min:.1f} min · {job.runs_per_month} execuções/mês",
            "mesma configuração de workers",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def flex_saving(job: GlueJob, config: Config, discount: float = 0.34) -> Estimation:
    """ExecutionClass FLEX ~34% mais barato para jobs não sensíveis a tempo."""
    pricing = config.pricing
    baseline = _monthly_cost(job, pricing)
    saving = baseline * discount
    return Estimation(
        method="glue_flex_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            "job em batch, não sensível a tempo (sem SLA rígido)",
            f"FLEX ~{int(discount * 100)}% mais barato que STANDARD",
            "tolera variação no tempo de início",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def bookmark_saving(job: GlueJob, config: Config, reprocess: float = 0.25) -> Estimation:
    """Job bookmarks evitam reprocessar dados já processados (processamento incremental)."""
    pricing = config.pricing
    baseline = _monthly_cost(job, pricing)
    saving = baseline * reprocess
    return Estimation(
        method="glue_bookmark_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            "job recorrente sem job bookmarks (reprocessa dados antigos)",
            f"processamento incremental evita ~{int(reprocess * 100)}% do trabalho",
            "fonte cresce de forma incremental",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


# Ordem de downgrade de worker type (DPU por worker).
_TYPE_DPU = {"G.1X": 1, "G.2X": 2, "G.4X": 4, "G.8X": 8}
_DOWNGRADE = {"G.8X": "G.4X", "G.4X": "G.2X", "G.2X": "G.1X"}


def timeout_guardrail_saving(
    job: GlueJob, config: Config, hangs_per_month: float = 0.3, detection_horizon_min: float = 240.0
) -> Estimation:
    """Timeout muito acima da duração média: um job travado cobra DPU-hora até o
    timeout. Guardrail conservador — limita a janela desperdiçada ao horizonte
    realista de detecção (um job pendurado é notado em ~4h, não nas 48h do timeout)."""
    pricing = config.pricing
    exec_min = job.avg_execution_sec / 60.0
    total_dpu = job.number_of_workers * job.dpu_per_worker
    wasted_min = min(max(0.0, job.timeout_min - exec_min), detection_horizon_min)
    wasted_per_hang = wasted_min / 60.0 * total_dpu * pricing.glue_dpu_hour
    saving = wasted_per_hang * hangs_per_month
    return Estimation(
        method="glue_timeout_guardrail_v1",
        baseline_cost=round(wasted_per_hang, 2),
        projected_cost=0.0,
        estimated_saving=round(saving, 2),
        assumptions=[
            f"timeout {job.timeout_min} min vs. duração média {exec_min:.0f} min",
            f"~{hangs_per_month:.1f} execução travada/mês, janela limitada a {detection_horizon_min:.0f} min",
            "alinhar o timeout corta a cobrança de um job pendurado mais cedo",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def worker_type_downgrade_saving(job: GlueJob, config: Config) -> tuple[Estimation, str]:
    """Worker type grande (G.4X/G.8X) com CPU baixa → um type menor bastaria."""
    pricing = config.pricing
    new_type = _DOWNGRADE.get(job.worker_type, job.worker_type)
    cur_dpu = _TYPE_DPU.get(job.worker_type, job.dpu_per_worker)
    new_dpu = _TYPE_DPU.get(new_type, cur_dpu)
    baseline = _monthly_cost(job, pricing)
    ratio = (cur_dpu - new_dpu) / cur_dpu if cur_dpu else 0
    saving = baseline * ratio
    est = Estimation(
        method="glue_worker_type_downgrade_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"worker type {job.worker_type} ({cur_dpu} DPU) → {new_type} ({new_dpu} DPU)",
            f"CPU média {(job.avg_cpu_load or 0):.0%} não justifica o type maior",
            "mesmo paralelismo (nº de workers) mantido",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
    return est, new_type


def failure_waste_saving(job: GlueJob, config: Config) -> Estimation:
    """Execuções que falham cobram DPU-hora até o ponto da falha (desperdício).

    O ganho é o desperdício recuperável ao corrigir a causa das falhas/retries.
    """
    pricing = config.pricing
    failed_runs = job.runs_per_month * job.failure_rate
    fail_exec = job.avg_failed_execution_sec or (job.avg_execution_sec * 0.5)
    total_dpu = job.number_of_workers * job.dpu_per_worker
    wasted_hours = failed_runs * total_dpu * (fail_exec / 3600.0)
    baseline = wasted_hours * pricing.glue_dpu_hour
    return Estimation(
        method="glue_failure_waste_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=0.0,
        estimated_saving=round(baseline, 2),
        assumptions=[
            f"taxa de falha {job.failure_rate:.0%} de {job.runs_per_month} execuções/mês",
            "Glue cobra DPU-hora do compute até a falha (retries incluídos)",
            f"compute médio até a falha ~{fail_exec / 60:.0f} min",
            "corrigir a causa recupera o desperdício",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def worker_reduction_saving(job: GlueJob, config: Config) -> Estimation:
    """Redução de workers proporcional à utilização observada."""
    pricing = config.pricing
    util = job.avg_cpu_load if job.avg_cpu_load is not None else config.thresholds.low_cpu
    recommended = max(
        config.thresholds.session_min_dpu,
        math.ceil(job.number_of_workers * util / config.thresholds.utilization_target),
    )
    recommended = min(recommended, job.number_of_workers)
    baseline = _monthly_cost(job, pricing)
    ratio = (job.number_of_workers - recommended) / job.number_of_workers if job.number_of_workers else 0
    saving = baseline * ratio
    return Estimation(
        method="glue_worker_reduction_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            "mesmo volume processado",
            f"workers {job.number_of_workers} → {recommended} (utilização {util:.0%})",
            "sem contenção de memória observada",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
