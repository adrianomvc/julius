"""Regras de Redshift sobre o que o plano de controle e o CloudWatch mostram.

O que dá para afirmar com essa evidência é capacidade e ociosidade. Regra de
query — distribuição ruim, tabela nunca lida, fila saturada — exigiria as
tabelas de sistema, que a coleta atual não alcança. Elas não estão aqui, e o
relatório diz isso em vez de deixar o silêncio parecer ausência de problema.
"""

from __future__ import annotations

from julius.collection.models import Account, RedshiftCluster
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation

_DOC_PAUSE = "https://docs.aws.amazon.com/redshift/latest/mgmt/managing-cluster-operations.html"
_DOC_RESIZE = "https://docs.aws.amazon.com/redshift/latest/mgmt/managing-cluster-operations.html#elastic-resize"
_DOC_SERVERLESS = "https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-capacity.html"

#: Sem histórico de query, toda economia aqui é investigação, não número.
_UNMEASURED = "histórico de query não coletado: SVV_*/STL_* exigem acesso de banco"


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    for cluster in getattr(account, "redshift_clusters", []):
        if cluster.paused:
            continue
        if _is_idle(cluster, config):
            out.append(_idle(account, cluster, config, scan_id))
        if _is_oversized(cluster, config):
            out.append(_oversized(account, cluster, config, scan_id))
    return out


def _is_idle(cluster: RedshiftCluster, config: Config) -> bool:
    """Sem conexão observada e CPU no chão durante toda a janela."""
    return (
        cluster.observed_days > 0
        and cluster.avg_connections is not None
        and cluster.avg_connections < 1.0
        and cluster.avg_cpu_load is not None
        and cluster.avg_cpu_load < config.thresholds.low_cpu
    )


def _is_oversized(cluster: RedshiftCluster, config: Config) -> bool:
    """CPU baixa no pico, não só na média — média baixa pode ser janela ociosa."""
    return (
        cluster.kind == "provisioned"
        and cluster.node_count > 2
        and cluster.max_cpu_load is not None
        and cluster.max_cpu_load < config.thresholds.worker_type_low_cpu
    )


def _blocked_estimation(method: str) -> Estimation:
    """Achado sem contrafactual: aparece, mas não reserva economia."""
    return Estimation(
        method=method,
        baseline_cost=0.0,
        projected_cost=0.0,
        estimated_saving=0.0,
        assumptions=[
            _UNMEASURED,
            "economia não quantificada até haver medição de uso",
        ],
        saving_quality="unavailable",
    )


def _idle(
    account: Account, cluster: RedshiftCluster, config: Config, scan_id: str
) -> Opportunity:
    return build(
        Finding(
            rule_id="REDSHIFT-IDLE-CLUSTER",
            rule_version="1.0.0",
            asset_type="redshift_cluster",
            asset_name=cluster.name,
            title="Cluster sem conexão observada na janela",
            why=(
                f"Conexões médias {cluster.avg_connections:.2f} e CPU média "
                f"{cluster.avg_cpu_load:.0%} em {cluster.observed_days} dias medidos."
            ),
        ),
        Recommendation(
            difficulty=2,
            action="Avaliar pausa agendada ou desprovisionamento do cluster",
            how_to_apply=(
                "Confirmar com o dono se o cluster ainda é necessário; havendo uso "
                "esporádico, agendar pausa fora da janela de trabalho."
            ),
            how_to_validate=(
                "Acompanhar DatabaseConnections e a cobrança do serviço na janela "
                "seguinte."
            ),
            risks=[
                "cluster pausado não aceita conexão: confirmar janelas de carga",
                "conexões podem existir sem aparecer na métrica agregada por dia",
            ],
            docs=[_DOC_PAUSE, _DOC_SERVERLESS],
            blocked=True,
        ),
        Evidence(
            items=[
                f"conexões médias {cluster.avg_connections:.2f}",
                f"CPU média {cluster.avg_cpu_load:.0%}",
                f"{cluster.observed_days} dias com métrica",
                _UNMEASURED,
            ],
            sources=["Redshift DescribeClusters", "CloudWatch"],
            observed_runs=cluster.observed_days,
            coverage_days=cluster.coverage_days,
            has_optional_metrics=False,
            owner_tag=cluster.owner_tag,
        ),
        _blocked_estimation("redshift_idle_cluster_v1"),
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def _oversized(
    account: Account, cluster: RedshiftCluster, config: Config, scan_id: str
) -> Opportunity:
    return build(
        Finding(
            rule_id="REDSHIFT-OVERSIZED",
            rule_version="1.0.0",
            asset_type="redshift_cluster",
            asset_name=cluster.name,
            title="Cluster com CPU baixa até no pico",
            why=(
                f"{cluster.node_count} nós {cluster.node_type} com CPU máxima "
                f"{cluster.max_cpu_load:.0%} na janela."
            ),
        ),
        Recommendation(
            difficulty=3,
            action="Avaliar elastic resize para menos nós",
            how_to_apply=(
                "Comparar o perfil de carga com a capacidade atual e testar o resize "
                "numa janela controlada."
            ),
            how_to_validate=(
                "Comparar CPU, duração de query e fila antes e depois do resize."
            ),
            risks=[
                "menos nós reduz memória e disco disponíveis por query",
                "CPU baixa pode esconder gargalo de I/O ou de fila",
            ],
            docs=[_DOC_RESIZE],
            blocked=True,
        ),
        Evidence(
            items=[
                f"{cluster.node_count} nós {cluster.node_type}",
                f"CPU máxima {cluster.max_cpu_load:.0%}",
                _UNMEASURED,
            ],
            sources=["Redshift DescribeClusters", "CloudWatch"],
            observed_runs=cluster.observed_days,
            coverage_days=cluster.coverage_days,
            has_optional_metrics=False,
            owner_tag=cluster.owner_tag,
        ),
        _blocked_estimation("redshift_oversized_v1"),
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
