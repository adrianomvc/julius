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

from julius.collection.collectors.paginate import safe_pages
from julius.collection.models import SageMakerApp, SageMakerEndpoint
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
            app_type=str(raw.get("AppType") or "JupyterLab"),
            instance_type=str(resource.get("InstanceType") or "ml.t3.medium"),
            status=str(raw.get("Status") or "InService"),
            coverage_days=window.days,
            owner_tag=_tag(raw, "Owner"),
        )
        app.idle_hours_per_day, active_days = _idle_profile(
            cloudwatch_client, name, window
        )
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
        variant = _production_variant(sagemaker_client, name)
        endpoint = SageMakerEndpoint(
            name=name,
            instance_type=str(variant.get("InstanceType") or "ml.m5.large"),
            instance_count=int(variant.get("CurrentInstanceCount") or 1),
            coverage_days=window.days,
            owner_tag=_tag(raw, "Owner"),
        )
        endpoint.invocations_per_month = _invocations(
            cloudwatch_client, name, window
        )
        scaling = _scalable_target(
            autoscaling_client, name, str(variant.get("VariantName") or "AllTraffic")
        )
        if scaling is not None:
            endpoint.auto_scaling = True
            endpoint.min_capacity = scaling
        endpoints.append(endpoint)
    return endpoints


def _scalable_target(autoscaling_client, endpoint: str, variant: str) -> int | None:
    """Capacidade mínima registrada no Application Auto Scaling, se houver.

    Ausência de alvo registrado é o caso comum e significa endpoint sem
    autoscaling — aqui o silêncio é resposta, não evidência faltando.
    """
    if autoscaling_client is None:
        return None
    resource_id = f"endpoint/{endpoint}/variant/{variant}"
    try:
        response = autoscaling_client.describe_scalable_targets(
            ServiceNamespace="sagemaker",
            ResourceIds=[resource_id],
        )
    except Exception:
        return None
    for target in response.get("ScalableTargets", []) or []:
        if str(target.get("ResourceId")) == resource_id:
            return int(target.get("MinCapacity") or 1)
    return None


def _production_variant(sagemaker_client, endpoint_name: str) -> dict:
    try:
        described = sagemaker_client.describe_endpoint(EndpointName=endpoint_name)
    except Exception:
        return {}
    variants = described.get("ProductionVariants") or []
    return variants[0] if variants else {}


def _idle_profile(cloudwatch_client, app_name: str, window) -> tuple[float, int]:
    """Horas ociosas/dia e dias com atividade observada, do mesmo conjunto.

    O período do datapoint é diário, então cada ponto é um dia em que o app
    existiu. Contar os pontos dá os dias ativos medidos, que antes eram um 22
    fixo multiplicando a economia — número plausível e inventado.

    Sem CloudWatch a ociosidade fica em zero e a regra de idle shutdown não
    dispara: não medimos, então não afirmamos.
    """
    if cloudwatch_client is None:
        return 0.0, 0
    points = _metric_points(
        cloudwatch_client,
        namespace="/aws/sagemaker/Studio",
        metric="CPUUtilization",
        dimension=("AppName", app_name),
        window=window,
        statistic="Average",
    )
    if not points:
        return 0.0, 0
    idle_fraction = sum(1 for value in points if value / 100.0 < _IDLE_CPU_THRESHOLD)
    observed_days = len(points)
    # Normaliza a janela para o mês, para casar com o custo mensal do modelo.
    active_days = round(observed_days * 30.0 / max(1, window.days))
    return round(24.0 * idle_fraction / observed_days, 2), active_days


def _invocations(cloudwatch_client, endpoint_name: str, window) -> int | None:
    """`None` sem CloudWatch: não consultar não é o mesmo que não haver."""
    if cloudwatch_client is None:
        return None
    points = _metric_points(
        cloudwatch_client,
        namespace="AWS/SageMaker",
        metric="Invocations",
        dimension=("EndpointName", endpoint_name),
        window=window,
        statistic="Sum",
    )
    return int(sum(points))


def _metric_points(
    cloudwatch_client,
    *,
    namespace: str,
    metric: str,
    dimension: tuple[str, str],
    window: AnalysisWindow,
    statistic: str,
) -> list[float]:
    try:
        response = cloudwatch_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=[{"Name": dimension[0], "Value": dimension[1]}],
            StartTime=window.start,
            EndTime=window.end,
            Period=86400,
            Statistics=[statistic],
        )
    except Exception:
        return []
    return [
        float(point[statistic])
        for point in response.get("Datapoints", [])
        if statistic in point
    ]


def _paginate(
    client, operation: str, key: str, gaps: list[str] | None = None
) -> list[dict]:
    resultado = safe_pages(client, operation, key)
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
