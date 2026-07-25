"""Telemetria offline da saúde da coleta AWS."""

from __future__ import annotations

import json

import pytest

from julius.aws import collect as collect_module
from julius.aws.collection_health import (
    CollectionRecorder,
    RequiredCollectionError,
)
from julius.ingest.dump import account_to_dataset
from julius.ingest.loader import load_account
from julius.inventory.model import Account, CollectionHealth, GlueJob
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
        collect_module.glue_collector, "collect_jobs", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.glue_collector, "collect_tables", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.crawlers_collector, "collect_crawlers", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.glue_triggers_collector, "collect_triggers", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.databrew_collector, "collect_jobs", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.cloudwatch_collector, "enrich_glue_cpu", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        collect_module.cloudwatch_collector,
        "enrich_glue_observability",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        collect_module.sessions_collector, "collect_sessions", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.athena_collector, "collect_queries", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        collect_module.stepfunctions_collector,
        "collect_state_machines",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        collect_module.schedules_collector, "collect_schedules", lambda *_a, **_k: []
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
    account = collect_module.collect_account(FakeSession())

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
        collect_module.glue_collector,
        "collect_jobs",
        lambda *_a, **_k: [GlueJob(name="etl", worker_type="G.1X", number_of_workers=2)],
    )
    account = collect_module.collect_account(FakeSession())

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
    account = collect_module.collect_account(FakeSession())
    assert account.collection_status == "partial"
    cost = next(
        item for item in account.collection_health if item.source == "Cost Explorer"
    )
    assert cost.error_category == "permission_denied"

    monkeypatch.setattr(
        collect_module.glue_collector,
        "collect_jobs",
        lambda *_a, **_k: (_ for _ in ()).throw(AwsLikeError()),
    )
    with pytest.raises(RequiredCollectionError, match="Glue Jobs"):
        collect_module.collect_account(FakeSession())


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
