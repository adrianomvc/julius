"""Telemetria offline da saúde da coleta AWS."""

from __future__ import annotations

import json

import pytest

from julius.collection import sources as collect_module
from julius.collection.health.recorder import (
    CollectionRecorder,
    RequiredCollectionError,
)
from julius.collection.models import Account, CollectionHealth, GlueJob, IamGap
from julius.collection.normalizers.dump import account_to_dataset
from julius.collection.normalizers.loader import load_account
from julius.collection.orchestrator import collect_account
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG
from julius.pipeline import analyze
from julius.reporting import renderer
from julius.state import RunStore


class AwsLikeError(Exception):
    response = {
        "Error": {
            "Code": "AccessDeniedException",
            "Message": "token=never-persist-this",
        }
    }


class FakeSts:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


class _PaginadorVazio:
    def paginate(self, **_kwargs):
        return iter([{}])


class FakeClient:
    """Cliente que responde vazio a qualquer operação.

    Antes era um `object()` puro, e toda chamada levantava `AttributeError` —
    que a paginação engolia e transformava em lista vazia. Isso fazia "a AWS
    respondeu que não há nada" e "a chamada nem aconteceu" serem indistinguíveis
    no teste, exatamente a confusão que a coleta passou a recusar.
    """

    def paginate(self, **_kwargs):
        return iter([{}])

    def get_paginator(self, _operation):
        return _PaginadorVazio()

    def __getattr__(self, _name):
        return lambda **_kwargs: {}


class FakeSession:
    region_name = "sa-east-1"

    # `**_kwargs` porque a coleta passa `config=` — retry adaptativo e timeouts
    # não são opcionais no caminho real.
    def client(self, name, **_kwargs):
        return FakeSts() if name == "sts" else FakeClient()


def _patch_empty_collectors(monkeypatch):
    monkeypatch.setattr(
        collect_module.cost_explorer, "collect_services", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.jobs, "collect_jobs", lambda *_a, **_k: []
    )
    # O catálogo agora lista bancos antes de ler tabelas: as duas etapas
    # precisam ser neutralizadas para a fonte ficar vazia sem ficar quebrada.
    monkeypatch.setattr(
        collect_module.jobs, "list_database_names", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.jobs, "collect_tables", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.glue_crawlers, "collect_crawlers", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.triggers, "collect_triggers", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.databrew, "collect_jobs", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.cloudwatch, "enrich_glue_cpu", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        collect_module.cloudwatch,
        "enrich_glue_observability",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        collect_module.sessions, "collect_sessions", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.athena, "collect_analysis", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        collect_module.stepfunctions,
        "collect_state_machines",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        collect_module.schedules, "collect_schedules", lambda *_a, **_k: []
    )


def test_recorder_categorizes_errors_without_persisting_messages():
    recorder = CollectionRecorder()
    result = recorder.capture(
        "CloudWatch",
        lambda: (_ for _ in ()).throw(AwsLikeError("password=secret")),
        [],
        impact="métricas ausentes",
        next_action="validar permissão",
    )
    assert result == []
    assert recorder.entries[0].status == "unavailable"
    assert recorder.entries[0].error_category == "permission_denied"
    serialized = json.dumps(recorder.entries[0].__dict__)
    assert "never-persist-this" not in serialized
    assert "password=secret" not in serialized


def test_required_source_blocks_and_partial_coverage_is_explicit():
    recorder = CollectionRecorder()
    recorder.capture(
        "CloudWatch",
        lambda: ["a"],
        [],
        count=len,
        expected=2,
    )
    assert recorder.entries[0].status == "partial"
    assert recorder.entries[0].coverage == 0.5

    with pytest.raises(RequiredCollectionError, match="Glue Jobs"):
        recorder.capture(
            "Glue Jobs",
            lambda: (_ for _ in ()).throw(AwsLikeError()),
            [],
            required=True,
        )
    assert recorder.entries[-1].status == "error"


def test_collect_account_records_sources_and_optional_disabled_do_not_degrade(
    monkeypatch,
):
    _patch_empty_collectors(monkeypatch)
    account = collect_account(FakeSession(), config=DEFAULT_CONFIG)

    by_source = {item.source: item for item in account.collection_health}
    assert by_source["AWS identity"].status == "ok"
    # Só a identidade é obrigatória. Ver
    # `test_a_denied_glue_does_not_take_the_other_services_down`.
    assert by_source["AWS identity"].required is True
    assert by_source["Glue Jobs"].required is False
    assert by_source["Spark Event Logs"].error_category == "not_configured"
    assert by_source["Table Touches"].affects_status is False
    assert by_source["CloudTrail Ownership"].affects_status is False
    # Sem job coletado não há custo Glue a atribuir; a fonte não degrada o scan.
    assert by_source["Glue Cost Explorer"].status == "ok"
    assert account.collection_status == "ok"


def test_collect_account_closes_all_domain_checkpoints(monkeypatch, tmp_path):
    _patch_empty_collectors(monkeypatch)
    scan_id = "scan-20260803-120000-000001"
    with RunStore(tmp_path / "runs.duckdb") as store:
        account = collect_account(
            FakeSession(),
            config=DEFAULT_CONFIG,
            scan_id=scan_id,
            run_store=store,
            checkpoint_dir=tmp_path / "checkpoints",
        )

        assert account.scan_id == scan_id
        assert store.run_status(account.account_id, scan_id) == "deterministic_ready"
        assert {item.domain for item in store.checkpoints(account.account_id, scan_id)} == {
            "athena",
            "glue",
            "orchestration",
            "redshift",
            "s3",
            "sagemaker",
        }
        assert len(store.tasks()) == 6


def test_parallel_and_serial_collection_are_semantically_equivalent(monkeypatch):
    """O DAG pode mudar o relógio, nunca inventário, cobertura ou ordem."""
    _patch_empty_collectors(monkeypatch)

    serial = collect_account(
        FakeSession(), config=DEFAULT_CONFIG, collection_execution="serial"
    )
    parallel = collect_account(
        FakeSession(), config=DEFAULT_CONFIG, collection_execution="parallel"
    )

    serial_payload = account_to_dataset(serial)
    parallel_payload = account_to_dataset(parallel)
    for payload in (serial_payload, parallel_payload):
        telemetry = payload["run_telemetry"]
        for key in (
            "execution_mode",
            "collection_wall_ms",
            "source_duration_ms",
            "max_parallel_sources",
            "max_pending_sources",
            "critical_path_ms",
            "critical_path_sources",
            "scheduler_wait_ms",
            "service_concurrency_limits",
            "service_limit_reductions",
            "page_concurrency_limit",
            "max_parallel_pages",
            "page_backpressure_wait_ms",
            "peak_memory_bytes",
            "memory_limit_bytes",
            "memory_pressure_events",
        ):
            telemetry.pop(key, None)
        for entry in payload["collection_health"]:
            entry.pop("started_at", None)
            entry.pop("completed_at", None)
            entry.pop("duration_ms", None)

    assert parallel_payload == serial_payload
    assert parallel.run_telemetry.execution_mode == "parallel"


def test_known_athena_workgroups_reach_the_collector(monkeypatch):
    _patch_empty_collectors(monkeypatch)
    observed = {}

    def collect_analysis(*_args, **kwargs):
        observed.update(kwargs)
        return None

    monkeypatch.setattr(collect_module.athena, "collect_analysis", collect_analysis)

    collect_account(
        FakeSession(),
        config=DEFAULT_CONFIG,
        collection_execution="serial",
        athena_history_workgroups=(
            "primary",
            "analytics-workgroup",
            "analytics-workgroup-v3",
        ),
        athena_workgroup_roles={
            "primary": "unused_expected",
            "analytics-workgroup": "legacy",
            "analytics-workgroup-v3": "preferred",
        },
    )

    assert observed["configured_workgroups"] == (
        "primary",
        "analytics-workgroup",
        "analytics-workgroup-v3",
    )
    assert observed["configured_workgroup_roles"]["analytics-workgroup"] == "legacy"


def test_missing_glue_billing_degrades_the_scan_when_there_are_jobs(monkeypatch):
    _patch_empty_collectors(monkeypatch)
    monkeypatch.setattr(
        collect_module.jobs,
        "collect_jobs",
        lambda *_a, **_k: [GlueJob(name="etl", worker_type="G.1X", number_of_workers=2)],
    )
    account = collect_account(FakeSession(), config=DEFAULT_CONFIG)

    billing = next(
        item
        for item in account.collection_health
        if item.source == "Glue Cost Explorer"
    )
    assert billing.status == "unavailable"
    assert billing.error_category == "no_data"
    assert "modelado por tarifa" in billing.impact
    assert account.collection_status == "partial"


def test_optional_failure_marks_scan_partial(monkeypatch):
    _patch_empty_collectors(monkeypatch)
    monkeypatch.setattr(
        collect_module.cost_explorer,
        "collect_services",
        lambda *_a, **_k: (_ for _ in ()).throw(AwsLikeError()),
    )
    account = collect_account(FakeSession(), config=DEFAULT_CONFIG)
    assert account.collection_status == "partial"
    cost = next(
        item for item in account.collection_health if item.source == "Cost Explorer"
    )
    assert cost.error_category == "permission_denied"


def test_a_denied_glue_does_not_take_the_other_services_down(monkeypatch):
    """`glue:GetJobs` negado custava o scan inteiro, não só a análise de Glue.

    O SSO restrito que não enxerga Glue costuma enxergar S3, Athena, Redshift e
    SageMaker — e a conta ficava sem relatório nenhum por causa da permissão de
    um serviço só. Agora a fonte degrada e o scan continua, dizendo o que faltou.
    """
    _patch_empty_collectors(monkeypatch)
    monkeypatch.setattr(
        collect_module.jobs,
        "collect_jobs",
        lambda *_a, **_k: (_ for _ in ()).throw(AwsLikeError()),
    )

    account = collect_account(FakeSession(), config=DEFAULT_CONFIG)

    assert account.collection_status == "partial"
    by_source = {item.source: item for item in account.collection_health}
    assert by_source["Glue Jobs"].status == "unavailable"
    assert by_source["Glue Jobs"].error_category == "permission_denied"
    # As outras fontes continuaram rodando: é o ponto da mudança.
    assert by_source["Amazon Redshift"].status == "ok"
    assert by_source["SageMaker Studio"].status == "ok"
    assert by_source["Step Functions"].status == "ok"


def test_only_a_lost_identity_stops_the_scan(monkeypatch):
    """Sem saber em qual conta se está, nenhum número tem significado."""
    _patch_empty_collectors(monkeypatch)
    monkeypatch.setattr(
        FakeSts, "get_caller_identity", lambda self: (_ for _ in ()).throw(AwsLikeError())
    )
    with pytest.raises(RequiredCollectionError, match="AWS identity"):
        collect_account(FakeSession(), config=DEFAULT_CONFIG)


def test_health_roundtrip_and_report_json(tmp_path):
    account = Account(
        account_id="health-offline",
        collection_health=[
            CollectionHealth(
                source="Glue Jobs",
                status="ok",
                required=True,
                collected=2,
                expected=2,
                coverage=1.0,
                result_origin="cached",
                cache_age_seconds=120,
                iam_gaps=[
                    IamGap(
                        service="s3",
                        operation="get_bucket_logging",
                        iam_action="s3:GetBucketLogging",
                        affected_resources=2,
                        examples=["a", "b"],
                    )
                ],
            ),
            CollectionHealth(
                source="CloudWatch",
                status="partial",
                collected=1,
                expected=2,
                coverage=0.5,
                error_category="partial_data",
                impact="capacidade incompleta",
                next_action="habilitar métricas",
            ),
        ],
    )
    account.run_telemetry.snapshot_hits = 1
    account.run_telemetry.snapshot_misses = 2
    dataset = tmp_path / "health.json"
    dataset.write_text(
        json.dumps(account_to_dataset(account)),
        encoding="utf-8",
    )
    loaded = load_account(dataset)
    assert loaded.collection_status == "partial"
    assert loaded.collection_health[0].result_origin == "cached"
    assert loaded.collection_health[0].cache_age_seconds == 120
    assert loaded.collection_health[0].iam_gaps[0].iam_action == "s3:GetBucketLogging"
    assert loaded.collection_health[0].iam_gaps[0].affected_resources == 2
    assert loaded.run_telemetry.snapshot_hits == 1
    assert loaded.run_telemetry.snapshot_misses == 2
    analysis = analyze(dataset)
    payload = json.loads(
        renderer.render_json(analysis.vm, analysis.opportunities)
    )
    assert payload["collection_health"]["status"] == "partial"
    assert payload["collection_health"]["sources"][1]["coverage"] == "50%"
    # A saúde da coleta saiu do HTML junto com o apêndice técnico — o desenho é
    # o documento do analista. Ela continua íntegra no JSON, que é o registro
    # completo do scan.
    registro = json.loads(renderer.render_json(analysis.vm, analysis.opportunities))
    assert registro["collection_health"]["sources"]


def test_every_source_declares_impact_and_next_action():
    """A saúde só é acionável se toda fonte disser o que quebra e o que fazer."""
    incomplete = [
        source.name
        for source in collect_module.SOURCES
        if not source.impact.strip() or not source.next_action.strip()
    ]
    assert incomplete == []

    # Fonte opcional precisa dizer também o que significa estar desligada.
    optional_without_reason = [
        source.name
        for source in collect_module.SOURCES
        if source.enabled is not None
        and not (source.disabled_impact or source.impact).strip()
    ]
    assert optional_without_reason == []


def test_a_new_source_runs_without_touching_the_orchestrator():
    """Fonte nova é dado: nada em `collect_account` precisa mudar por causa dela."""
    recorder = CollectionRecorder()
    account = Account(account_id="123456789012")
    context = collect_module.CollectionContext(
        session=FakeSession(),
        window=AnalysisWindow.trailing(),
        billing=BillingMonth.current(),
        account=account,
        config=DEFAULT_CONFIG,
    )
    novel = collect_module.Source(
        name="Amazon Redshift",
        collect=lambda ctx: ["cluster-a", "cluster-b"],
        into="state_machines",
        count=len,
        impact="clusters não avaliados",
        next_action="validar redshift:DescribeClusters",
    )

    collect_module.run(novel, context, recorder)

    assert account.state_machines == ["cluster-a", "cluster-b"]
    entry = recorder.entries[-1]
    assert entry.source == "Amazon Redshift"
    assert entry.status == "ok"
    assert entry.collected == 2


def test_a_disabled_source_is_reported_instead_of_disappearing():
    recorder = CollectionRecorder()
    context = collect_module.CollectionContext(
        session=FakeSession(),
        window=AnalysisWindow.trailing(),
        billing=BillingMonth.current(),
        account=Account(account_id="123456789012"),
        config=DEFAULT_CONFIG,
    )
    off = collect_module.Source(
        name="Amazon Redshift",
        collect=lambda ctx: [],
        enabled=lambda ctx: False,
        disabled_category="not_enabled",
        disabled_impact="clusters não classificados",
        disabled_next_action="usar --redshift quando houver cluster na conta",
        impact="clusters não avaliados",
        next_action="validar redshift:DescribeClusters",
    )

    collect_module.run(off, context, recorder)

    entry = recorder.entries[-1]
    assert entry.status == "unavailable"
    assert entry.error_category == "not_enabled"
    assert entry.impact == "clusters não classificados"
    assert entry.affects_status is False


def test_the_slowest_sources_are_shown_after_a_collection():
    """O tempo por fonte já era medido; sem mostrá-lo, otimizar vira palpite."""
    from julius.cli.collect import slowest_sources

    health = [
        CollectionHealth(source="Glue Catalog", duration_ms=42_000, collected=1830),
        CollectionHealth(source="CloudWatch Glue CPU", duration_ms=17_500, collected=300),
        CollectionHealth(source="Cost Explorer", duration_ms=900, collected=12),
        CollectionHealth(source="EventBridge Schedules", duration_ms=0),
    ]

    linhas = slowest_sources(health, limit=2)

    assert "60.4s" in linhas[0]  # o total soma todas, não só as mostradas
    assert linhas[1].strip().startswith("42.0s  Glue Catalog")
    assert "1830 itens" in linhas[1]
    assert "CloudWatch Glue CPU" in linhas[2]
    # Fonte sem tempo medido não ocupa uma das vagas.
    assert len(linhas) == 3


def test_a_collection_with_no_measured_time_shows_nothing():
    from julius.cli.collect import slowest_sources

    assert slowest_sources([CollectionHealth(source="Cost Explorer")]) == []


class _CountingSession:
    region_name = "sa-east-1"

    def __init__(self):
        self.built: list[str] = []
        self.configs: list[object] = []

    def client(self, name, **kwargs):
        self.built.append(name)
        self.configs.append(kwargs.get("config"))
        return object()


def test_each_service_client_is_built_once_and_configured():
    """Montar o cliente lê o modelo do serviço; `glue` aparece em cinco fontes.

    E o `config` não é opcional: sem ele o botocore usa retry `legacy`, que
    responde a throttling insistindo no ritmo que o causou.
    """
    session = _CountingSession()
    context = collect_module.CollectionContext(
        session=session,
        window=AnalysisWindow.trailing(),
        billing=BillingMonth.current(),
        account=Account(account_id="123456789012"),
        config=DEFAULT_CONFIG,
    )

    first = context.client("glue")
    again = context.client("glue")
    context.client("athena")

    assert first is again
    assert session.built == ["glue", "athena"]
    assert all(item is not None for item in session.configs)
    assert session.configs[0].retries["mode"] == "adaptive"
