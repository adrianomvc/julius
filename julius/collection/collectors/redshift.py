"""Coleta read-only de Redshift: plano de controle e CloudWatch.

**O escopo é deliberadamente limitado, e isso muda o que dá para afirmar.**

Os sinais que tornariam a análise de Redshift comparável à de Glue e Athena —
histórico de query, skew de distribuição, tabelas nunca lidas — vivem em tabelas
de sistema `SVV_*` e `STL_*`. Alcançá-las exige conexão de banco ou a Redshift
Data API: credencial de banco, uma permissão nova e um raio de acesso diferente
do resto da coleta, que hoje só fala com API de controle.

Enquanto essa decisão não for tomada, este coletor traz o que o plano de
controle e o CloudWatch expõem: capacidade, utilização, conexões e estado. Isso
sustenta regras de capacidade e ociosidade — não regras de query.

O que não é medido não vira zero: `queries_in_window` fica `None`, e nenhuma
regra que dependa dele dispara.
"""

from __future__ import annotations

from julius.collection.models import RedshiftCluster
from julius.collection.window import AnalysisWindow


def collect_clusters(
    redshift_client,
    cloudwatch_client=None,
    serverless_client=None,
    *,
    window: AnalysisWindow,
) -> list[RedshiftCluster]:
    clusters = _provisioned(redshift_client, window)
    clusters.extend(_serverless(serverless_client, window))
    for cluster in clusters:
        _enrich_cloudwatch(cloudwatch_client, cluster, window)
    return clusters


def _provisioned(client, window: AnalysisWindow) -> list[RedshiftCluster]:
    out: list[RedshiftCluster] = []
    for raw in _paginate(client, "describe_clusters", "Clusters"):
        identifier = str(raw.get("ClusterIdentifier") or "")
        if not identifier:
            continue
        status = str(raw.get("ClusterStatus") or "available")
        out.append(
            RedshiftCluster(
                name=identifier,
                kind="provisioned",
                node_type=str(raw.get("NodeType") or ""),
                node_count=int(raw.get("NumberOfNodes") or 0),
                status=status,
                paused=status in {"paused", "pausing"},
                encrypted=bool(raw.get("Encrypted")),
                created_at=_iso(raw.get("ClusterCreateTime")),
                coverage_days=window.days,
                owner_tag=_tag(raw.get("Tags")),
            )
        )
    return out


def _serverless(client, window: AnalysisWindow) -> list[RedshiftCluster]:
    if client is None:
        return []
    out: list[RedshiftCluster] = []
    for raw in _paginate(client, "list_workgroups", "workgroups"):
        name = str(raw.get("workgroupName") or "")
        if not name:
            continue
        out.append(
            RedshiftCluster(
                name=name,
                kind="serverless",
                status=str(raw.get("status") or "AVAILABLE").lower(),
                base_rpu=int(raw.get("baseCapacity") or 0),
                encrypted=True,  # Serverless é sempre criptografado em repouso.
                created_at=_iso(raw.get("creationDate")),
                coverage_days=window.days,
            )
        )
    return out


def _enrich_cloudwatch(
    client, cluster: RedshiftCluster, window: AnalysisWindow
) -> None:
    if client is None:
        return
    dimension = (
        ("ClusterIdentifier", cluster.name)
        if cluster.kind == "provisioned"
        else ("Workgroup", cluster.name)
    )
    namespace = "AWS/Redshift" if cluster.kind == "provisioned" else "AWS/Redshift-Serverless"

    cpu = _points(client, namespace, "CPUUtilization", dimension, window, "Average")
    if cpu:
        cluster.avg_cpu_load = round(sum(cpu) / len(cpu) / 100.0, 3)
        cluster.observed_days = len(cpu)
    peak = _points(client, namespace, "CPUUtilization", dimension, window, "Maximum")
    if peak:
        cluster.max_cpu_load = round(max(peak) / 100.0, 3)
    connections = _points(
        client, namespace, "DatabaseConnections", dimension, window, "Average"
    )
    if connections:
        cluster.avg_connections = round(sum(connections) / len(connections), 2)


def _points(
    client,
    namespace: str,
    metric: str,
    dimension: tuple[str, str],
    window: AnalysisWindow,
    statistic: str,
) -> list[float]:
    try:
        response = client.get_metric_statistics(
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
    if client is None:
        return []
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


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _tag(tags) -> str | None:
    for item in tags or []:
        if isinstance(item, dict) and item.get("Key") == "Owner":
            return item.get("Value")
    return None
