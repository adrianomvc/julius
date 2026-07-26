"""Coleta read-only de apps do Studio e endpoints de inferência.

As regras e o modelo financeiro de SageMaker já existiam desde o MVP; o que
faltava era o coletor. Até aqui os dados só chegavam pelo dataset exportado, o
que significa que nenhuma conta coletada ao vivo produzia achado de SageMaker —
e o silêncio parecia "não há nada a otimizar".

Ociosidade e invocação vêm do CloudWatch. Sem métrica, o campo fica no default
e a regra correspondente não dispara: evidência ausente nunca vira zero.
"""

from __future__ import annotations

from julius.collection.models import SageMakerApp, SageMakerEndpoint
from julius.collection.window import AnalysisWindow

# Um app sem sessão de kernel ativa ainda cobra a instância. `AppInstanceCount`
# não existe como métrica; a ociosidade é derivada do uso de CPU do kernel.
_IDLE_CPU_THRESHOLD = 0.05


def collect_apps(
    sagemaker_client, cloudwatch_client=None, *, window: AnalysisWindow
) -> list[SageMakerApp]:
    apps: list[SageMakerApp] = []
    for raw in _paginate(sagemaker_client, "list_apps", "Apps"):
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
        app.idle_hours_per_day = _idle_hours_per_day(
            cloudwatch_client, name, window
        )
        apps.append(app)
    return apps


def collect_endpoints(
    sagemaker_client, cloudwatch_client=None, *, window: AnalysisWindow
) -> list[SageMakerEndpoint]:
    endpoints: list[SageMakerEndpoint] = []
    for raw in _paginate(sagemaker_client, "list_endpoints", "Endpoints"):
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
        endpoints.append(endpoint)
    return endpoints


def _production_variant(sagemaker_client, endpoint_name: str) -> dict:
    try:
        described = sagemaker_client.describe_endpoint(EndpointName=endpoint_name)
    except Exception:
        return {}
    variants = described.get("ProductionVariants") or []
    return variants[0] if variants else {}


def _idle_hours_per_day(cloudwatch_client, app_name: str, window) -> float:
    """Horas/dia em que o kernel ficou abaixo do limiar de CPU.

    Sem CloudWatch a ociosidade fica em zero e a regra de idle shutdown não
    dispara — o que é correto: não medimos, então não afirmamos.
    """
    if cloudwatch_client is None:
        return 0.0
    points = _metric_points(
        cloudwatch_client,
        namespace="/aws/sagemaker/Studio",
        metric="CPUUtilization",
        dimension=("AppName", app_name),
        window=window,
        statistic="Average",
    )
    if not points:
        return 0.0
    idle_fraction = sum(1 for value in points if value / 100.0 < _IDLE_CPU_THRESHOLD)
    return round(24.0 * idle_fraction / len(points), 2)


def _invocations(cloudwatch_client, endpoint_name: str, window) -> int:
    if cloudwatch_client is None:
        return 0
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


def _paginate(client, operation: str, key: str) -> list[dict]:
    try:
        paginator = client.get_paginator(operation)
        pages = paginator.paginate()
    except Exception:
        try:
            pages = [getattr(client, operation)()]
        except Exception:
            return []
    out: list[dict] = []
    for page in pages:
        out.extend(page.get(key, []))
    return out


def _tag(raw: dict, key: str) -> str | None:
    tags = raw.get("Tags") or {}
    if isinstance(tags, dict):
        return tags.get(key)
    for item in tags:
        if isinstance(item, dict) and item.get("Key") == key:
            return item.get("Value")
    return None
