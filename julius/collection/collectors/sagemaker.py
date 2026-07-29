"""Coleta read-only de apps do Studio e endpoints de inferência.

As regras e o modelo financeiro de SageMaker já existiam desde o MVP; o que
faltava era o coletor. Até aqui os dados só chegavam pelo dataset exportado, o
que significa que nenhuma conta coletada ao vivo produzia achado de SageMaker —
e o silêncio parecia "não há nada a otimizar".

Ociosidade e invocação vêm do CloudWatch. Sem métrica, o campo fica no default
e a regra correspondente não dispara: evidência ausente nunca vira zero.

A configuração declarada segue a mesma disciplina, e ela faltava. O idle
shutdown do app e o autoscaling do endpoint são propriedades que a AWS publica,
mas nenhum coletor as lia — e como `idle_shutdown_min` usava `0` tanto para
"desligado" quanto para "não coletado", a regra de ociosidade tratava um app
bem configurado como se não tivesse proteção nenhuma. Agora não coletado é
`None`, e a regra sabe a diferença.
"""

from __future__ import annotations

from datetime import datetime
from statistics import quantiles

from julius.collection.collectors.paginate import safe_call, safe_pages
from julius.collection.models import (
    SageMakerApp,
    SageMakerEndpoint,
    SageMakerInferenceComponent,
    SageMakerVariant,
)
from julius.collection.window import AnalysisWindow

# Um app sem sessão de kernel ativa ainda cobra a instância. `AppInstanceCount`
# não existe como métrica; a ociosidade é derivada do uso de CPU do kernel.
_IDLE_CPU_THRESHOLD = 0.05


def collect_apps(
    sagemaker_client,
    cloudwatch_client=None,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerApp]:
    apps: list[SageMakerApp] = []
    for raw in _paginate(sagemaker_client, "list_apps", "Apps", gaps):
        name = str(raw.get("AppName") or "")
        if not name or str(raw.get("Status") or "") == "Deleted":
            continue
        resource = raw.get("ResourceSpec") or {}
        app = SageMakerApp(
            name=name,
            arn=str(raw.get("AppArn") or ""),
            app_type=str(raw.get("AppType") or "JupyterLab"),
            instance_type=str(resource.get("InstanceType") or "ml.t3.medium"),
            status=str(raw.get("Status") or "InService"),
            coverage_days=window.days,
            domain_id=str(raw.get("DomainId") or ""),
            space_name=str(raw.get("SpaceName") or ""),
            user_profile_name=str(raw.get("UserProfileName") or ""),
            owner_tag=_owner_tag(sagemaker_client, raw),
        )
        idle_hours, active_days, metrics_available, cpu_p95 = _idle_profile(
            cloudwatch_client, name, window
        )
        app.idle_hours_per_day = idle_hours
        app.activity_metrics_available = metrics_available
        app.cpu_p95 = cpu_p95
        if active_days:
            app.active_days_per_month = active_days
        app.idle_shutdown_min = _idle_shutdown_min(sagemaker_client, raw)
        apps.append(app)
    return apps


def _idle_shutdown_min(sagemaker_client, raw: dict) -> int | None:
    """Minutos de idle shutdown declarados, ou `None` se não der para saber.

    O ajuste vive em `AppLifecycleManagement.IdleSettings`, que pode estar no
    próprio app, no space ou no default do domain — nessa ordem de precedência,
    porque o mais específico é o que vale.
    """
    app_name = str(raw.get("AppName") or "")
    domain_id = str(raw.get("DomainId") or "")
    space_name = str(raw.get("SpaceName") or "")
    user_profile = str(raw.get("UserProfileName") or "")

    described = _describe(
        sagemaker_client,
        "describe_app",
        DomainId=domain_id,
        AppType=str(raw.get("AppType") or ""),
        AppName=app_name,
        **({"SpaceName": space_name} if space_name else {}),
        **({"UserProfileName": user_profile} if user_profile and not space_name else {}),
    )
    timeout = _idle_timeout(described.get("ResourceSpec"))
    if timeout is None and space_name and domain_id:
        space = _describe(
            sagemaker_client,
            "describe_space",
            DomainId=domain_id,
            SpaceName=space_name,
        )
        timeout = _idle_timeout(space.get("SpaceSettings"))
    if timeout is None and domain_id:
        domain = _describe(sagemaker_client, "describe_domain", DomainId=domain_id)
        timeout = _idle_timeout(domain.get("DefaultSpaceSettings")) or _idle_timeout(
            domain.get("DefaultUserSettings")
        )
    return timeout


def _idle_timeout(settings: object) -> int | None:
    """Procura `IdleTimeoutInMinutes` sob qualquer bloco de app settings."""
    if not isinstance(settings, dict):
        return None
    lifecycle = settings.get("AppLifecycleManagement")
    if isinstance(lifecycle, dict):
        idle = lifecycle.get("IdleSettings")
        if isinstance(idle, dict):
            value = idle.get("IdleTimeoutInMinutes")
            if isinstance(value, int):
                return value
            # Desligado explicitamente é zero, e é diferente de não declarado.
            if str(idle.get("LifecycleManagement") or "").upper() == "DISABLED":
                return 0
    for nested in settings.values():
        found = _idle_timeout(nested)
        if found is not None:
            return found
    return None


def _describe(client, operation: str, **kwargs) -> dict:
    try:
        result = getattr(client, operation)(**kwargs)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def collect_endpoints(
    sagemaker_client,
    cloudwatch_client=None,
    autoscaling_client=None,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[SageMakerEndpoint]:
    endpoints: list[SageMakerEndpoint] = []
    for raw in _paginate(sagemaker_client, "list_endpoints", "Endpoints", gaps):
        name = str(raw.get("EndpointName") or "")
        if not name:
            continue
        described, config = _endpoint_description(sagemaker_client, name)
        variants = _variants(
            sagemaker_client,
            cloudwatch_client,
            autoscaling_client,
            name,
            described,
            config,
            window,
        )
        components = _inference_components(
            sagemaker_client,
            autoscaling_client,
            name,
            gaps,
        )
        variant = variants[0] if variants else SageMakerVariant(name="AllTraffic")
        mode = _endpoint_mode(config, components)
        invocations, last_invocation, invocations_available = _invocations(
            cloudwatch_client, name, window
        )
        normalized_invocations = (
            round(invocations * 30.0 / max(1, window.days))
            if invocations_available
            else None
        )
        endpoint = SageMakerEndpoint(
            name=name,
            arn=str(raw.get("EndpointArn") or described.get("EndpointArn") or ""),
            status=str(raw.get("EndpointStatus") or described.get("EndpointStatus") or ""),
            endpoint_config_name=str(
                described.get("EndpointConfigName")
                or raw.get("EndpointConfigName")
                or ""
            ),
            mode=mode,
            variants=variants,
            inference_components=components,
            instance_type=variant.instance_type or "ml.m5.large",
            instance_count=variant.current_instance_count or 1,
            coverage_days=window.days,
            owner_tag=_owner_tag(sagemaker_client, raw, described),
            invocations=invocations if invocations_available else None,
            invocations_per_month=normalized_invocations,
            last_invocation_at=last_invocation,
            auto_scaling=any(v.auto_scaling for v in variants)
            or any(c.auto_scaling for c in components),
            min_capacity=variant.min_capacity
            if variant.auto_scaling
            else max(1, variant.current_instance_count),
            serverless_memory_mb=variant.serverless_memory_mb,
            provisioned_concurrency=variant.provisioned_concurrency,
        )
        endpoint.model_errors = _metric_total(
            cloudwatch_client, "AWS/SageMaker", "ModelError", name, window
        )
        endpoint.invocation_4xx = _metric_total(
            cloudwatch_client, "AWS/SageMaker", "Invocation4XXErrors", name, window
        )
        endpoint.invocation_5xx = _metric_total(
            cloudwatch_client, "AWS/SageMaker", "Invocation5XXErrors", name, window
        )
        endpoint.model_latency_p95_us = _metric_p95(
            cloudwatch_client, "AWS/SageMaker", "ModelLatency", name, window
        )
        if mode == "async":
            endpoint.backlog_without_capacity = _metric_total(
                cloudwatch_client,
                "AWS/SageMaker",
                "HasBacklogWithoutCapacity",
                name,
                window,
            )
        endpoints.append(endpoint)
    return endpoints


def _scalable_target(
    autoscaling_client, resource_id: str
) -> tuple[int, int] | None:
    """Capacidade mínima registrada no Application Auto Scaling, se houver.

    Ausência de alvo registrado é o caso comum e significa endpoint sem
    autoscaling — aqui o silêncio é resposta, não evidência faltando.
    """
    if autoscaling_client is None:
        return None
    try:
        response = autoscaling_client.describe_scalable_targets(
            ServiceNamespace="sagemaker",
            ResourceIds=[resource_id],
        )
    except Exception:
        return None
    for target in response.get("ScalableTargets", []) or []:
        if str(target.get("ResourceId")) == resource_id:
            return (
                int(target.get("MinCapacity") or 0),
                int(target.get("MaxCapacity") or 0),
            )
    return None


def _endpoint_description(sagemaker_client, endpoint_name: str) -> tuple[dict, dict]:
    described, _ = safe_call(
        sagemaker_client, "describe_endpoint", EndpointName=endpoint_name
    )
    config_name = str(described.get("EndpointConfigName") or "")
    if not config_name:
        return described, {}
    config, _ = safe_call(
        sagemaker_client,
        "describe_endpoint_config",
        EndpointConfigName=config_name,
    )
    return described, config


def _variants(
    sagemaker_client,
    cloudwatch_client,
    autoscaling_client,
    endpoint_name: str,
    described: dict,
    config: dict,
    window: AnalysisWindow,
) -> list[SageMakerVariant]:
    deployed = {
        str(item.get("VariantName") or "AllTraffic"): item
        for item in described.get("ProductionVariants", []) or []
    }
    declared = {
        str(item.get("VariantName") or "AllTraffic"): item
        for item in config.get("ProductionVariants", []) or []
    }
    names = list(dict.fromkeys([*deployed, *declared]))
    variants: list[SageMakerVariant] = []
    for name in names:
        current = deployed.get(name, {})
        wanted = declared.get(name, {})
        serverless = wanted.get("ServerlessConfig") or {}
        resource_id = f"endpoint/{endpoint_name}/variant/{name}"
        scaling = _scalable_target(autoscaling_client, resource_id)
        policy_count = _scaling_policy_count(autoscaling_client, resource_id)
        variant = SageMakerVariant(
            name=name,
            instance_type=str(
                current.get("InstanceType") or wanted.get("InstanceType") or ""
            ),
            current_instance_count=int(current.get("CurrentInstanceCount") or 0),
            desired_instance_count=int(current.get("DesiredInstanceCount") or 0),
            initial_instance_count=int(wanted.get("InitialInstanceCount") or 0),
            min_capacity=scaling[0] if scaling else 0,
            max_capacity=scaling[1] if scaling else 0,
            auto_scaling=scaling is not None,
            scaling_policy_count=policy_count,
            serverless_memory_mb=int(serverless.get("MemorySizeInMB") or 0),
            provisioned_concurrency=int(
                serverless.get("ProvisionedConcurrency") or 0
            ),
        )
        variant.invocations = _variant_metric_total(
            cloudwatch_client,
            "AWS/SageMaker",
            "Invocations",
            endpoint_name,
            name,
            window,
        )
        variant.cpu_p95 = _variant_metric_p95(
            cloudwatch_client,
            "/aws/sagemaker/Endpoints",
            "CPUUtilization",
            endpoint_name,
            name,
            window,
        )
        variant.gpu_p95 = _variant_metric_p95(
            cloudwatch_client,
            "/aws/sagemaker/Endpoints",
            "GPUUtilization",
            endpoint_name,
            name,
            window,
        )
        variant.memory_p95 = _variant_metric_p95(
            cloudwatch_client,
            "/aws/sagemaker/Endpoints",
            "MemoryUtilization",
            endpoint_name,
            name,
            window,
        )
        variants.append(variant)
    return variants


def _inference_components(
    sagemaker_client,
    autoscaling_client,
    endpoint_name: str,
    gaps: list[str] | None,
) -> list[SageMakerInferenceComponent]:
    raw_components = _paginate(
        sagemaker_client,
        "list_inference_components",
        "InferenceComponents",
        gaps,
        EndpointNameEquals=endpoint_name,
    )
    out: list[SageMakerInferenceComponent] = []
    for summary in raw_components:
        name = str(summary.get("InferenceComponentName") or "")
        if not name:
            continue
        described, _ = safe_call(
            sagemaker_client,
            "describe_inference_component",
            InferenceComponentName=name,
        )
        runtime = described.get("RuntimeConfig") or {}
        specification = described.get("Specification") or {}
        resource_id = f"inference-component/{name}"
        scaling = _scalable_target(autoscaling_client, resource_id)
        out.append(
            SageMakerInferenceComponent(
                name=name,
                variant_name=str(described.get("VariantName") or ""),
                status=str(
                    described.get("InferenceComponentStatus")
                    or summary.get("InferenceComponentStatus")
                    or ""
                ),
                instance_type=str(specification.get("InstanceType") or ""),
                current_copies=int(runtime.get("CurrentCopyCount") or 0),
                desired_copies=int(runtime.get("DesiredCopyCount") or 0),
                min_copies=scaling[0] if scaling else 0,
                max_copies=scaling[1] if scaling else 0,
                auto_scaling=scaling is not None,
            )
        )
    return out


def _endpoint_mode(
    config: dict, components: list[SageMakerInferenceComponent]
) -> str:
    if components:
        return "inference_components"
    variants = config.get("ProductionVariants", []) or []
    if any(item.get("ServerlessConfig") for item in variants):
        return "serverless"
    if config.get("AsyncInferenceConfig"):
        return "async"
    return "real_time"


def _scaling_policy_count(autoscaling_client, resource_id: str) -> int:
    if autoscaling_client is None:
        return 0
    response, _ = safe_call(
        autoscaling_client,
        "describe_scaling_policies",
        ServiceNamespace="sagemaker",
        ResourceId=resource_id,
    )
    return len(response.get("ScalingPolicies", []) or [])


def _idle_profile(
    cloudwatch_client, app_name: str, window
) -> tuple[float, int, bool, float | None]:
    """Horas ociosas/dia e dias com atividade observada, do mesmo conjunto.

    O período do datapoint é diário, então cada ponto é um dia em que o app
    existiu. Contar os pontos dá os dias ativos medidos, que antes eram um 22
    fixo multiplicando a economia — número plausível e inventado.

    Sem CloudWatch a ociosidade fica em zero e a regra de idle shutdown não
    dispara: não medimos, então não afirmamos.
    """
    if cloudwatch_client is None:
        return 0.0, 0, False, None
    available, series = _metric_series(
        cloudwatch_client,
        namespace="/aws/sagemaker/Studio",
        metric="CPUUtilization",
        dimension=("AppName", app_name),
        window=window,
        statistic="Average",
    )
    points = [value for _, value in series]
    if not available:
        return 0.0, 0, False, None
    if not points:
        return 0.0, 0, True, None
    idle_fraction = sum(1 for value in points if value / 100.0 < _IDLE_CPU_THRESHOLD)
    observed_days = len(points)
    # Normaliza a janela para o mês, para casar com o custo mensal do modelo.
    active_days = round(observed_days * 30.0 / max(1, window.days))
    return (
        round(24.0 * idle_fraction / observed_days, 2),
        active_days,
        True,
        _p95(points),
    )


def _invocations(
    cloudwatch_client, endpoint_name: str, window
) -> tuple[int, str, bool]:
    """Total, última invocação e disponibilidade da consulta."""
    if cloudwatch_client is None:
        return 0, "", False
    available, series = _metric_series(
        cloudwatch_client,
        namespace="AWS/SageMaker",
        metric="Invocations",
        dimension=("EndpointName", endpoint_name),
        window=window,
        statistic="Sum",
    )
    last = max(
        (
            stamp
            for stamp, value in series
            if value > 0 and isinstance(stamp, datetime)
        ),
        default=None,
    )
    return (
        int(sum(value for _, value in series)),
        last.isoformat() if isinstance(last, datetime) else "",
        available,
    )


def _metric_points(
    cloudwatch_client,
    *,
    namespace: str,
    metric: str,
    dimension: tuple[str, str],
    window: AnalysisWindow,
    statistic: str,
) -> list[float]:
    _, series = _metric_series(
        cloudwatch_client,
        namespace=namespace,
        metric=metric,
        dimension=dimension,
        window=window,
        statistic=statistic,
    )
    return [value for _, value in series]


def _metric_series(
    cloudwatch_client,
    *,
    namespace: str,
    metric: str,
    dimension: tuple[str, str],
    window: AnalysisWindow,
    statistic: str,
    extra_dimensions: list[dict] | None = None,
) -> tuple[bool, list[tuple[object, float]]]:
    try:
        response = cloudwatch_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=[
                {"Name": dimension[0], "Value": dimension[1]},
                *(extra_dimensions or []),
            ],
            StartTime=window.start,
            EndTime=window.end,
            Period=86400,
            Statistics=[statistic],
        )
    except Exception:
        return False, []
    return True, [
        (point.get("Timestamp"), float(point[statistic]))
        for point in response.get("Datapoints", [])
        if statistic in point
    ]


def _metric_total(
    cloudwatch_client,
    namespace: str,
    metric: str,
    endpoint_name: str,
    window: AnalysisWindow,
) -> int | None:
    if cloudwatch_client is None:
        return None
    available, series = _metric_series(
        cloudwatch_client,
        namespace=namespace,
        metric=metric,
        dimension=("EndpointName", endpoint_name),
        window=window,
        statistic="Sum",
    )
    return int(sum(value for _, value in series)) if available else None


def _metric_p95(
    cloudwatch_client,
    namespace: str,
    metric: str,
    endpoint_name: str,
    window: AnalysisWindow,
) -> float | None:
    if cloudwatch_client is None:
        return None
    available, series = _metric_series(
        cloudwatch_client,
        namespace=namespace,
        metric=metric,
        dimension=("EndpointName", endpoint_name),
        window=window,
        statistic="Average",
    )
    return _p95([value for _, value in series]) if available else None


def _variant_metric_total(
    cloudwatch_client,
    namespace: str,
    metric: str,
    endpoint_name: str,
    variant_name: str,
    window: AnalysisWindow,
) -> int | None:
    if cloudwatch_client is None:
        return None
    available, series = _metric_series(
        cloudwatch_client,
        namespace=namespace,
        metric=metric,
        dimension=("EndpointName", endpoint_name),
        extra_dimensions=[{"Name": "VariantName", "Value": variant_name}],
        window=window,
        statistic="Sum",
    )
    return int(sum(value for _, value in series)) if available else None


def _variant_metric_p95(
    cloudwatch_client,
    namespace: str,
    metric: str,
    endpoint_name: str,
    variant_name: str,
    window: AnalysisWindow,
) -> float | None:
    if cloudwatch_client is None:
        return None
    available, series = _metric_series(
        cloudwatch_client,
        namespace=namespace,
        metric=metric,
        dimension=("EndpointName", endpoint_name),
        extra_dimensions=[{"Name": "VariantName", "Value": variant_name}],
        window=window,
        statistic="Average",
    )
    return _p95([value for _, value in series]) if available else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 4)
    return round(quantiles(values, n=100, method="inclusive")[94], 4)


def _paginate(
    client,
    operation: str,
    key: str,
    gaps: list[str] | None = None,
    **kwargs,
) -> list[dict]:
    resultado = safe_pages(client, operation, key, **kwargs)
    if gaps is not None and not resultado.complete:
        gaps.append(f"{operation}: {resultado.error_category or 'incompleto'}")
    return resultado.items


def _tag(raw: dict, key: str) -> str | None:
    tags = raw.get("Tags") or {}
    if isinstance(tags, dict):
        return tags.get(key)
    for item in tags:
        if isinstance(item, dict) and item.get("Key") == key:
            return item.get("Value")
    return None


def _owner_tag(client, *resources: dict) -> str | None:
    for resource in resources:
        direct = _tag(resource, "Owner")
        if direct:
            return direct
    arn = next(
        (
            str(resource.get(key) or "")
            for resource in resources
            for key in ("AppArn", "EndpointArn", "ResourceArn")
            if resource.get(key)
        ),
        "",
    )
    if not arn:
        return None
    response, _ = safe_call(client, "list_tags", ResourceArn=arn)
    return _tag(response, "Owner")
