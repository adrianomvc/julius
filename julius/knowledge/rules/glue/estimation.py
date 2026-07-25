"""Modelos financeiros para Glue Jobs (Auto Scaling e redução de workers)."""

from __future__ import annotations

import math

from julius.collection.models import GlueJob
from julius.config import Config
from julius.opportunities.base import Estimation


def _rate(job: GlueJob, pricing, execution_class: str | None = None) -> float:
    """Tarifa versionada de tabela (USD/DPU-hora)."""
    if job.capacity_unit == "M-DPU":
        return pricing.glue_ray_mdpu_hour
    return pricing.glue_rate(execution_class or job.execution_class)


def _measured_rate(job: GlueJob) -> float | None:
    """USD/DPU-hora implícito na cobrança alocada do mês, quando existir."""
    if job.allocated_cost is None or job.total_dpu_hours_window <= 0:
        return None
    return job.allocated_cost / job.total_dpu_hours_window


def billing_rate(job: GlueJob, pricing) -> tuple[float, str, str]:
    """Tarifa efetiva do job: fatura rateada quando há, tabela quando não há.

    Devolve `(tarifa, premissa, baseline_quality)`. O custo alocado vem do
    rateio do bucket do Cost Explorer pelas DPU-horas coletadas — é custo
    alocado, nunca fatura por job.
    """
    measured = _measured_rate(job)
    if measured is None:
        return (
            _rate(job, pricing),
            f"tarifa versionada de {_rate(job, pricing):.4f} USD/DPU-h",
            "modeled",
        )
    quality = "allocated" if job.cost_quality == "reconciled" else "allocated_partial"
    return (
        measured,
        (
            f"custo alocado do Cost Explorer ({measured:.4f} USD/DPU-h "
            f"observado no mês, qualidade {job.cost_quality})"
        ),
        quality,
    )


def _baseline(job: GlueJob, pricing) -> tuple[float, str, str]:
    """Custo mensal do job ancorado na fatura quando a alocação existe."""
    rate, source, quality = billing_rate(job, pricing)
    return job.window_dpu_hours * rate, source, quality


def autoscaling_saving(job: GlueJob, config: Config) -> Estimation:
    """Auto Scaling remove workers ociosos: saving ∝ (1 − util/alvo)."""
    pricing = config.pricing
    util = job.avg_cpu_load if job.avg_cpu_load is not None else config.thresholds.low_cpu
    ratio = max(0.0, min(0.6, 1 - util / config.thresholds.utilization_target))
    baseline, source, quality = _baseline(job, pricing)
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
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


def version_upgrade_saving(job: GlueJob, config: Config) -> Estimation:
    """Glue <2.0 fatura em blocos de 10 min; 2.0+ em 1 min. Ganho no arredondamento."""
    pricing = config.pricing
    exec_min = job.avg_execution_sec / 60.0
    billed_old = max(10.0, exec_min)
    billed_new = max(1.0, exec_min)
    total_dpu = job.configured_dpu
    rate, source, quality = billing_rate(job, pricing)
    per_run = (billed_old - billed_new) / 60.0 * total_dpu * rate
    saving = max(0.0, per_run * job.runs_per_month)
    baseline = billed_old / 60.0 * total_dpu * rate * job.runs_per_month
    return Estimation(
        method="glue_version_upgrade_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"GlueVersion {job.glue_version} fatura mín. 10 min; 2.0+ fatura mín. 1 min",
            f"duração média {exec_min:.1f} min · {job.runs_per_month} execuções/mês",
            "mesma configuração de workers",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


def flex_saving(job: GlueJob, config: Config) -> Estimation:
    """Aplica ao custo do job a diferença tarifária versionada STANDARD→FLEX."""
    pricing = config.pricing
    baseline, source, quality = _baseline(job, pricing)
    standard = pricing.glue_rate("STANDARD")
    # Ray fatura em M-DPU e não tem classe FLEX; sem desconto aplicável.
    discount = (
        0.0
        if job.capacity_unit == "M-DPU" or not standard
        else max(0.0, 1 - pricing.glue_rate("FLEX") / standard)
    )
    projected = baseline * (1 - discount)
    saving = max(0.0, baseline - projected)
    return Estimation(
        method="glue_flex_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(projected, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            "job em batch, não sensível a tempo (sem SLA rígido)",
            (
                f"tarifa versionada STANDARD={pricing.glue_dpu_hour:.4f} "
                f"e FLEX={pricing.glue_flex_dpu_hour:.4f} USD/DPU-h"
            ),
            f"diferença tarifária modelada de {discount:.0%}",
            "tolera variação no tempo de início",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


def bookmark_saving(job: GlueJob, config: Config, reprocess: float = 0.25) -> Estimation:
    """Job bookmarks evitam reprocessar dados já processados (processamento incremental)."""
    pricing = config.pricing
    baseline, source, quality = _baseline(job, pricing)
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
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


# Ordem de downgrade de worker type (DPU por worker).
_TYPE_DPU = {
    "G.1X": 1,
    "G.2X": 2,
    "G.4X": 4,
    "G.8X": 8,
    "G.12X": 12,
    "G.16X": 16,
    "R.1X": 1,
    "R.2X": 2,
    "R.4X": 4,
    "R.8X": 8,
}
_DOWNGRADE = {
    "G.16X": "G.12X",
    "G.12X": "G.8X",
    "G.8X": "G.4X",
    "G.4X": "G.2X",
    "G.2X": "G.1X",
    "R.8X": "R.4X",
    "R.4X": "R.2X",
    "R.2X": "R.1X",
}


def timeout_guardrail_saving(
    job: GlueJob, config: Config, hangs_per_month: float = 0.3, detection_horizon_min: float = 240.0
) -> Estimation:
    """Timeout muito acima da duração média: um job travado cobra DPU-hora até o
    timeout. Guardrail conservador — limita a janela desperdiçada ao horizonte
    realista de detecção (um job pendurado é notado em ~4h, não nas 48h do timeout)."""
    pricing = config.pricing
    exec_min = (job.p95_execution_sec or job.avg_execution_sec) / 60.0
    total_dpu = job.configured_dpu
    rate, source, quality = billing_rate(job, pricing)
    wasted_min = min(max(0.0, job.timeout_min - exec_min), detection_horizon_min)
    wasted_per_hang = wasted_min / 60.0 * total_dpu * rate
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
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


def worker_type_downgrade_saving(job: GlueJob, config: Config) -> tuple[Estimation, str]:
    """Worker type grande (G.4X/G.8X) com CPU baixa → um type menor bastaria."""
    pricing = config.pricing
    # `worker_type` é opcional no modelo (jobs em MaxCapacity não têm), mas a
    # regra só chega aqui com um type conhecido; o fallback mantém isso explícito.
    current_type = job.worker_type or ""
    new_type = _DOWNGRADE.get(current_type, current_type)
    cur_dpu = _TYPE_DPU.get(current_type, job.dpu_per_worker)
    new_dpu = _TYPE_DPU.get(new_type, cur_dpu)
    baseline, source, quality = _baseline(job, pricing)
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
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )
    return est, new_type


def failure_waste_saving(job: GlueJob, config: Config) -> Estimation:
    """Execuções que falham cobram DPU-hora até o ponto da falha (desperdício).

    O ganho é o desperdício recuperável ao corrigir a causa das falhas/retries.
    """
    pricing = config.pricing
    failed_runs = job.runs_per_month * job.failure_rate
    fail_exec = job.avg_failed_execution_sec or (job.avg_execution_sec * 0.5)
    total_dpu = job.configured_dpu
    rate, source, quality = billing_rate(job, pricing)
    wasted_hours = failed_runs * total_dpu * (fail_exec / 3600.0)
    baseline = wasted_hours * rate
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
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


def worker_reduction_saving(job: GlueJob, config: Config) -> Estimation:
    """Redução de workers proporcional à utilização observada."""
    pricing = config.pricing
    util = job.avg_cpu_load if job.avg_cpu_load is not None else config.thresholds.low_cpu
    current_workers = max(0, job.number_of_workers or 0)
    recommended = max(
        config.thresholds.session_min_dpu,
        math.ceil(current_workers * util / config.thresholds.utilization_target),
    )
    recommended = min(recommended, current_workers)
    baseline, source, quality = _baseline(job, pricing)
    ratio = (current_workers - recommended) / current_workers if current_workers else 0
    saving = baseline * ratio
    return Estimation(
        method="glue_worker_reduction_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            "mesmo volume processado",
            f"workers {current_workers} → {recommended} (utilização {util:.0%})",
            "redução só é acionável com memória, disco e spill observados",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


def code_pattern_saving(
    job: GlueJob,
    config: Config,
    *,
    method: str,
    potential_fraction: float,
    assumption: str,
) -> Estimation:
    """Potencial conservador para antipadrão estático, pendente de benchmark."""
    pricing = config.pricing
    baseline, source, quality = _baseline(job, pricing)
    fraction = max(0.0, min(0.30, potential_fraction))
    saving = baseline * fraction
    return Estimation(
        method=method,
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            assumption,
            "potencial condicionado a benchmark A/B com o mesmo volume de entrada",
            "a recomendação permanece bloqueada até validar duração e resultado",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )


def python_shell_migration_saving(job: GlueJob, config: Config) -> Estimation:
    """Compara o consumo Spark atual com Python Shell a 0,0625 DPU.

    A duração equivalente é somente hipótese inicial; a oportunidade permanece
    bloqueada até um piloto confirmar runtime, memória, dependências e resultado.
    """
    pricing = config.pricing
    baseline, source, quality = _baseline(job, pricing)
    projected = (
        baseline * 0.0625 / job.configured_dpu
        if job.configured_dpu > 0
        else baseline
    )
    projected = min(baseline, projected)
    return Estimation(
        method="glue_spark_to_python_shell_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(projected, 2),
        estimated_saving=round(max(0.0, baseline - projected), 2),
        assumptions=[
            "script sem APIs Spark/Glue distribuídas detectadas",
            "Python Shell configurado com 0,0625 DPU",
            "duração inicialmente assumida igual à média atual",
            "piloto obrigatório para validar memória, dependências e duração",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
    )
