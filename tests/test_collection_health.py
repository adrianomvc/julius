"""Telemetria offline da saúde da coleta AWS."""

from __future__ import annotations

import json

import pytest

from julius.collection import sources as collect_module
from julius.collection.health.recorder import (
    CollectionRecorder,
    RequiredCollectionError,
)
from julius.collection.models import Account, CollectionHealth, GlueJob
from julius.collection.normalizers.dump import account_to_dataset
from julius.collection.normalizers.loader import load_account
from julius.collection.orchestrator import collect_account
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG
from julius.pipeline import analyze
from julius.report import renderer


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


class FakeSession:
    region_name = "sa-east-1"

    def client(self, name):
        return FakeSts() if name == "sts" else object()


def _patch_empty_collectors(monkeypatch):
    monkeypatch.setattr(
        collect_module.cost_explorer, "collect_services", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.jobs, "collect_jobs", lambda *_a, **_k: []
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
        collect_module.athena, "collect_queries", lambda *_a, **_k: []
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
    account = collect_account(FakeSession())

    by_source = {item.source: item for item in account.collection_health}
    assert by_source["AWS identity"].status == "ok"
    assert by_source["Glue Jobs"].required is True
    assert by_source["Spark Event Logs"].error_category == "not_configured"
    assert by_source["Table Touches"].affects_status is False
    assert by_source["CloudTrail Ownership"].affects_status is False
    # Sem job coletado não há custo Glue a atribuir; a fonte não degrada o scan.
    assert by_source["Glue Cost Explorer"].status == "ok"
    assert account.collection_status == "ok"


def test_missing_glue_billing_degrades_the_scan_when_there_are_jobs(monkeypatch):
    _patch_empty_collectors(monkeypatch)
    monkeypatch.setattr(
        collect_module.jobs,
        "collect_jobs",
        lambda *_a, **_k: [GlueJob(name="etl", worker_type="G.1X", number_of_workers=2)],
    )
    account = collect_account(FakeSession())

    billing = next(
        item
        for item in account.collection_health
        if item.source == "Glue Cost Explorer"
    )
    assert billing.status == "unavailable"
    assert billing.error_category == "no_data"
    assert "modelado por tarifa" in billing.impact
    assert account.collection_status == "partial"


def test_optional_failure_marks_scan_partial_and_glue_failure_blocks(monkeypatch):
    _patch_empty_collectors(monkeypatch)
    monkeypatch.setattr(
        collect_module.cost_explorer,
        "collect_services",
        lambda *_a, **_k: (_ for _ in ()).throw(AwsLikeError()),
    )
    account = collect_account(FakeSession())
    assert account.collection_status == "partial"
    cost = next(
        item for item in account.collection_health if item.source == "Cost Explorer"
    )
    assert cost.error_category == "permission_denied"

    monkeypatch.setattr(
        collect_module.jobs,
        "collect_jobs",
        lambda *_a, **_k: (_ for _ in ()).throw(AwsLikeError()),
    )
    with pytest.raises(RequiredCollectionError, match="Glue Jobs"):
        collect_account(FakeSession())


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
    dataset = tmp_path / "health.json"
    dataset.write_text(
        json.dumps(account_to_dataset(account)),
        encoding="utf-8",
    )
    loaded = load_account(dataset)
    assert loaded.collection_status == "partial"
    analysis = analyze(dataset)
    payload = json.loads(
        renderer.render_json(analysis.vm, analysis.opportunities)
    )
    assert payload["collection_health"]["status"] == "partial"
    assert payload["collection_health"]["sources"][1]["coverage"] == "50%"
    assert "Saúde da coleta" in renderer.render_html(analysis.vm)


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
