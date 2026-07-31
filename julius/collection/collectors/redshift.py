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

from julius.collection.collectors import metrics
from julius.collection.collectors.metrics import MetricQuery
from julius.collection.collectors.paginate import safe_pages
from julius.collection.models import RedshiftCluster
from julius.collection.ownership_tags import owner_from_tags
from julius.collection.window import AnalysisWindow


def collect_clusters(
    redshift_client,
    cloudwatch_client=None,
    serverless_client=None,
    *,
    window: AnalysisWindow,
    gaps: list[str] | None = None,
) -> list[RedshiftCluster]:
    """Clusters provisionados e workgroups serverless, com o que falhou anotado.

    `gaps` recebe a categoria de cada listagem que não completou. Sem ele, um
    `describe_clusters` negado devolveria a mesma lista vazia que uma conta sem
    Redshift nenhum — e o relatório afirmaria que não há cluster.
    """
    clusters = _provisioned(redshift_client, window, gaps)
    _attach_advisor(redshift_client, clusters, gaps)
    clusters.extend(_serverless(serverless_client, window, gaps))
    _attach_serverless_limits(serverless_client, clusters, gaps)
    _enrich_cloudwatch(cloudwatch_client, clusters, window)
    return clusters


def _provisioned(
    client, window: AnalysisWindow, gaps: list[str] | None = None
) -> list[RedshiftCluster]:
    out: list[RedshiftCluster] = []
    for raw in _paginate(client, "describe_clusters", "Clusters", gaps):
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


def _serverless(
    client, window: AnalysisWindow, gaps: list[str] | None = None
) -> list[RedshiftCluster]:
    if client is None:
        return []
    out: list[RedshiftCluster] = []
    for raw in _paginate(client, "list_workgroups", "workgroups", gaps):
        name = str(raw.get("workgroupName") or "")
        if not name:
            continue
        out.append(
            RedshiftCluster(
                name=name,
                kind="serverless",
                resource_arn=str(raw.get("workgroupArn") or ""),
                status=str(raw.get("status") or "AVAILABLE").lower(),
                base_rpu=int(raw.get("baseCapacity") or 0),
                max_rpu=(
                    int(raw["maxCapacity"])
                    if raw.get("maxCapacity") is not None
                    else None
                ),
                price_performance_target=str(
                    (raw.get("pricePerformanceTarget") or {}).get("level") or ""
                ),
                encrypted=True,  # Serverless é sempre criptografado em repouso.
                created_at=_iso(raw.get("creationDate")),
                coverage_days=window.days,
            )
        )
    return out


def _attach_advisor(client, clusters: list[RedshiftCluster], gaps) -> None:
    """Anexa recomendações oficiais do Advisor sem inferir economia."""
    by_name = {item.name: item for item in clusters if item.kind == "provisioned"}
    for raw in _paginate(client, "list_recommendations", "Recommendations", gaps):
        name = str(raw.get("ClusterIdentifier") or "")
        cluster = by_name.get(name)
        if cluster is None:
            continue
        cluster.advisor_recommendations.append(
            {
                "type": str(raw.get("RecommendationType") or ""),
                "action_type": str(raw.get("RecommendedActionType") or ""),
                "text": str(raw.get("RecommendationText") or ""),
                "action": str(raw.get("RecommendedAction") or ""),
            }
        )


def _attach_serverless_limits(client, clusters: list[RedshiftCluster], gaps) -> None:
    if client is None:
        return
    serverless = [item for item in clusters if item.kind == "serverless"]
    if not serverless:
        return
    limits = _paginate(client, "list_usage_limits", "usageLimits", gaps)
    for cluster in serverless:
        cluster.serverless_usage_limits = [
            str(item.get("featureType") or item.get("limitType") or "usage")
            for item in limits
            if (
                str(item.get("resourceArn") or "") == cluster.resource_arn
                or cluster.name in str(item.get("resourceArn") or "")
            )
        ]


def _enrich_cloudwatch(
    client, clusters: list[RedshiftCluster], window: AnalysisWindow
) -> None:
    """CPU e conexões de todos os clusters, em lote.

    Eram três chamadas por cluster, em série: vinte clusters pagavam sessenta
    latências. As mesmas sessenta consultas cabem numa chamada de `GetMetricData`.
    """
    if client is None or not clusters:
        return
    pedidos = [
        (cluster, campo, MetricQuery(
            namespace=(
                "AWS/Redshift"
                if cluster.kind == "provisioned"
                else "AWS/Redshift-Serverless"
            ),
            metric_name=metrica,
            stat=estatistica,
            dimensions=(
                (
                    "ClusterIdentifier"
                    if cluster.kind == "provisioned"
                    else "Workgroup",
                    cluster.name,
                ),
            ),
        ))
        for cluster in clusters
        for campo, metrica, estatistica in (
            ("cpu", "CPUUtilization", "Average"),
            ("peak", "CPUUtilization", "Maximum"),
            ("connections", "DatabaseConnections", "Average"),
        )
    ]
    metrics.collect(
        client, [query for _c, _f, query in pedidos], start=window.start, end=window.end
    )

    for cluster, campo, query in pedidos:
        pontos = query.values
        if not pontos:
            continue
        if campo == "cpu":
            cluster.avg_cpu_load = round(sum(pontos) / len(pontos) / 100.0, 3)
            cluster.observed_days = len(pontos)
        elif campo == "peak":
            cluster.max_cpu_load = round(max(pontos) / 100.0, 3)
        else:
            cluster.avg_connections = round(sum(pontos) / len(pontos), 2)


def _paginate(
    client, operation: str, key: str, gaps: list[str] | None = None
) -> list[dict]:
    resultado = safe_pages(client, operation, key)
    if gaps is not None and not resultado.complete:
        gaps.append(f"{operation}: {resultado.error_category or 'incompleto'}")
    return resultado.items


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _tag(tags) -> str | None:
    return owner_from_tags(tags)
