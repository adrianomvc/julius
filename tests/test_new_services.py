"""Redshift, SageMaker e a primeira regra que atravessa serviços."""

from __future__ import annotations

from datetime import datetime, timezone

from julius.collection.collectors import redshift, sagemaker
from julius.collection.models import (
    Account,
    AthenaQuery,
    GlueJob,
    RedshiftCluster,
    Table,
)
from julius.collection.window import AnalysisWindow
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules import REGISTRY
from julius.knowledge.rules.cross_service import pipelines
from julius.knowledge.rules.redshift import rules as redshift_rules

WINDOW = AnalysisWindow.trailing(now=datetime(2026, 7, 26, tzinfo=timezone.utc))


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **_):
        return self.pages


class _Redshift:
    def get_paginator(self, name):
        assert name == "describe_clusters"
        return _Paginator(
            [
                {
                    "Clusters": [
                        {
                            "ClusterIdentifier": "analitico",
                            "NodeType": "ra3.xlplus",
                            "NumberOfNodes": 4,
                            "ClusterStatus": "available",
                            "Encrypted": True,
                            "Tags": [{"Key": "Owner", "Value": "dados"}],
                        },
                        {
                            "ClusterIdentifier": "pausado",
                            "NodeType": "ra3.xlplus",
                            "NumberOfNodes": 2,
                            "ClusterStatus": "paused",
                        },
                    ]
                }
            ]
        )


class _CloudWatch:
    """CPU no chão e nenhuma conexão — o perfil de um cluster esquecido."""

    def __init__(self, *, cpu=2.0, peak=6.0, connections=0.0):
        self.cpu, self.peak, self.connections = cpu, peak, connections

    def get_metric_statistics(self, **kwargs):
        metric, statistic = kwargs["MetricName"], kwargs["Statistics"][0]
        if metric == "DatabaseConnections":
            return {"Datapoints": [{"Average": self.connections}] * 30}
        value = self.peak if statistic == "Maximum" else self.cpu
        return {"Datapoints": [{statistic: value}] * 30}


def test_redshift_collects_control_plane_and_metrics():
    clusters = redshift.collect_clusters(
        _Redshift(), _CloudWatch(), window=WINDOW
    )

    by_name = {cluster.name: cluster for cluster in clusters}
    assert by_name["analitico"].node_count == 4
    assert by_name["analitico"].owner_tag == "dados"
    assert by_name["analitico"].avg_cpu_load == 0.02
    assert by_name["analitico"].observed_days == 30
    assert by_name["pausado"].paused is True


def test_redshift_query_history_is_absent_not_zero():
    """O que não é medido não vira zero — seria número sem procedência."""
    clusters = redshift.collect_clusters(_Redshift(), None, window=WINDOW)

    assert all(cluster.queries_in_window is None for cluster in clusters)
    # Sem CloudWatch, nem a CPU é inventada.
    assert all(cluster.avg_cpu_load is None for cluster in clusters)


def test_redshift_rules_report_the_missing_evidence_and_stay_blocked():
    account = Account(
        account_id="123456789012",
        redshift_clusters=[
            RedshiftCluster(
                name="analitico",
                node_type="ra3.xlplus",
                node_count=4,
                avg_cpu_load=0.02,
                max_cpu_load=0.06,
                avg_connections=0.0,
                observed_days=30,
                coverage_days=30,
            )
        ],
    )

    found = redshift_rules.detect(account, DEFAULT_CONFIG, "scan")

    rules = {item.rule_id for item in found}
    assert rules == {"REDSHIFT-IDLE-CLUSTER", "REDSHIFT-OVERSIZED"}
    for item in found:
        # Sem histórico de query, o achado aparece mas não reserva economia.
        assert item.blocked is True
        assert item.estimation.saving_quality == "unavailable"
        assert any("SVV_*/STL_*" in line for line in item.evidence)


def test_a_paused_cluster_is_not_flagged_as_idle():
    account = Account(
        account_id="123456789012",
        redshift_clusters=[
            RedshiftCluster(
                name="pausado",
                paused=True,
                avg_cpu_load=0.0,
                avg_connections=0.0,
                observed_days=30,
            )
        ],
    )

    assert redshift_rules.detect(account, DEFAULT_CONFIG, "scan") == []


class _SageMaker:
    def get_paginator(self, name):
        pages = {
            "list_apps": [
                {
                    "Apps": [
                        {
                            "AppName": "estudo",
                            "AppType": "JupyterLab",
                            "Status": "InService",
                            "ResourceSpec": {"InstanceType": "ml.g5.xlarge"},
                        },
                        {"AppName": "antigo", "Status": "Deleted"},
                    ]
                }
            ],
            "list_endpoints": [{"Endpoints": [{"EndpointName": "inferencia"}]}],
        }[name]
        return _Paginator(pages)

    def describe_endpoint(self, EndpointName):
        return {
            "ProductionVariants": [
                {"InstanceType": "ml.m5.xlarge", "CurrentInstanceCount": 2}
            ]
        }


class _SageMakerMetrics:
    def get_metric_statistics(self, **kwargs):
        if kwargs["MetricName"] == "Invocations":
            return {"Datapoints": [{"Sum": 4.0}] * 3}
        # CPU quase zerada: o kernel ficou ocioso.
        return {"Datapoints": [{"Average": 1.0}] * 20}


def test_sagemaker_collects_apps_and_endpoints():
    apps = sagemaker.collect_apps(_SageMaker(), _SageMakerMetrics(), window=WINDOW)
    endpoints = sagemaker.collect_endpoints(
        _SageMaker(), _SageMakerMetrics(), window=WINDOW
    )

    assert [app.name for app in apps] == ["estudo"], "app deletado não é inventário"
    assert apps[0].instance_type == "ml.g5.xlarge"
    assert apps[0].idle_hours_per_day == 24.0
    assert endpoints[0].instance_count == 2
    assert endpoints[0].invocations_per_month == 12


def test_sagemaker_without_metrics_does_not_claim_idleness():
    apps = sagemaker.collect_apps(_SageMaker(), None, window=WINDOW)

    assert apps[0].idle_hours_per_day == 0.0


def test_the_cross_service_rule_needs_both_sides_to_fire():
    """Nenhum detector de serviço único enxerga este caso."""
    job = GlueJob(
        name="agrega_vendas",
        dpu_seconds_window=3600.0 * 200,
        writes_tables=["db.vendas"],
        observed_runs=30,
        coverage_days=30,
        window_days=30,
    )
    query = AthenaQuery(
        query_id="padrao-1",
        reads_tables=["db.vendas"],
        full_scan_confirmed=True,
        observed_runs=40,
        coverage_days=30,
    )
    account = Account(
        account_id="123456789012",
        glue_jobs=[job],
        athena_queries=[query],
        tables=[Table(name="db.vendas")],
    )

    found = pipelines.detect(account, DEFAULT_CONFIG, "scan")

    assert [item.rule_id for item in found] == ["XSVC-WASTED-PRODUCTION"]
    item = found[0]
    assert item.asset_name == "db.vendas"
    assert item.source_process == "agrega_vendas"
    # O baseline é o custo de produzir; quanto se recupera depende de qual lado
    # muda, e isso exige benchmark.
    assert item.estimation.baseline_cost > 0
    assert item.estimation.estimated_saving == 0.0
    assert item.blocked is True

    # Sem o lado da leitura problemático, não há achado.
    query.full_scan_confirmed = False
    assert pipelines.detect(account, DEFAULT_CONFIG, "scan") == []

    # Sem linhagem conhecida, também não: seria acusar sem saber quem produz.
    query.full_scan_confirmed = True
    account.glue_jobs = [GlueJob(name="outro")]
    account.tables = [Table(name="db.vendas")]
    assert pipelines.detect(account, DEFAULT_CONFIG, "scan") == []


def test_the_new_families_are_registered_and_declare_what_they_need():
    families = {(item.service, item.name) for item in REGISTRY}

    assert ("redshift", "clusters") in families
    assert ("cross_service", "wasted_production") in families
    cross = next(
        item for item in REGISTRY if item.name == "wasted_production"
    )
    assert set(cross.requires) == {"glue_jobs", "athena_queries"}
