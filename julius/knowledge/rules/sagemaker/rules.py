"""Regras e sinais SageMaker sustentados por configuração e métricas."""

from __future__ import annotations

import math

from julius.collection.models import (
    Account,
    SageMakerApp,
    SageMakerDomain,
    SageMakerEndpoint,
    SageMakerFeatureGroup,
    SageMakerInferenceRecommendation,
    SageMakerJob,
    SageMakerSpace,
)
from julius.config import Config, is_gpu_instance
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal
from julius.knowledge.rules.sagemaker import estimation as sm_est
from julius.knowledge.signal_potential import potential_from_estimate

_DOC_IDLE = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/"
    "studio-updated-idle-shutdown.html"
)
_DOC_METRICS = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/monitoring-cloudwatch.html"
)
_DOC_SERVERLESS = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/serverless-endpoints.html"
)
_DOC_ASYNC = "https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference.html"
_DOC_ASYNC_ZERO = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/"
    "async-inference-autoscale.html"
)
_DOC_SCALE_ZERO = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/"
    "endpoint-auto-scaling-zero-instances.html"
)
_DOC_RECOMMENDER = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/inference-recommender.html"
)
_DOC_SPOT = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/model-managed-spot-training.html"
)
_DOC_WARM_POOL = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/train-warm-pools.html"
)
_DOC_FEATURE_THROUGHPUT = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/"
    "feature-store-throughput-mode.html"
)
_DOC_FEATURE_TTL = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/"
    "feature-store-time-to-live.html"
)
_DOC_G3 = "https://docs.aws.amazon.com/ec2/latest/instancetypes/pg.html"
_DOC_ENDPOINT_TYPES = (
    "https://docs.aws.amazon.com/sagemaker/latest/APIReference/"
    "API_ProductionVariant.html"
)
_DOC_MODEL_MONITOR = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/model-monitor.html"
)
_DOC_SPACE_STORAGE = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/"
    "studio-updated-running-stop.html"
)
_DOC_EFS_METRICS = "https://docs.aws.amazon.com/efs/latest/ug/efs-metrics.html"


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    for app in account.sagemaker_apps:
        if _idle_app_candidate(app, config) and _financial_ready(app, config):
            out.append(_idle_app(account, app, config, scan_id))

    for space in account.sagemaker_spaces:
        if (
            space.ebs_volume_size_gb > 0
            and space.active_app_count == 0
            and _financial_ready(space, config)
            and (space.allocated_storage_cost or 0) > 0
        ):
            out.append(_idle_space(account, space, config, scan_id))

    for domain in account.sagemaker_domains:
        if (
            (domain.efs_storage_bytes or 0) > 0
            and domain.active_app_count == 0
            and domain.efs_total_io_bytes == 0
            and domain.efs_client_connections == 0
            and _financial_ready(domain, config)
            and (domain.allocated_storage_cost or 0) > 0
        ):
            out.append(_idle_domain(account, domain, config, scan_id))

    for endpoint in account.sagemaker_endpoints:
        if not _zero_traffic(endpoint) or not _financial_ready(endpoint, config):
            continue
        if endpoint.mode == "async" and endpoint.min_capacity > 0:
            out.append(
                _idle_endpoint(
                    account,
                    endpoint,
                    config,
                    scan_id,
                    rule_id="SM-ASYNC-MIN-CAPACITY-IDLE",
                    title="Async endpoint sem backlog ainda mantém capacidade mínima",
                    action="Avaliar MinCapacity=0 com scale-to-zero",
                    method="sm_async_min_capacity_v1",
                    docs=[_DOC_ASYNC, _DOC_ASYNC_ZERO],
                )
            )
        elif (
            endpoint.mode == "serverless"
            and endpoint.provisioned_concurrency > 0
        ):
            out.append(
                _idle_endpoint(
                    account,
                    endpoint,
                    config,
                    scan_id,
                    rule_id="SM-SERVERLESS-PC-IDLE",
                    title="Serverless provisionado sem invocações",
                    action="Avaliar redução da provisioned concurrency",
                    method="sm_serverless_pc_idle_v1",
                    docs=[_DOC_SERVERLESS],
                )
            )
        else:
            out.append(
                _idle_endpoint(
                    account,
                    endpoint,
                    config,
                    scan_id,
                    rule_id="SM-ENDPOINT-ZERO-TRAFFIC",
                    title="Endpoint com capacidade paga e tráfego zero",
                    action=(
                        "Validar desativação, Serverless, Async ou scale-to-zero"
                    ),
                    method="sm_endpoint_zero_traffic_v1",
                    docs=[_DOC_SERVERLESS, _DOC_ASYNC, _DOC_SCALE_ZERO],
                )
            )

    for job in account.sagemaker_jobs:
        if job.status.lower() == "failed":
            out.append(_failed_job(account, job, config, scan_id))
        if (
            job.kind == "training"
            and job.warm_pool_billable_seconds > 0
            and not job.warm_pool_reused
        ):
            out.append(_unused_warm_pool(account, job, config, scan_id))

    for group in account.sagemaker_feature_groups:
        if _feature_store_idle(group, config):
            out.append(_feature_store(account, group, config, scan_id))

    out.extend(_recommender_opportunities(account, config, scan_id))
    return out


def _idle_app_candidate(app: SageMakerApp, config: Config) -> bool:
    timeout = app.idle_shutdown_min
    metrics = app.activity_metrics_available or app.idle_hours_per_day > 0
    return bool(
        timeout is not None
        and metrics
        and app.status == "InService"
        and app.idle_hours_per_day >= config.thresholds.sm_idle_hours_min
        and (
            timeout == 0
            or timeout > config.thresholds.sm_idle_shutdown_high_min
        )
    )


def _idle_space(
    account: Account,
    space: SageMakerSpace,
    config: Config,
    scan_id: str,
) -> Opportunity:
    estimation = sm_est.space_storage_saving(space, config)
    opportunity = build(
        Finding(
            asset_type="sagemaker_space",
            asset_name=f"{space.domain_id}/{space.name}",
            rule_id="SM-SPACE-STORAGE-IDLE",
            rule_version="1.0.0",
            title="Space sem app ativo mantém volume EBS faturado",
            why=(
                f"{space.ebs_volume_size_gb} GiB persistem sem app ativo; "
                "o custo do volume foi rateado do Cost Explorer."
            ),
        ),
        Recommendation(
            difficulty=3,
            action="Validar retenção, realizar backup e avaliar remoção do Space",
            how_to_apply=(
                "O time dono deve validar os arquivos e realizar backup antes "
                "de qualquer remoção. O Julius não altera o Space."
            ),
            how_to_validate="Confirmar backup restaurável e queda do custo de storage.",
            risks=["remover o Space exclui definitivamente o volume e seus dados"],
            docs=[_DOC_SPACE_STORAGE],
            blocked=True,
        ),
        Evidence(
            items=[
                f"EBS={space.ebs_volume_size_gb} GiB",
                "apps ativos=0",
                f"cobertura={space.coverage_days} dias",
                f"custo alocado={space.allocated_storage_cost:.6f} USD",
            ],
            sources=["SageMaker DescribeSpace", "Cost Explorer"],
            observed_runs=max(1, space.consistent_scans),
            coverage_days=space.coverage_days,
            has_optional_metrics=True,
            owner_tag=space.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    opportunity.missing_evidence = [
        "necessidade dos dados, backup restaurável e aprovação do owner"
    ]
    return opportunity


def _idle_domain(
    account: Account,
    domain: SageMakerDomain,
    config: Config,
    scan_id: str,
) -> Opportunity:
    estimation = sm_est.domain_storage_saving(domain, config)
    opportunity = build(
        Finding(
            asset_type="sagemaker_domain",
            asset_name=domain.domain_id,
            rule_id="SM-DOMAIN-EFS-STORAGE-IDLE",
            rule_version="1.0.0",
            title="Domain mantém storage EFS sem atividade observada",
            why=(
                f"{(domain.efs_storage_bytes or 0) / 1024**3:.2f} GiB, "
                "zero I/O, zero conexões e nenhum app ativo na janela."
            ),
        ),
        Recommendation(
            difficulty=4,
            action="Validar consumidores externos e plano de retenção do EFS",
            how_to_apply=(
                "Inventariar consumidores, owner e backup antes de qualquer "
                "ação no Domain ou filesystem. O Julius não altera recursos."
            ),
            how_to_validate="Confirmar retenção e reconciliar o custo EFS no scan seguinte.",
            risks=[
                "o filesystem pode ser usado fora do Studio",
                "remoção pode causar perda permanente de dados",
            ],
            docs=[_DOC_SPACE_STORAGE, _DOC_EFS_METRICS],
            blocked=True,
        ),
        Evidence(
            items=[
                f"storage={(domain.efs_storage_bytes or 0) / 1024**3:.2f} GiB",
                "I/O total=0",
                "ClientConnections=0",
                "apps ativos=0",
                f"custo alocado={domain.allocated_storage_cost:.6f} USD",
            ],
            sources=["SageMaker DescribeDomain", "CloudWatch AWS/EFS", "Cost Explorer"],
            observed_runs=max(1, domain.consistent_scans),
            coverage_days=domain.coverage_days,
            has_optional_metrics=True,
            owner_tag=domain.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    opportunity.missing_evidence = [
        "consumidores externos, retenção, backup e aprovação do owner"
    ]
    return opportunity


def _financial_ready(asset, config: Config) -> bool:
    return bool(
        int(getattr(asset, "coverage_days", 0) or 0)
        >= config.thresholds.sm_financial_coverage_days
        or int(getattr(asset, "consistent_scans", 1) or 1)
        >= config.thresholds.sm_financial_consistent_scans
    )


def _zero_traffic(endpoint: SageMakerEndpoint) -> bool:
    measured = (
        endpoint.invocations
        if endpoint.invocations is not None
        else endpoint.invocations_per_month
    )
    return measured == 0


def _idle_app(
    account: Account, app: SageMakerApp, config: Config, scan_id: str
) -> Opportunity:
    estimation = sm_est.idle_app_saving(app, config)
    blocked = estimation.saving_quality == "unavailable"
    gpu = is_gpu_instance(app.instance_type)
    return build(
        Finding(
            asset_type="sagemaker_app",
            asset_name=app.name,
            rule_id="SM-APP-IDLE",
            rule_version="2.0.0",
            title=f"{app.app_type} com baixa atividade sem proteção adequada",
            why=(
                f"{app.instance_type} apresentou ~{app.idle_hours_per_day:.1f}h/dia "
                "de baixa atividade e idle shutdown "
                + (
                    "desabilitado."
                    if app.idle_shutdown_min == 0
                    else f"em {app.idle_shutdown_min} minutos."
                )
            ),
        ),
        Recommendation(
            difficulty=1,
            action="Habilitar ou reduzir o idle shutdown",
            how_to_apply=(
                "O time dono deve configurar idle shutdown compatível com a "
                "sessão e validar trabalho não salvo."
            ),
            how_to_validate="Comparar horas de baixa atividade e custo no scan seguinte.",
            risks=["shutdown pode interromper sessão com trabalho não salvo"],
            docs=[_DOC_IDLE],
            blocked=blocked,
        ),
        Evidence(
            items=[
                f"baixa atividade ~{app.idle_hours_per_day:.1f}h/dia",
                f"idle_shutdown={app.idle_shutdown_min}",
                f"instância={app.instance_type}" + (" GPU" if gpu else ""),
                f"cobertura={app.coverage_days} dias",
            ],
            sources=["SageMaker List/DescribeApp", "CloudWatch", "Cost Explorer"],
            observed_runs=max(1, app.active_days_per_month),
            coverage_days=app.coverage_days,
            has_optional_metrics=not blocked,
            owner_tag=app.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def _idle_endpoint(
    account: Account,
    endpoint: SageMakerEndpoint,
    config: Config,
    scan_id: str,
    *,
    rule_id: str,
    title: str,
    action: str,
    method: str,
    docs: list[str],
) -> Opportunity:
    estimation = sm_est.endpoint_idle_saving(endpoint, config, method)
    blocked = estimation.saving_quality == "unavailable"
    return build(
        Finding(
            asset_type="sagemaker_endpoint",
            asset_name=endpoint.name,
            rule_id=rule_id,
            rule_version="1.0.0",
            title=title,
            why=(
                f"modalidade={endpoint.mode}, invocações=0, capacidade atual "
                f"{endpoint.instance_count}× {endpoint.instance_type}."
            ),
        ),
        Recommendation(
            difficulty=2,
            action=action,
            how_to_apply=(
                "Confirmar consumidores, SLA, payload, GPU, VPC e tolerância a "
                "cold start; depois selecionar um cenário elegível."
            ),
            how_to_validate=(
                "Executar teste de carga e comparar erros, p95 de latência e custo."
            ),
            risks=[
                "consumidor crítico pode ser esporádico",
                "scale-to-zero e Serverless podem introduzir cold start",
            ],
            docs=docs,
            risk=0.7,
            blocked=blocked,
        ),
        Evidence(
            items=[
                "0 invocações medidas",
                f"modalidade={endpoint.mode}",
                f"variantes={len(endpoint.variants) or 1}",
                f"cobertura={endpoint.coverage_days} dias",
            ],
            sources=[
                "SageMaker DescribeEndpoint/EndpointConfig",
                "CloudWatch",
                "Application Auto Scaling",
                "Cost Explorer",
            ],
            observed_runs=max(1, endpoint.coverage_days // 7),
            coverage_days=endpoint.coverage_days,
            has_optional_metrics=not blocked,
            owner_tag=endpoint.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def _failed_job(
    account: Account, job: SageMakerJob, config: Config, scan_id: str
) -> Opportunity:
    estimation = sm_est.failed_job_cost(job, config)
    blocked = estimation.saving_quality == "unavailable"
    labels = {
        "training": "Training",
        "processing": "Processing",
        "transform": "Batch Transform",
    }
    return build(
        Finding(
            asset_type=f"sagemaker_{job.kind}_job",
            asset_name=job.name,
            rule_id=f"SM-{job.kind.upper()}-FAILED-COST",
            rule_version="1.0.0",
            title=f"{labels.get(job.kind, job.kind)} Job falhou depois de consumir recurso",
            why=(
                f"status=Failed, duração={job.duration_seconds:.0f}s, "
                f"{job.instance_count}× {job.instance_type}; "
                f"categoria={job.failure_category or 'não classificada'}."
            ),
        ),
        Recommendation(
            difficulty=2,
            action="Corrigir a causa antes da próxima execução",
            how_to_apply=(
                "Revisar a categoria da falha e os logs do job; validar entrada, "
                "permissões, limite e capacidade antes de reexecutar."
            ),
            how_to_validate="Confirmar execução seguinte concluída sem repetir o gasto.",
            risks=["a causa pode ser externa ou transitória"],
            docs=[_DOC_METRICS],
            blocked=blocked,
        ),
        Evidence(
            items=[
                "status=Failed",
                f"duração faturável/modelada={job.billable_seconds or job.duration_seconds:.0f}s",
                f"{job.instance_count}× {job.instance_type}",
                f"categoria={job.failure_category or 'não classificada'}",
                (
                    "falha isolada: custo não anualizado"
                    if job.consistent_scans < 2
                    else "falha observada em scans recorrentes"
                ),
            ],
            sources=["SageMaker DescribeJob", "CloudWatch", "Cost Explorer"],
            observed_runs=1,
            coverage_days=job.coverage_days,
            has_optional_metrics=not blocked,
            owner_tag=job.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def _unused_warm_pool(
    account: Account, job: SageMakerJob, config: Config, scan_id: str
) -> Opportunity:
    estimation = sm_est.warm_pool_waste(job, config)
    blocked = estimation.saving_quality == "unavailable"
    return build(
        Finding(
            asset_type="sagemaker_training_job",
            asset_name=job.name,
            rule_id="SM-WARM-POOL-UNUSED",
            rule_version="1.0.0",
            title="Warm pool faturado sem reutilização",
            why=(
                f"{job.warm_pool_billable_seconds:.0f}s retidos após o training "
                "job sem ReusedByJob."
            ),
        ),
        Recommendation(
            difficulty=1,
            action="Avaliar reduzir ou remover KeepAlivePeriodInSeconds",
            how_to_apply=(
                "O time dono deve comparar o intervalo até o próximo job compatível "
                "com o período de retenção."
            ),
            how_to_validate="Confirmar redução do tempo retido e do custo faturável.",
            risks=["reduzir retenção aumenta tempo de inicialização do próximo job"],
            docs=[_DOC_WARM_POOL],
            blocked=blocked,
        ),
        Evidence(
            items=[
                f"warm_pool_status={job.warm_pool_status}",
                f"retido={job.warm_pool_billable_seconds:.0f}s",
                "ReusedByJob ausente",
            ],
            sources=["SageMaker DescribeTrainingJob"],
            observed_runs=1,
            coverage_days=job.coverage_days,
            has_optional_metrics=not blocked,
            owner_tag=job.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def _feature_store_idle(group: SageMakerFeatureGroup, config: Config) -> bool:
    if (
        group.throughput_mode.lower() != "provisioned"
        or not _financial_ready(group, config)
        or group.max_consumed_read_capacity is None
        or group.max_consumed_write_capacity is None
        or group.throttled_requests is None
        or group.throttled_requests > 0
        or not group.allocated_cost
    ):
        return False
    read_target = max(1, math.ceil(group.max_consumed_read_capacity))
    write_target = max(1, math.ceil(2 * group.max_consumed_write_capacity))
    return (
        read_target + write_target
        < group.provisioned_read_capacity + group.provisioned_write_capacity
    )


def _feature_store(
    account: Account,
    group: SageMakerFeatureGroup,
    config: Config,
    scan_id: str,
) -> Opportunity:
    estimation = sm_est.feature_store_capacity(group, config)
    return build(
        Finding(
            asset_type="sagemaker_feature_group",
            asset_name=group.name,
            rule_id="SM-FEATURE-STORE-PROVISIONED-IDLE",
            rule_version="1.0.0",
            title="Feature Store provisionado acima do consumo medido",
            why=(
                f"capacidade={group.provisioned_read_capacity} RCU/"
                f"{group.provisioned_write_capacity} WCU; picos="
                f"{group.max_consumed_read_capacity:.1f}/"
                f"{group.max_consumed_write_capacity:.1f}, sem throttling."
            ),
        ),
        Recommendation(
            difficulty=2,
            action="Avaliar redução conservadora do throughput provisionado",
            how_to_apply=(
                "Usar o pico de leitura e 2× o pico de escrita como piso; o time "
                "dono aplica a alteração após validar backfills."
            ),
            how_to_validate="Monitorar throttling, latência e custo após a alteração.",
            risks=["picos futuros e backfills podem exigir capacidade adicional"],
            docs=[_DOC_FEATURE_THROUGHPUT],
        ),
        Evidence(
            items=[
                f"provisionado={group.provisioned_read_capacity}/"
                f"{group.provisioned_write_capacity}",
                f"pico consumido={group.max_consumed_read_capacity:.1f}/"
                f"{group.max_consumed_write_capacity:.1f}",
                "throttling=0",
            ],
            sources=["SageMaker DescribeFeatureGroup", "CloudWatch", "Cost Explorer"],
            observed_runs=max(1, group.coverage_days),
            coverage_days=group.coverage_days,
            has_optional_metrics=True,
            owner_tag=group.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def _recommender_opportunities(
    account: Account, config: Config, scan_id: str
) -> list[Opportunity]:
    endpoints = {endpoint.name: endpoint for endpoint in account.sagemaker_endpoints}
    out: list[Opportunity] = []
    for recommendation in account.sagemaker_inference_recommendations:
        endpoint = endpoints.get(recommendation.endpoint_name)
        if (
            endpoint is None
            or recommendation.status.lower() != "completed"
            or recommendation.cost_per_hour is None
            or recommendation.model_latency_ms is None
            or endpoint.model_latency_p95_us is None
            or recommendation.model_latency_ms
            > endpoint.model_latency_p95_us / 1000.0
            or not _financial_ready(endpoint, config)
        ):
            continue
        estimation = sm_est.recommender_saving(
            endpoint,
            recommendation.cost_per_hour,
            recommendation.initial_instance_count,
            config,
        )
        if estimation.saving_quality == "unavailable":
            continue
        out.append(
            _recommender_opportunity(
                account, endpoint, recommendation, estimation, config, scan_id
            )
        )
    return out


def _recommender_opportunity(
    account: Account,
    endpoint: SageMakerEndpoint,
    recommendation: SageMakerInferenceRecommendation,
    estimation,
    config: Config,
    scan_id: str,
) -> Opportunity:
    return build(
        Finding(
            asset_type="sagemaker_endpoint",
            asset_name=endpoint.name,
            rule_id="SM-ENDPOINT-RECOMMENDER-SAVING",
            rule_version="1.0.0",
            title="Inference Recommender encontrou configuração mais barata",
            why=(
                f"{recommendation.initial_instance_count}× "
                f"{recommendation.recommended_instance_type}, "
                f"latência {recommendation.model_latency_ms:.1f}ms."
            ),
        ),
        Recommendation(
            difficulty=3,
            action="Validar em teste de carga a configuração recomendada pela AWS",
            how_to_apply="Criar plano de mudança pelo time dono e manter rollback.",
            how_to_validate="Comparar throughput, p95/p99, erros e custo.",
            risks=["benchmark pode não representar o tráfego de produção"],
            docs=[_DOC_RECOMMENDER],
        ),
        Evidence(
            items=[
                f"job={recommendation.job_name}",
                f"alvo={recommendation.initial_instance_count}× "
                f"{recommendation.recommended_instance_type}",
                f"latência alvo={recommendation.model_latency_ms:.1f}ms",
            ],
            sources=["SageMaker Inference Recommender", "Cost Explorer"],
            observed_runs=1,
            coverage_days=endpoint.coverage_days,
            has_optional_metrics=True,
            owner_tag=endpoint.owner_tag,
        ),
        estimation,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def signals(account: Account, config: Config) -> list[Signal]:
    out: list[Signal] = []
    for app in account.sagemaker_apps:
        if _idle_app_candidate(app, config):
            if not _financial_ready(app, config):
                out.append(
                    _signal(
                        "SM-APP-IDLE-CANDIDATE",
                        "sagemaker_app",
                        app.name,
                        f"~{app.idle_hours_per_day:.1f}h/dia de baixa atividade "
                        f"em {app.coverage_days} dias.",
                        "O padrão permanece após 90 dias ou três scans mensais?",
                        ["histórico de 90 dias ou três scans consistentes"],
                        [_DOC_IDLE],
                        sm_est.idle_app_saving(app, config),
                    )
                )
            out.append(_app_fit_signal(app))
        if _legacy_gpu(app.instance_type):
            out.append(_legacy_signal("sagemaker_app", app.name, app.instance_type))

    for space in account.sagemaker_spaces:
        if (
            space.ebs_volume_size_gb > 0
            and space.active_app_count == 0
            and not (
                _financial_ready(space, config)
                and (space.allocated_storage_cost or 0) > 0
            )
        ):
            out.append(
                _signal(
                    "SM-SPACE-STORAGE-IDLE",
                    "sagemaker_space",
                    f"{space.domain_id}/{space.name}",
                    (
                        f"Space sem app ativo mantém "
                        f"{space.ebs_volume_size_gb} GiB de EBS."
                    ),
                    (
                        "Os arquivos ainda são necessários, possuem backup e o "
                        "owner aprova remover o Space?"
                    ),
                    ["necessidade dos dados, backup e aprovação do owner"],
                    [_DOC_SPACE_STORAGE],
                    sm_est.space_storage_saving(space, config),
                )
            )

    for domain in account.sagemaker_domains:
        financial_domain = bool(
            domain.efs_total_io_bytes == 0
            and domain.efs_client_connections == 0
            and _financial_ready(domain, config)
            and (domain.allocated_storage_cost or 0) > 0
        )
        storage_bytes = domain.efs_storage_bytes or 0
        if (
            storage_bytes > 0
            and domain.active_app_count == 0
            and domain.efs_total_io_bytes == 0
            and not financial_domain
        ):
            out.append(
                _signal(
                    "SM-DOMAIN-EFS-IDLE",
                    "sagemaker_domain",
                    domain.domain_id,
                    (
                        f"Domain sem apps ativos mantém "
                        f"{storage_bytes / 1024**3:.2f} GiB em EFS "
                        "e I/O medido igual a zero; custo, conexões ou histórico "
                        "ainda não fecham uma oportunidade."
                    ),
                    (
                        "O EFS contém dados necessários ou pode ser tratado pelo "
                        "plano de retenção do domínio?"
                    ),
                    [
                        "owner, retenção, backup e consumidores externos do EFS",
                        "ClientConnections=0 e custo EFS atribuível",
                        "90 dias ou três scans consistentes",
                    ],
                    [_DOC_SPACE_STORAGE, _DOC_EFS_METRICS],
                    sm_est.domain_storage_saving(domain, config),
                )
            )

    for endpoint in account.sagemaker_endpoints:
        invocations = (
            endpoint.invocations_per_month
            if endpoint.invocations_per_month is not None
            else endpoint.invocations
        )
        if invocations is not None and invocations <= config.thresholds.sm_endpoint_unused_invocations:
            out.append(_endpoint_mode_signal(endpoint, invocations))
        if _zero_traffic(endpoint) and not _financial_ready(endpoint, config):
            out.append(
                _signal(
                    "SM-ENDPOINT-ZERO-TRAFFIC-CANDIDATE",
                    "sagemaker_endpoint",
                    endpoint.name,
                    f"0 invocações em {endpoint.coverage_days} dias.",
                    "O tráfego continua zero após 90 dias ou três scans?",
                    ["histórico de 90 dias ou três scans consistentes"],
                    [_DOC_METRICS],
                    sm_est.endpoint_idle_saving(
                        endpoint, config, "sm_endpoint_zero_traffic_v1"
                    ),
                )
            )
        if _endpoint_low_utilization(endpoint, config):
            out.append(
                _signal(
                    "SM-ENDPOINT-RIGHTSIZE",
                    "sagemaker_endpoint",
                    endpoint.name,
                    "CPU/GPU/memória p95 baixos nas variantes medidas.",
                    "Qual configuração atende throughput, memória e p99 de latência?",
                    ["SLA de p95/p99", "teste de carga ou Inference Recommender"],
                    [_DOC_RECOMMENDER],
                )
            )
        for instance_type in _endpoint_instance_types(endpoint):
            if _legacy_gpu(instance_type):
                out.append(
                    _legacy_signal(
                        "sagemaker_endpoint", endpoint.name, instance_type
                    )
                )
        if any(
            value and value > 0
            for value in (
                endpoint.model_errors,
                endpoint.invocation_4xx,
                endpoint.invocation_5xx,
                endpoint.backlog_without_capacity,
            )
        ):
            out.append(
                _signal(
                    "SM-ENDPOINT-HEALTH",
                    "sagemaker_endpoint",
                    endpoint.name,
                    "Há erros ou backlog registrados no CloudWatch.",
                    "O comportamento é esperado ou indica saturação/falha do modelo?",
                    ["SLA e causa nos logs da aplicação"],
                    [_DOC_METRICS],
                )
            )

    for notebook in account.sagemaker_notebooks:
        if notebook.status == "InService":
            out.append(
                _signal(
                    "SM-NOTEBOOK-RUNNING",
                    "sagemaker_notebook",
                    notebook.name,
                    f"Notebook {notebook.instance_type} está InService.",
                    "Ele está sendo usado ou permanece ligado sem sessão ativa?",
                    ["métrica ou evidência de atividade do notebook"],
                    [_DOC_METRICS],
                )
            )
        if _legacy_gpu(notebook.instance_type):
            out.append(
                _legacy_signal(
                    "sagemaker_notebook", notebook.name, notebook.instance_type
                )
            )

    for job in account.sagemaker_jobs:
        if _legacy_gpu(job.instance_type):
            out.append(
                _legacy_signal(
                    f"sagemaker_{job.kind}_job", job.name, job.instance_type
                )
            )
        if job.kind == "training" and not job.use_spot and job.instance_hours > 0:
            out.append(
                _signal(
                    "SM-TRAINING-SPOT-CANDIDATE",
                    "sagemaker_training_job",
                    job.name,
                    f"{job.instance_hours:.1f} instância-hora on-demand.",
                    "O SLA tolera espera/interrupção e existe checkpoint seguro?",
                    ["tolerância a interrupção", "checkpoint e prazo máximo"],
                    [_DOC_SPOT],
                )
            )
        if job.detailed_metrics and _job_low_utilization(job, config):
            recurring = (
                job.workload_runs >= 3
                and job.low_utilization_runs >= 3
            )
            out.append(
                _signal(
                    (
                        "SM-JOB-RIGHTSIZE"
                        if recurring
                        else "SM-JOB-RIGHTSIZE-CANDIDATE"
                    ),
                    f"sagemaker_{job.kind}_job",
                    job.name,
                    (
                        "Baixa utilização repetida em "
                        f"{job.low_utilization_runs}/{job.workload_runs} jobs "
                        f"do workload em {job.history_coverage_days} dias."
                        if recurring
                        else (
                            "Utilização p95 baixa nesta execução, mas ainda sem "
                            "três jobs comparáveis do mesmo workload."
                        )
                    ),
                    "Qual instância menor preserva memória, I/O e duração?",
                    [
                        "benchmark com instância candidata",
                        "SLA de duração",
                        *(
                            []
                            if recurring
                            else ["três execuções comparáveis do mesmo workload"]
                        ),
                    ],
                    [_DOC_METRICS],
                )
            )

    for pipeline in account.sagemaker_pipelines:
        if pipeline.failed or pipeline.stopped:
            out.append(
                _signal(
                    "SM-PIPELINE-RETRY-PATTERN",
                    "sagemaker_pipeline",
                    pipeline.name,
                    f"{pipeline.failed} falhas e {pipeline.stopped} paradas na janela.",
                    "Há retries ou reprocessamento que repetem jobs já faturados?",
                    ["grafo de passos, retries e causa de cada falha"],
                    [_DOC_METRICS],
                )
            )

    for schedule in account.sagemaker_monitoring_schedules:
        if (
            schedule.status.lower() == "failed"
            or schedule.last_execution_status.lower() in {"failed", "stopped"}
        ):
            out.append(
                _signal(
                    "SM-MODEL-MONITOR-HEALTH",
                    "sagemaker_monitoring_schedule",
                    schedule.name,
                    (
                        f"schedule={schedule.status}, última execução="
                        f"{schedule.last_execution_status}; "
                        f"{schedule.failure_reason or 'sem causa coletada'}."
                    ),
                    "A falha impede detectar drift ou qualidade degradada do modelo?",
                    ["logs e causa da execução do Model Monitor"],
                    [_DOC_MODEL_MONITOR],
                )
            )

    for group in account.sagemaker_feature_groups:
        if (
            group.online_store
            and group.consumed_read_request_units == 0
            and (group.consumed_write_request_units or 0) > 0
        ):
            out.append(
                _signal(
                    "SM-FEATURE-STORE-ONLINE-UNUSED",
                    "sagemaker_feature_group",
                    group.name,
                    "Há escrita no Online Store e nenhuma leitura medida.",
                    "Existe consumidor de baixa frequência ou o Online Store deixou de ser necessário?",
                    ["consumidores e SLA de leitura online"],
                    [_DOC_FEATURE_THROUGHPUT],
                )
            )
        if group.online_store and group.ttl_seconds is None:
            out.append(
                _signal(
                    "SM-FEATURE-STORE-TTL-GAP",
                    "sagemaker_feature_group",
                    group.name,
                    "Online Store não declara TTL.",
                    "A retenção indefinida é requisito ou pode haver expiração?",
                    ["retenção funcional e regulatória"],
                    [_DOC_FEATURE_TTL],
                )
            )

    savings = account.sagemaker_savings_plans
    if savings and savings.quality != "unavailable":
        out.append(
            _signal(
                "SM-SAVINGS-PLAN-FINOPS",
                "sagemaker_service",
                account.account_id,
                f"cobertura={savings.coverage_percent}, "
                f"utilização={savings.utilization_percent}, "
                f"economia recomendada={savings.estimated_monthly_saving}.",
                "FinOps confirma compromisso, prazo, pagamento e risco de lock-in?",
                ["aprovação FinOps e previsão de demanda"],
                [
                    "https://docs.aws.amazon.com/savingsplans/latest/"
                    "userguide/plan-types.html"
                ],
            )
        )
    return out


def _app_fit_signal(app: SageMakerApp) -> Signal:
    return _signal(
        "SM-APP-INSTANCE-FIT",
        "sagemaker_app",
        app.name,
        f"{app.app_type} em {app.instance_type} com "
        f"~{app.idle_hours_per_day:.1f}h/dia de baixa atividade.",
        "O trabalho exige essa instância e Studio ativo, ou cabe em instância menor/job sob demanda?",
        ["notebooks em uso", "picos de memória/GPU durante atividade"],
        [_DOC_IDLE],
    )


def _endpoint_mode_signal(
    endpoint: SageMakerEndpoint, invocations: int
) -> Signal:
    return _signal(
        "SM-ENDPOINT-MODE-FIT",
        "sagemaker_endpoint",
        endpoint.name,
        f"modalidade={endpoint.mode}, {invocations} invocações/mês.",
        "O SLA/payload favorece manter real-time, Serverless, Async, Batch ou scale-to-zero?",
        [
            "SLA e tolerância a cold start",
            "tamanho do payload e duração",
            "restrições de GPU, VPC, data capture e multi-model",
            "consumidores atuais",
        ],
        [_DOC_SERVERLESS, _DOC_ASYNC, _DOC_SCALE_ZERO],
    )


def _legacy_signal(asset_type: str, name: str, instance_type: str) -> Signal:
    return _signal(
        "SM-LEGACY-GPU-FAMILY",
        asset_type,
        name,
        f"{instance_type} pertence à família G3 de geração anterior.",
        "Framework, CUDA, memória GPU e SLA permitem avaliar G4dn/G5/G6/G7e?",
        [
            "compatibilidade de framework/CUDA",
            "memória GPU e benchmark",
            "disponibilidade e preço na região",
        ],
        [_DOC_G3, _DOC_ENDPOINT_TYPES],
    )


def _legacy_gpu(instance_type: str) -> bool:
    return ".g3." in str(instance_type or "").lower()


def _endpoint_instance_types(endpoint: SageMakerEndpoint) -> set[str]:
    values = {endpoint.instance_type}
    values.update(variant.instance_type for variant in endpoint.variants)
    values.update(
        component.instance_type for component in endpoint.inference_components
    )
    return {value for value in values if value}


def _endpoint_low_utilization(endpoint: SageMakerEndpoint, config: Config) -> bool:
    values = [
        metric
        for variant in endpoint.variants
        for metric in (variant.cpu_p95, variant.gpu_p95, variant.memory_p95)
        if metric is not None
    ]
    return bool(values) and max(values) / 100.0 < config.thresholds.sm_low_utilization


def _job_low_utilization(job: SageMakerJob, config: Config) -> bool:
    values = [
        value
        for value in (job.cpu_p95, job.gpu_p95, job.memory_p95)
        if value is not None
    ]
    return bool(values) and max(values) / 100.0 < config.thresholds.sm_low_utilization


def _signal(
    rule_id: str,
    asset_type: str,
    asset_name: str,
    observation: str,
    question: str,
    missing: list[str],
    docs: list[str],
    estimation=None,
) -> Signal:
    """`estimation` entra só quando o motor já sabe fazer a conta do ativo.

    Nem todo sinal daqui é uma incógnita financeira. Vários existem porque falta
    confiança de que a condição **persiste** — `_financial_ready` recusa por
    cobertura curta ou por scan único, não porque o dinheiro seja desconhecido.
    Nesses, a faixa vem do próprio estimador determinístico e o que ela abre
    para baixo é a chance de o padrão não se repetir.

    Onde não há estimador — versão de runtime, resize de cluster —, o argumento
    fica de fora, e o sinal segue sem faixa. Arbitrar uma fração ali seria
    inventar o número que este produto passou a rodada inteira removendo.
    """
    return Signal(
        kind="config",
        rule_id=rule_id,
        asset_type=asset_type,
        asset_name=asset_name,
        observation=observation,
        question=question,
        missing_evidence=missing,
        doc_links=docs,
        potential_range=potential_from_estimate(
            estimation,
            basis="cálculo determinístico do ativo sobre custo rateado",
            caveat=(
                "condicionada à persistência do padrão: o valor é o que o motor "
                "calcularia hoje, não o que a próxima janela vai confirmar"
            ),
        ),
    )
