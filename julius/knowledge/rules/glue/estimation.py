"""Modelos financeiros para Glue Jobs (Auto Scaling e redução de workers)."""

from __future__ import annotations

import math

from julius.collection.models import GlueJob
from julius.config import Config
from julius.findings.opportunity import Estimation


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
            f"tarifa de {_rate(job, pricing):.4f} USD/DPU-h · {pricing.provenance}",
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
    """Potencial de Auto Scaling; só financeiro após benchmark comparável."""
    pricing = config.pricing
    util = (
        job.avg_worker_utilization
        if job.avg_worker_utilization is not None
        else (
            job.avg_cpu_load
            if job.avg_cpu_load is not None
            else config.thresholds.low_cpu
        )
    )
    ratio = max(0.0, min(0.6, 1 - util / config.thresholds.utilization_target))
    baseline, source, quality = _baseline(job, pricing)
    saving = baseline * ratio
    validated = _capacity_benchmark_validated(job)
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
        saving_quality="modeled_evidence" if validated else "potential",
        is_strategic=not validated,
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


def bookmark_saving(job: GlueJob, config: Config) -> Estimation:
    """Valoriza somente bytes redundantes medidos; não assume percentual fixo."""
    pricing = config.pricing
    baseline, source, quality = _baseline(job, pricing)
    total = job.bytes_read_window
    redundant = job.redundant_read_bytes_window
    measured = (
        job.incremental_source_evidence
        and total is not None
        and total > 0
        and redundant is not None
        and redundant >= 0
    )
    if not measured:
        return Estimation(
            method="glue_bookmark_measured_reprocessing_v2",
            baseline_cost=round(baseline, 2),
            projected_cost=round(baseline, 2),
            estimated_saving=0.0,
            assumptions=[
                "fonte incremental e bytes redundantes ainda não comprovados",
                "nenhum percentual genérico de reprocessamento foi aplicado",
                source,
            ],
            pricing_region=pricing.region,
            estimation_version=pricing.version,
            baseline_quality=quality,
            saving_quality="unavailable",
        )
    redundant_bytes = float(redundant or 0.0)
    total_bytes = float(total or 0.0)
    ratio = min(1.0, max(0.0, redundant_bytes / total_bytes))
    saving = baseline * ratio
    return Estimation(
        method="glue_bookmark_measured_reprocessing_v2",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        estimated_saving_low=round(saving, 2),
        estimated_saving_high=round(saving, 2),
        assumptions=[
            f"{redundant:.0f} de {total:.0f} bytes lidos foram redundantes",
            f"fração redundante medida={ratio:.1%}",
            "fonte incremental comprovada",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
        saving_quality="measured",
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


def timeout_guardrail_saving(job: GlueJob, config: Config) -> Estimation:
    """Só quantifica timeouts que realmente ocorreram na janela."""
    pricing = config.pricing
    exec_min = (job.p95_execution_sec or job.avg_execution_sec) / 60.0
    rate, source, quality = billing_rate(job, pricing)
    timeout_runs = int(job.failure_categories.get("timeout", 0))
    failed_runs = max(0, int(job.failed_runs_in_window or 0))
    if timeout_runs <= 0:
        return Estimation(
            method="glue_timeout_guardrail_v2",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                f"timeout {job.timeout_min} min vs. duração p95 {exec_min:.0f} min",
                "nenhuma execução atingiu timeout na janela; valor é risco evitado",
            ],
            baseline_quality=quality,
            saving_quality="unavailable",
            is_strategic=True,
        )
    if job.monthly_failed_cost is not None and failed_runs > 0:
        saving = job.monthly_failed_cost * timeout_runs / failed_runs
        quality = (
            "allocated"
            if job.failure_cost_quality == "reconciled"
            else "allocated_partial"
        )
        source = "custo de falhas rateado pela proporção de timeouts"
    else:
        timeout_hours = (
            job.total_failed_dpu_hours_window
            * job.monthly_factor
            * timeout_runs
            / max(1, failed_runs or timeout_runs)
        )
        saving = timeout_hours * rate
        source = f"{timeout_hours:.2f} DPU-h de timeouts × tarifa"
    return Estimation(
        method="glue_timeout_guardrail_v2",
        baseline_cost=round(saving, 2),
        projected_cost=0.0,
        estimated_saving=round(saving, 2),
        assumptions=[
            f"timeout {job.timeout_min} min vs. duração média {exec_min:.0f} min",
            f"{timeout_runs} execução(ões) atingiram timeout na janela",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
        saving_quality=(
            "measured" if quality.startswith("allocated") else "modeled_evidence"
        ),
    )


def worker_type_downgrade_saving(job: GlueJob, config: Config) -> tuple[Estimation, str]:
    """Candidato a worker menor; cifra só após benchmark comparável."""
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
    validated = _capacity_benchmark_validated(job)
    est = Estimation(
        method="glue_worker_type_downgrade_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"worker type {job.worker_type} ({cur_dpu} DPU) → {new_type} ({new_dpu} DPU)",
            f"CPU média {(job.avg_cpu_load or 0):.0%}; hipótese a validar",
            "mesmo paralelismo (nº de workers) mantido",
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
        saving_quality="modeled_evidence" if validated else "potential",
        is_strategic=not validated,
    )
    return est, new_type


def _capacity_benchmark_validated(job: GlueJob) -> bool:
    return bool(
        job.rightsize_test_runs >= 3
        and job.rightsize_output_validated
        and job.rightsize_tested_workers is not None
    )


def failure_waste_saving(job: GlueJob, config: Config) -> Estimation:
    """Execuções que falham cobram DPU-hora até o ponto da falha (desperdício).

    O ganho é o desperdício recuperável ao corrigir a causa das falhas/retries.
    """
    pricing = config.pricing
    if (
        job.failed_dpu_seconds_window is None
        and job.estimated_failed_dpu_hours_window is None
    ):
        # Contrato anterior: mantém exatamente o modelo v1 ao ler datasets
        # existentes, sem fingir que a nova telemetria foi coletada e deu zero.
        failed_runs = job.runs_per_month * job.failure_rate
        fail_exec = job.avg_failed_execution_sec or (job.avg_execution_sec * 0.5)
        rate, source, quality = billing_rate(job, pricing)
        wasted_hours = (
            failed_runs * job.configured_dpu * (fail_exec / 3600.0)
        )
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

    rate, source, quality = billing_rate(job, pricing)
    failed_hours = job.total_failed_dpu_hours_window * job.monthly_factor
    if job.monthly_failed_cost is not None:
        baseline = job.monthly_failed_cost
        quality = (
            "allocated"
            if job.failure_cost_quality == "reconciled"
            else "allocated_partial"
        )
        cost_source = (
            "parcela do custo alocado do Cost Explorer proporcional à DPU-hora "
            f"das falhas (qualidade {job.failure_cost_quality})"
        )
    elif failed_hours > 0:
        baseline = failed_hours * rate
        cost_source = (
            f"{failed_hours:.2f} DPU-h/mês de falhas medidas/modeladas × tarifa"
        )
    else:
        # Coleta nova sem capacidade suficiente para quantificar a DPU-hora.
        failed_runs = job.runs_per_month * job.failure_rate
        fail_exec = job.avg_failed_execution_sec or (job.avg_execution_sec * 0.5)
        failed_hours = failed_runs * job.configured_dpu * (fail_exec / 3600.0)
        baseline = failed_hours * rate
        cost_source = "fallback: taxa × duração média × capacidade configurada"
    return Estimation(
        method="glue_failure_waste_v2",
        baseline_cost=round(baseline, 2),
        projected_cost=0.0,
        estimated_saving=round(baseline, 2),
        assumptions=[
            f"taxa de falha {job.failure_rate:.0%} de {job.runs_per_month} execuções/mês",
            "Glue cobra DPU-hora do compute até a falha (retries incluídos)",
            f"{failed_hours:.2f} DPU-h/mês atribuídas às falhas",
            cost_source,
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
    util = (
        job.avg_worker_utilization
        if job.avg_worker_utilization is not None
        else (
            job.avg_cpu_load
            if job.avg_cpu_load is not None
            else config.thresholds.low_cpu
        )
    )
    current_workers = max(0, job.number_of_workers or 0)
    modeled_recommended = max(
        config.thresholds.session_min_dpu,
        math.ceil(current_workers * util / config.thresholds.utilization_target),
    )
    modeled_recommended = min(modeled_recommended, current_workers)
    benchmark_validated = bool(
        job.rightsize_tested_workers is not None
        and job.rightsize_test_runs >= 3
        and job.rightsize_output_validated
        and 2 <= job.rightsize_tested_workers < current_workers
    )
    recommended = (
        int(job.rightsize_tested_workers)
        if benchmark_validated and job.rightsize_tested_workers is not None
        else modeled_recommended
    )
    baseline, source, quality = _baseline(job, pricing)
    ratio = (current_workers - recommended) / current_workers if current_workers else 0
    saving = baseline * ratio
    return Estimation(
        method="glue_worker_reduction_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        estimated_saving_low=round(saving, 2) if benchmark_validated else 0.0,
        estimated_saving_high=round(saving, 2),
        assumptions=[
            "mesmo volume processado",
            f"workers {current_workers} → {recommended} (utilização {util:.0%})",
            "redução só é acionável com memória, disco e spill observados",
            (
                f"benchmark validado em {job.rightsize_test_runs} runs"
                if benchmark_validated
                else "ganho potencial até concluir três runs por candidato"
            ),
            source,
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
        baseline_quality=quality,
        saving_quality=(
            "modeled_evidence" if benchmark_validated else "potential"
        ),
        is_strategic=not benchmark_validated,
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
    projected = baseline * 0.0625 / job.configured_dpu if job.configured_dpu > 0 else baseline
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
