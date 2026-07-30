"""Redshift, SageMaker e a primeira regra que atravessa serviços."""

from __future__ import annotations

from datetime import datetime, timezone

from verified_pricing import verified_config

from julius.collection.collectors import redshift, sagemaker
from julius.collection.models import (
    Account,
    AthenaQuery,
    GlueJob,
    RedshiftCluster,
    SageMakerApp,
    StateMachine,
    Table,
)
from julius.collection.window import AnalysisWindow
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules import REGISTRY
from julius.knowledge.rules.cross_service import pipelines
from julius.knowledge.rules.redshift import rules as redshift_rules
from julius.knowledge.rules.sagemaker import rules as sagemaker_rules
from julius.knowledge.rules.stepfunctions import rules as stepfunctions_rules

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


class _SageMakerWithConfig(_SageMaker):
    """A mesma conta, agora respondendo o que a configuração declara."""

    def __init__(self, idle_timeout: int | None = 60):
        self.idle_timeout = idle_timeout

    def describe_app(self, **_):
        if self.idle_timeout is None:
            return {}
        return {
            "ResourceSpec": {
                "AppLifecycleManagement": {
                    "IdleSettings": {"IdleTimeoutInMinutes": self.idle_timeout}
                }
            }
        }


class _AutoScaling:
    def describe_scalable_targets(self, **kwargs):
        return {
            "ScalableTargets": [
                {"ResourceId": kwargs["ResourceIds"][0], "MinCapacity": 2}
            ]
        }


def test_every_field_a_rule_gates_on_can_be_filled_by_the_collector():
    """O teste que faltava, e que teria pego quatro regras mortas.

    O dataset de exemplo traz esses campos preenchidos à mão, então a suíte
    passava enquanto nenhuma conta real produzia achado. Aqui a exigência é a
    outra: partindo de respostas AWS simuladas, o coletor precisa conseguir
    preencher todo campo que uma regra usa como porta.
    """
    apps = sagemaker.collect_apps(
        _SageMakerWithConfig(), _SageMakerMetrics(), window=WINDOW
    )
    endpoints = sagemaker.collect_endpoints(
        _SageMakerWithConfig(), _SageMakerMetrics(), _AutoScaling(), window=WINDOW
    )

    # Gate de SM-APP-IDLE.
    assert apps[0].idle_shutdown_min == 60
    # Multiplicador da economia, antes fixo em 22.
    assert apps[0].active_days_per_month > 0
    # Evidência de SM-ENDPOINT-UNUSED.
    assert endpoints[0].auto_scaling is True
    assert endpoints[0].min_capacity == 2


def test_a_configured_idle_shutdown_is_not_an_idle_finding():
    """Antes, `0` significava tanto "desligado" quanto "não coletado"."""
    account = Account(
        account_id="123456789012",
        sagemaker_apps=[
            SageMakerApp(
                name="estudo",
                status="InService",
                idle_hours_per_day=14.0,
                idle_shutdown_min=60,
                coverage_days=90,
            )
        ],
    )

    rules = {o.rule_id for o in sagemaker_rules.detect(account, DEFAULT_CONFIG, "scan")}
    assert "SM-APP-IDLE" not in rules

    account.sagemaker_apps[0].idle_shutdown_min = 0
    rules = {o.rule_id for o in sagemaker_rules.detect(account, DEFAULT_CONFIG, "scan")}
    assert "SM-APP-IDLE" in rules


def test_uncollected_idle_shutdown_is_not_a_finding_either():
    account = Account(
        account_id="123456789012",
        sagemaker_apps=[
            SageMakerApp(
                name="estudo",
                status="InService",
                idle_hours_per_day=14.0,
                idle_shutdown_min=None,
                coverage_days=30,
            )
        ],
    )

    assert sagemaker_rules.detect(account, DEFAULT_CONFIG, "scan") == []


def _state_machine(**overrides) -> StateMachine:
    defaults = dict(
        name="orquestra",
        type="STANDARD",
        executions_per_month=45000,
        avg_duration_sec=90.0,
        avg_state_transitions=12,
        observed_runs=45000,
        coverage_days=30,
        sampled_executions=20,
    )
    defaults.update(overrides)
    return StateMachine(**defaults)


def test_express_candidate_no_longer_dies_on_an_uncollected_field():
    """Sem benchmark/idempotência o candidato fica como sinal sem economia."""
    account = Account(
        account_id="123456789012", state_machines=[_state_machine(idempotent=None)]
    )

    found = stepfunctions_rules.detect(account, DEFAULT_CONFIG, "scan")
    signals = stepfunctions_rules.signals(account, DEFAULT_CONFIG)
    assert not any(o.rule_id == "SFN-STANDARD-TO-EXPRESS" for o in found)
    express = next(s for s in signals if s.rule_id == "SFN-STANDARD-TO-EXPRESS")
    assert any("idempotência" in item for item in express.missing_evidence)
    assert any("benchmark" in item for item in express.missing_evidence)

    # Idempotência sozinha ainda não inventa duração/memória do Express.
    account.state_machines[0].idempotent = True
    found = stepfunctions_rules.detect(account, DEFAULT_CONFIG, "scan")
    assert not any(o.rule_id == "SFN-STANDARD-TO-EXPRESS" for o in found)


def test_idempotency_becomes_a_question_for_the_contextual_analysis():
    account = Account(
        account_id="123456789012", state_machines=[_state_machine(idempotent=None)]
    )

    signals = stepfunctions_rules.signals(account, DEFAULT_CONFIG)
    idempotency = next(s for s in signals if s.rule_id == "SFN-STANDARD-TO-EXPRESS")

    assert idempotency.kind == "config"
    assert idempotency.asset_type == "state_machine"
    assert "at-least-once" in idempotency.question or "idempot" in idempotency.question
    assert idempotency.missing_evidence


def test_a_polling_loop_without_counted_waits_claims_no_saving():
    config = verified_config("stepfunctions")
    """A ASL prova a estrutura; só o histórico prova o custo."""
    account = Account(
        account_id="123456789012",
        state_machines=[
            _state_machine(
                has_polling_loop=True,
                poll_extra_transitions=None,
                avg_state_transitions=None,
            )
        ],
    )

    found = stepfunctions_rules.detect(account, config, "scan")
    polling = next(o for o in found if o.rule_id == "SFN-POLLING-LOOP")

    assert polling.blocked is True
    assert polling.estimated_gain.monthly_expected == 0
    assert polling.missing_evidence

    account.state_machines[0].poll_extra_transitions = 40
    account.state_machines[0].avg_state_transitions = 52
    found = stepfunctions_rules.detect(account, config, "scan")
    polling = next(o for o in found if o.rule_id == "SFN-POLLING-LOOP")

    assert polling.blocked is False
    assert polling.estimated_gain.monthly_expected > 0


def test_high_retry_asks_instead_of_asserting():
    account = Account(
        account_id="123456789012",
        state_machines=[_state_machine(max_retry_attempts=5)],
    )

    signals = stepfunctions_rules.signals(account, DEFAULT_CONFIG)
    retry = next(s for s in signals if s.rule_id == "SFN-RETRY-MASKING")

    assert "?" in retry.question
    assert retry.missing_evidence


def _cluster(**overrides) -> RedshiftCluster:
    defaults = dict(
        name="dw-legado",
        kind="provisioned",
        node_type="ra3.xlplus",
        node_count=4,
        avg_cpu_load=0.02,
        max_cpu_load=0.06,
        avg_connections=0.0,
        observed_days=30,
        coverage_days=30,
    )
    defaults.update(overrides)
    return RedshiftCluster(**defaults)


def test_an_idle_cluster_with_allocated_billing_finally_gets_a_number():
    """O achado mais óbvio de um data lake era reportado com economia zero."""
    account = Account(
        account_id="123456789012",
        redshift_clusters=[_cluster(allocated_compute_cost=1412.60)],
    )

    found = redshift_rules.detect(account, DEFAULT_CONFIG, "scan")
    idle = next(o for o in found if o.rule_id == "REDSHIFT-IDLE-CLUSTER")

    assert idle.blocked is False
    assert idle.estimated_gain.monthly_expected > 0
    assert idle.estimation is not None
    # Pausar não é uma fração: o compute inteiro deixa de ser cobrado.
    assert idle.estimation.baseline_cost == 1412.60
    assert idle.estimation.projected_cost == 0.0
    assert idle.estimation.baseline_quality == "allocated"
    assert idle.evidence_quality == "allocated"


def test_without_allocated_billing_the_idle_cluster_stays_as_it_was():
    """Sem cobrança rateada não há o que afirmar — comportamento preservado."""
    account = Account(account_id="123456789012", redshift_clusters=[_cluster()])

    found = redshift_rules.detect(account, DEFAULT_CONFIG, "scan")
    idle = next(o for o in found if o.rule_id == "REDSHIFT-IDLE-CLUSTER")

    assert idle.blocked is True
    assert idle.estimated_gain.monthly_expected == 0
    assert idle.estimation is not None
    assert idle.estimation.saving_quality == "unavailable"


def test_oversized_stays_blocked_even_with_allocated_billing():
    """Saber o que se paga não diz quantos nós cabem."""
    account = Account(
        account_id="123456789012",
        redshift_clusters=[_cluster(allocated_compute_cost=1412.60)],
    )

    found = redshift_rules.detect(account, DEFAULT_CONFIG, "scan")
    oversized = next(o for o in found if o.rule_id == "REDSHIFT-OVERSIZED")

    assert oversized.blocked is True
    assert oversized.estimated_gain.monthly_expected == 0


def test_redshift_asks_what_the_metric_cannot_answer():
    account = Account(
        account_id="123456789012",
        redshift_clusters=[_cluster(allocated_compute_cost=1412.60)],
    )

    signals = {s.rule_id: s for s in redshift_rules.signals(account, DEFAULT_CONFIG)}

    assert "REDSHIFT-IDLE-JUSTIFICATION" in signals
    assert "REDSHIFT-RESIZE-TARGET" in signals
    for signal in signals.values():
        assert signal.asset_type == "redshift_cluster"
        assert signal.question and signal.missing_evidence


def test_a_paused_cluster_produces_neither_finding_nor_question():
    account = Account(
        account_id="123456789012",
        redshift_clusters=[_cluster(paused=True, allocated_compute_cost=1412.60)],
    )

    assert redshift_rules.detect(account, DEFAULT_CONFIG, "scan") == []
    assert redshift_rules.signals(account, DEFAULT_CONFIG) == []
