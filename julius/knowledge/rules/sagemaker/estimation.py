"""Modelos financeiros determinísticos para SageMaker."""

from __future__ import annotations

import math

from julius.collection.models import (
    SageMakerApp,
    SageMakerDomain,
    SageMakerEndpoint,
    SageMakerFeatureGroup,
    SageMakerJob,
    SageMakerSpace,
)
from julius.config import Config
from julius.findings.opportunity import Estimation

_HOURS_MONTH = 730.0


def unavailable(method: str, *missing: str) -> Estimation:
    return Estimation(
        method=method,
        baseline_cost=0.0,
        projected_cost=0.0,
        estimated_saving=0.0,
        assumptions=[*missing, "economia não quantificada sem a evidência"],
        baseline_quality="unavailable",
        saving_quality="unavailable",
    )


def idle_app_saving(app: SageMakerApp, config: Config) -> Estimation:
    idle_fraction = min(1.0, max(0.0, app.idle_hours_per_day / 24.0))
    allocated = _monthly_allocated(
        app.allocated_cost,
        app.cost_coverage_days or app.coverage_days,
    )
    if allocated is not None:
        saving = allocated * idle_fraction
        return Estimation(
            method="sm_idle_app_v2_ce",
            baseline_cost=round(saving, 2),
            projected_cost=0.0,
            estimated_saving=round(saving, 2),
            assumptions=[
                "custo do app rateado do Cost Explorer",
                f"{idle_fraction:.1%} do tempo classificado como baixa atividade",
            ],
            pricing_region=config.pricing.region,
            estimation_version=config.sagemaker_cost.version,
            baseline_quality="allocated",
            saving_quality="modeled_rule",
        )
    hourly = config.pricing.sagemaker_hourly(app.instance_type, "studio")
    if hourly is None:
        return unavailable(
            "sm_idle_app_v2",
            f"tarifa Studio ausente para {app.instance_type}",
        )
    idle_hours_month = app.idle_hours_per_day * app.active_days_per_month
    saving = idle_hours_month * hourly
    return Estimation(
        method="sm_idle_app_v2",
        baseline_cost=round(saving, 2),
        projected_cost=0.0,
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{app.instance_type} Studio a USD {hourly:.4f}/h",
            f"~{app.idle_hours_per_day:.1f}h de baixa atividade/dia",
            "CPU é proxy; idle oficial depende de kernels e terminais",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
        baseline_quality="modeled",
        saving_quality="modeled_rule",
    )


def space_storage_saving(
    space: SageMakerSpace, config: Config
) -> Estimation:
    allocated = _monthly_allocated(
        space.allocated_storage_cost,
        space.cost_coverage_days or space.coverage_days,
    )
    if allocated is None:
        return unavailable(
            "sm_space_storage_idle_v1",
            "custo EBS do Space não rateado",
        )
    return Estimation(
        method="sm_space_storage_idle_v1",
        baseline_cost=round(allocated, 2),
        projected_cost=0.0,
        estimated_saving=round(allocated, 2),
        estimated_saving_low=0.0,
        estimated_saving_high=round(allocated, 2),
        assumptions=[
            "custo de volume Studio rateado do Cost Explorer",
            "nenhum app ativo no Space",
            "ganho potencial condicionado a backup, retenção e aprovação do owner",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.sagemaker_cost.version,
        baseline_quality="allocated",
        saving_quality="potential",
        is_strategic=True,
    )


def domain_storage_saving(
    domain: SageMakerDomain, config: Config
) -> Estimation:
    allocated = _monthly_allocated(
        domain.allocated_storage_cost,
        domain.cost_coverage_days or domain.coverage_days,
    )
    if allocated is None:
        return unavailable(
            "sm_domain_efs_storage_idle_v1",
            "custo EFS de storage não rateado ao Domain",
        )
    return Estimation(
        method="sm_domain_efs_storage_idle_v1",
        baseline_cost=round(allocated, 2),
        projected_cost=0.0,
        estimated_saving=round(allocated, 2),
        estimated_saving_low=0.0,
        estimated_saving_high=round(allocated, 2),
        assumptions=[
            "storage EFS rateado por bytes medidos entre Domains conhecidos",
            "zero apps ativos, I/O e conexões na janela",
            "ganho potencial condicionado a consumidores externos e retenção",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.sagemaker_cost.version,
        baseline_quality="allocated",
        saving_quality="potential",
        is_strategic=True,
    )


def endpoint_idle_saving(
    endpoint: SageMakerEndpoint, config: Config, method: str
) -> Estimation:
    allocated = _monthly_allocated(
        endpoint.allocated_cost,
        endpoint.cost_coverage_days or endpoint.coverage_days,
    )
    if allocated is not None:
        return Estimation(
            method=method + "_ce",
            baseline_cost=round(allocated, 2),
            projected_cost=0.0,
            estimated_saving=round(allocated, 2),
            assumptions=[
                "capacidade rateada da cobrança real do componente",
                "cenário zero só é elegível porque não houve tráfego na janela",
            ],
            pricing_region=config.pricing.region,
            estimation_version=config.sagemaker_cost.version,
            baseline_quality="allocated",
            saving_quality="measured",
        )
    monthly = 0.0
    missing: list[str] = []
    variants = endpoint.variants or []
    if variants:
        for variant in variants:
            count = max(
                variant.current_instance_count,
                variant.desired_instance_count,
                variant.initial_instance_count,
            )
            rate = config.pricing.sagemaker_hourly(
                variant.instance_type, "endpoint"
            )
            if count and rate is None:
                missing.append(variant.instance_type)
            elif rate is not None:
                monthly += count * rate * _HOURS_MONTH
    else:
        rate = config.pricing.sagemaker_hourly(
            endpoint.instance_type, "endpoint"
        )
        if rate is None:
            missing.append(endpoint.instance_type)
        else:
            monthly = endpoint.instance_count * rate * _HOURS_MONTH
    if missing or monthly <= 0:
        return unavailable(
            method,
            "tarifa de endpoint ausente para " + ", ".join(sorted(set(missing))),
        )
    return Estimation(
        method=method,
        baseline_cost=round(monthly, 2),
        projected_cost=0.0,
        estimated_saving=round(monthly, 2),
        assumptions=["todas as variantes do endpoint, 730 h/mês"],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
    )


def failed_job_cost(job: SageMakerJob, config: Config) -> Estimation:
    cost = job.allocated_cost if job.allocated_cost is not None else job.modeled_cost
    if cost is None or cost <= 0:
        # O motivo vem do coletor: "describe negado", "sem tarifa para o tipo" e
        # "não chegou a iniciar" pedem ações diferentes de quem lê, e viravam a
        # mesma frase.
        return unavailable(
            f"sm_{job.kind}_failed_v1",
            job.cost_unavailable_reason
            or f"custo não atribuído ao job {job.name}",
        )
    recurring = job.consistent_scans >= 2
    return Estimation(
        method=f"sm_{job.kind}_failed_v1",
        baseline_cost=round(cost, 2),
        projected_cost=0.0,
        # Falha isolada é desperdício realizado, não promessa de economia anual.
        estimated_saving=round(cost, 2) if recurring else 0.0,
        assumptions=[
            "custo faturável observado da execução que terminou Failed",
            (
                "padrão repetido: valor tratado como recorrente"
                if recurring
                else "execução isolada: não anualizada"
            ),
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.sagemaker_cost.version,
        baseline_quality=(
            "allocated" if job.allocated_cost is not None else "modeled"
        ),
        saving_quality="measured" if recurring else "one_time_observed",
        one_time_cost=round(cost, 2),
        monthly_recurring_saving=round(cost, 2) if recurring else 0.0,
    )


def warm_pool_waste(job: SageMakerJob, config: Config) -> Estimation:
    rate = config.pricing.sagemaker_hourly(job.instance_type, "training")
    if rate is None:
        return unavailable(
            "sm_training_warm_pool_v1",
            f"tarifa training ausente para {job.instance_type}",
        )
    cost = (
        job.warm_pool_billable_seconds
        * max(1, job.instance_count)
        / 3600.0
        * rate
    )
    return Estimation(
        method="sm_training_warm_pool_v1",
        baseline_cost=round(cost, 2),
        projected_cost=0.0,
        estimated_saving=0.0,
        assumptions=[
            "tempo faturável retido informado pelo WarmPoolStatus",
            "custo pontual observado; não anualizado",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
        one_time_cost=round(cost, 2),
        monthly_recurring_saving=0.0,
        saving_quality="one_time_observed",
    )


def feature_store_capacity(
    group: SageMakerFeatureGroup, config: Config
) -> Estimation:
    allocated = _monthly_allocated(
        group.allocated_cost,
        group.cost_coverage_days or group.coverage_days,
    )
    if allocated is None:
        return unavailable(
            "sm_feature_store_provisioned_v1",
            "cobrança Feature Store não rateada ao feature group",
        )
    read_target = max(
        1, math.ceil(float(group.max_consumed_read_capacity or 0.0))
    )
    write_target = max(
        1, math.ceil(2.0 * float(group.max_consumed_write_capacity or 0.0))
    )
    current = (
        group.provisioned_read_capacity + group.provisioned_write_capacity
    )
    target = read_target + write_target
    if current <= 0 or target >= current:
        return unavailable(
            "sm_feature_store_provisioned_v1",
            "capacidade alvo não é menor que a atual",
        )
    projected = allocated * target / current
    saving = allocated - projected
    return Estimation(
        method="sm_feature_store_provisioned_v1",
        baseline_cost=round(allocated, 2),
        projected_cost=round(projected, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"leitura alvo={read_target} RCU (máximo observado)",
            f"escrita alvo={write_target} WCU (2× pico observado)",
            "nenhum throttling na janela",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.sagemaker_cost.version,
        baseline_quality="allocated",
        saving_quality="modeled_rule",
    )


def recommender_saving(
    endpoint: SageMakerEndpoint,
    hourly_target: float,
    initial_count: int,
    config: Config,
) -> Estimation:
    baseline = _monthly_allocated(
        endpoint.allocated_cost,
        endpoint.cost_coverage_days or endpoint.coverage_days,
    )
    if baseline is None:
        return unavailable(
            "sm_inference_recommender_v1",
            "custo atual do endpoint não rateado",
        )
    projected = max(1, initial_count) * hourly_target * _HOURS_MONTH
    if projected >= baseline:
        return unavailable(
            "sm_inference_recommender_v1",
            "configuração recomendada não reduz o custo atual",
        )
    return Estimation(
        method="sm_inference_recommender_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(projected, 2),
        estimated_saving=round(baseline - projected, 2),
        assumptions=[
            "configuração e desempenho medidos pelo Inference Recommender",
            "730 h/mês para comparar com o endpoint atual",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.sagemaker_cost.version,
        baseline_quality="allocated",
        saving_quality="aws_recommendation",
    )


#: O alvo de comparação quando o script não usa a GPU que a conta paga. `m5` é
#: a família de propósito geral da mesma geração das GPUs em uso, e manter o
#: mesmo sufixo de tamanho mantém a ordem de grandeza de vCPU e memória. É
#: hipótese de comparação, não recomendação de tipo — a validação do achado
#: exige medir CPU e memória no piloto antes de fixar a instância.
_CPU_TARGET_FAMILY = "ml.m5."


def gpu_to_cpu_saving(job: SageMakerJob, config: Config) -> Estimation:
    """Diferença entre a tarifa paga com GPU e a de uma instância sem GPU.

    O baseline é o custo já atribuído ao job — não uma reconstrução. O que a
    regra estima é só o outro lado: a mesma duração numa instância de propósito
    geral do mesmo tamanho. Se a tabela da região não tiver a tarifa do alvo, a
    estimativa não existe, e dizer isso é a resposta certa: um zero aqui se
    leria como "não compensa trocar".
    """
    method = "sm_code_gpu_to_cpu_v1"
    baseline = job.allocated_cost if job.allocated_cost is not None else job.modeled_cost
    if baseline is None or baseline <= 0:
        return unavailable(
            method, job.cost_unavailable_reason or f"custo não atribuído ao job {job.name}"
        )
    atual = config.pricing.sagemaker_hourly(job.instance_type, job.kind)
    tamanho = job.instance_type.split(".")[-1] if "." in job.instance_type else ""
    alvo = config.pricing.sagemaker_hourly(f"{_CPU_TARGET_FAMILY}{tamanho}", job.kind)
    if not atual or not alvo or alvo >= atual:
        return unavailable(
            method,
            f"tarifa de comparação ausente para {_CPU_TARGET_FAMILY}{tamanho} "
            f"na região {config.pricing.region}",
        )
    projetado = baseline * (alvo / atual)
    return Estimation(
        method=method,
        baseline_cost=round(baseline, 2),
        projected_cost=round(projetado, 2),
        estimated_saving=round(baseline - projetado, 2),
        assumptions=[
            f"mesma duração em {_CPU_TARGET_FAMILY}{tamanho} a "
            f"{alvo:.4g} USD/h contra {atual:.4g} USD/h",
            "duração equivalente é hipótese: sem GPU em uso, o tempo tende a "
            "não mudar, mas só o piloto confirma",
            "o tipo alvo definitivo depende do perfil de CPU e memória medido",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.sagemaker_cost.version,
        baseline_quality="allocated" if job.allocated_cost is not None else "modeled",
        saving_quality="modeled",
    )


def idle_instances_saving(job: SageMakerJob, config: Config) -> Estimation:
    """As instâncias que o cluster cobra e o script não usa.

    O rateio é direto e não depende de hipótese sobre duração: o cluster é
    provisionado inteiro pelo mesmo tempo, então cada instância responde pela
    mesma fração do custo. Sem API distribuída no script, todas menos uma são
    capacidade paga sem trabalho.
    """
    method = "sm_code_idle_instances_v1"
    baseline = job.allocated_cost if job.allocated_cost is not None else job.modeled_cost
    if baseline is None or baseline <= 0:
        return unavailable(
            method, job.cost_unavailable_reason or f"custo não atribuído ao job {job.name}"
        )
    if job.instance_count <= 1:
        return unavailable(method, "cluster de uma instância: não há capacidade ociosa")
    ociosas = job.instance_count - 1
    saving = baseline * (ociosas / job.instance_count)
    return Estimation(
        method=method,
        baseline_cost=round(baseline, 2),
        projected_cost=round(baseline - saving, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{ociosas} de {job.instance_count} instâncias sem trabalho atribuível "
            "pelo script",
            "cluster provisionado pelo mesmo tempo: custo dividido igualmente",
            "encolher o cluster mantém a duração; distribuir de fato a reduziria",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.sagemaker_cost.version,
        baseline_quality="allocated" if job.allocated_cost is not None else "modeled",
        saving_quality="modeled",
    )


def _monthly_allocated(value: float | None, coverage_days: int) -> float | None:
    if value is None or value <= 0:
        return None
    return value * 30.0 / max(1, coverage_days)
