"""Regressões dos sinais de Glue, Athena, S3 e storage do SageMaker."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from verified_pricing import verified_config

from julius.collection.collectors.athena.aggregate import aggregate_queries
from julius.collection.collectors.athena.evidence import AthenaExecutionEvidence
from julius.collection.collectors.s3_cost import collect_s3_costs
from julius.collection.collectors.sagemaker_cost import (
    allocate_costs,
    collect_sagemaker_costs,
)
from julius.collection.collectors.sagemaker_extended import _apply_efs_metrics
from julius.collection.models import (
    Account,
    AthenaCoverage,
    GlueJob,
    S3CostCoverage,
    S3CostLine,
    SageMakerCostCoverage,
    SageMakerDomain,
    SageMakerSpace,
)
from julius.collection.normalizers.dump import account_to_dataset
from julius.collection.normalizers.loader import load_account
from julius.collection.window import AnalysisWindow
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.glue import jobs as glue_jobs
from julius.knowledge.rules.sagemaker import rules as sagemaker_rules


def _window() -> AnalysisWindow:
    return AnalysisWindow(
        start=datetime(2026, 6, 29, tzinfo=timezone.utc),
        end=datetime(2026, 7, 29, tzinfo=timezone.utc),
        days=30,
    )


def test_glue_inactivity_uses_the_collection_window_and_respects_missing_history():
    inactive = GlueJob(
        name="inactive",
        last_run_at="2026-04-19T00:00:00+00:00",
        window_end="2026-07-29T00:00:00+00:00",
    )
    denied = GlueJob(
        name="denied",
        last_run_at="2025-01-01T00:00:00+00:00",
        window_end="2026-07-29T00:00:00+00:00",
        run_history_available=False,
    )
    account = Account(account_id="123456789012", glue_jobs=[inactive, denied])

    signal_ids = {
        (item.asset_name, item.rule_id)
        for item in glue_jobs.signals(account, DEFAULT_CONFIG)
    }

    assert ("inactive", "GLUE-JOB-INACTIVE-90D") in signal_ids
    assert ("denied", "GLUE-JOB-INACTIVE-90D") not in signal_ids
    assert "GLUE-JOB-ABANDONED" not in {
        item.rule_id
        for item in glue_jobs.detect(account, DEFAULT_CONFIG, "scan")
        if item.asset_name == "denied"
    }


def test_glue_abandoned_observability_logging_and_small_files_are_reported():
    abandoned = GlueJob(
        name="abandoned",
        created_at="2025-01-01T00:00:00+00:00",
        window_end="2026-07-29T00:00:00+00:00",
    )
    measured = GlueJob(
        name="measured",
        glue_version="4.0",
        runs_in_window=3,
        observability_enabled=False,
        continuous_logging_enabled=False,
        files_written_window=200,
        bytes_written_window=2 * 1024**3,
    )
    account = Account(
        account_id="123456789012",
        glue_jobs=[abandoned, measured],
    )

    finding_ids = {
        item.rule_id for item in glue_jobs.detect(account, DEFAULT_CONFIG, "scan")
    }
    signal_ids = {
        item.rule_id for item in glue_jobs.signals(account, DEFAULT_CONFIG)
    }

    assert {"GLUE-JOB-ABANDONED", "GLUE-SMALL-FILES-OUTPUT"} <= finding_ids
    assert {"GLUE-OBSERVABILITY-OFF", "GLUE-CONTINUOUS-LOGGING-OFF"} <= signal_ids


def test_glue_observability_signal_is_limited_to_supported_versions():
    account = Account(
        account_id="123456789012",
        glue_jobs=[
            GlueJob(
                name="v3",
                glue_version="3.0",
                runs_in_window=1,
                observability_enabled=False,
            ),
            GlueJob(
                name="v4",
                glue_version="4.0",
                runs_in_window=1,
                observability_enabled=False,
            ),
        ],
    )

    assets = {
        item.asset_name
        for item in glue_jobs.signals(account, DEFAULT_CONFIG)
        if item.rule_id == "GLUE-OBSERVABILITY-OFF"
    }

    assert assets == {"v4"}


def test_glue_bookmark_only_values_measured_reprocessing():
    job = GlueJob(
        name="incremental",
        worker_type="G.1X",
        number_of_workers=2,
        avg_execution_sec=3600,
        runs_in_window=8,
        bytes_read_window=1000,
        incremental_source_evidence=True,
    )
    account = Account(account_id="123456789012", glue_jobs=[job])

    candidate = next(
        item
        for item in glue_jobs.detect(account, DEFAULT_CONFIG, "scan")
        if item.rule_id == "GLUE-BOOKMARK-OFF"
    )
    assert candidate.blocked is True
    assert candidate.estimation.saving_quality == "unavailable"
    assert candidate.estimated_gain.monthly_expected == 0

    job.redundant_read_bytes_window = 250
    measured = next(
        item
        for item in glue_jobs.detect(account, DEFAULT_CONFIG, "scan")
        if item.rule_id == "GLUE-BOOKMARK-OFF"
    )
    assert measured.blocked is False
    assert measured.estimation.saving_quality == "measured"
    assert measured.estimation.estimated_saving > 0


def test_glue_rightsizing_stays_potential_until_benchmark_is_validated():
    config = verified_config("glue")
    job = GlueJob(
        name="rightsizing",
        worker_type="G.1X",
        number_of_workers=8,
        auto_scaling=True,
        avg_execution_sec=3600,
        runs_in_window=6,
        observed_runs=6,
        coverage_days=30,
        avg_worker_utilization=0.20,
        max_memory_used_pct=0.30,
        max_disk_used_pct=0.20,
        has_spill_evidence=True,
        spark_event_log_evidence_complete=True,
        shuffle_spill_bytes=0,
    )
    account = Account(account_id="123456789012", glue_jobs=[job])

    candidate = next(
        item
        for item in glue_jobs.detect(account, config, "scan")
        if item.rule_id == "GLUE-OVERPROVISIONED"
    )
    assert candidate.blocked is True
    assert candidate.estimation.saving_quality == "potential"
    assert candidate.estimated_gain.is_strategic is True

    job.rightsize_tested_workers = 4
    job.rightsize_test_runs = 3
    job.rightsize_output_validated = True
    validated = next(
        item
        for item in glue_jobs.detect(account, config, "scan")
        if item.rule_id == "GLUE-OVERPROVISIONED"
    )
    assert validated.blocked is False
    assert validated.estimation.saving_quality == "modeled_evidence"
    assert validated.estimated_gain.is_strategic is False


def test_athena_preserves_requested_reuse_configuration_in_the_pattern():
    evidence = AthenaExecutionEvidence(
        query_execution_id="q-1",
        workgroup="primary",
        submitted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        state="SUCCEEDED",
        statement_type="DML",
        raw_sql="select 1",
        exact_fingerprint="exact",
        structural_fingerprint="shape",
        reuse_configured=True,
        reuse_max_age_minutes=45,
    )
    coverage = AthenaCoverage(cost_quality="reconciled", currency="USD")

    pattern = aggregate_queries([evidence], coverage)[0]

    assert pattern.result_reuse_enabled is True
    assert pattern.reuse_configured_runs == 1
    assert pattern.reuse_max_age_minutes == 45


class _CostExplorer:
    def get_cost_and_usage(self, **kwargs):
        assert kwargs["Metrics"] == ["NetUnblendedCost", "UsageQuantity"]
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Requests-Tier1"],
                            "Metrics": {
                                "NetUnblendedCost": {
                                    "Amount": "3.50",
                                    "Unit": "USD",
                                },
                                "UsageQuantity": {
                                    "Amount": "125000",
                                    "Unit": "Requests",
                                },
                            },
                        }
                    ]
                }
            ]
        }


def test_s3_cost_keeps_request_count_next_to_request_cost(tmp_path):
    coverage = collect_s3_costs(
        _CostExplorer(),
        window=_window(),
        markers=(("request", "requests"),),
    )
    account = Account(account_id="123456789012", s3_cost_coverage=coverage)
    path = tmp_path / "account.json"
    path.write_text(
        json.dumps(account_to_dataset(account)),
        encoding="utf-8",
    )

    restored = load_account(path)

    assert coverage.lines == [
        S3CostLine(
            usage_type="Requests-Tier1",
            bucket="requests",
            cost=3.5,
            usage_quantity=125000.0,
            usage_unit="Requests",
        )
    ]
    assert restored.s3_cost_coverage is not None
    assert restored.s3_cost_coverage.lines[0].usage_quantity == 125000.0


def test_s3_request_quantity_rejects_incompatible_units():
    coverage = S3CostCoverage(
        buckets={"requests_read": 4.0},
        lines=[
            S3CostLine(
                usage_type="Requests-Tier2",
                bucket="requests_read",
                cost=4.0,
                usage_quantity=20,
                usage_unit="GB",
            )
        ],
    )

    assert coverage.quantity_for({"requests_read"}) is None
    assert coverage.unit_cost_for({"requests_read"}) is None


def test_sagemaker_idle_storage_signals_need_mature_evidence_and_do_not_claim_saving():
    space = SageMakerSpace(
        name="analise",
        domain_id="d-123",
        ebs_volume_size_gb=100,
        active_app_count=0,
        consistent_scans=3,
    )
    domain = SageMakerDomain(
        domain_id="d-123",
        efs_storage_bytes=20 * 1024**3,
        efs_total_io_bytes=0,
        active_app_count=0,
        coverage_days=90,
    )
    account = Account(
        account_id="123456789012",
        sagemaker_spaces=[space],
        sagemaker_domains=[domain],
    )

    ids = {
        item.rule_id for item in sagemaker_rules.signals(account, DEFAULT_CONFIG)
    }

    assert {"SM-SPACE-STORAGE-IDLE", "SM-DOMAIN-EFS-IDLE"} <= ids
    assert sagemaker_rules.detect(account, DEFAULT_CONFIG, "scan") == []


def test_sagemaker_idle_storage_becomes_blocked_financial_opportunity_with_cost():
    space = SageMakerSpace(
        name="analise",
        domain_id="d-123",
        ebs_volume_size_gb=100,
        active_app_count=0,
        coverage_days=90,
        allocated_storage_cost=12.5,
        cost_quality="reconciled",
        cost_coverage_days=30,
    )
    domain = SageMakerDomain(
        domain_id="d-123",
        efs_storage_bytes=20 * 1024**3,
        efs_total_io_bytes=0,
        efs_client_connections=0,
        active_app_count=0,
        coverage_days=90,
        allocated_storage_cost=8.0,
        cost_quality="reconciled",
        cost_coverage_days=30,
    )
    account = Account(
        account_id="123456789012",
        sagemaker_spaces=[space],
        sagemaker_domains=[domain],
    )

    findings = sagemaker_rules.detect(account, DEFAULT_CONFIG, "scan")
    by_rule = {item.rule_id: item for item in findings}

    assert {
        "SM-SPACE-STORAGE-IDLE",
        "SM-DOMAIN-EFS-STORAGE-IDLE",
    } <= set(by_rule)
    assert all(item.blocked for item in by_rule.values())
    assert by_rule["SM-SPACE-STORAGE-IDLE"].estimation.estimated_saving == 12.5
    assert (
        by_rule["SM-DOMAIN-EFS-STORAGE-IDLE"].estimation.estimated_saving
        == 8.0
    )

    domain.efs_client_connections = 1
    remaining = {
        item.rule_id
        for item in sagemaker_rules.detect(account, DEFAULT_CONFIG, "scan")
    }
    assert "SM-DOMAIN-EFS-STORAGE-IDLE" not in remaining


def test_studio_volume_cost_is_allocated_only_to_space_storage():
    space = SageMakerSpace(name="analise", domain_id="d-123", ebs_volume_size_gb=100)
    account = Account(account_id="123456789012", sagemaker_spaces=[space])
    coverage = SageMakerCostCoverage(
        buckets={"studio_storage": 12.5},
        window_days=30,
    )

    allocate_costs(account, coverage, {"studio_storage"})

    assert space.allocated_storage_cost == 12.5
    assert space.cost_coverage_days == 30
    assert coverage.allocated_buckets == ["studio_storage"]
    assert coverage.cost_quality == "reconciled"


def test_sagemaker_cost_collection_consumes_all_cost_explorer_pages():
    class CostExplorer:
        def get_cost_and_usage(self, **kwargs):
            page = kwargs.get("NextPageToken")
            amount = "2.0" if page else "1.0"
            response = {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["Studio-Apps"],
                                "Metrics": {
                                    "NetUnblendedCost": {
                                        "Amount": amount,
                                        "Unit": "USD",
                                    }
                                },
                            }
                        ]
                    }
                ]
            }
            if page is None:
                response["NextPageToken"] = "next"
            return response

    coverage = collect_sagemaker_costs(
        CostExplorer(),
        window=_window(),
        markers=(("studio", "studio"),),
    )

    assert coverage.buckets == {"studio": 3.0}
    assert coverage.window_days == 30


class _CloudWatch:
    """Responde `GetMetricData`, guardando as consultas de cada chamada."""

    def __init__(self):
        self.calls = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "MetricDataResults": [
                {"Id": query["Id"], "Values": [1.0]}
                for query in kwargs["MetricDataQueries"]
            ]
        }

    @property
    def queries(self) -> list[dict]:
        return [
            query["MetricStat"]["Metric"]
            for call in self.calls
            for query in call["MetricDataQueries"]
        ]


def test_efs_storage_metric_uses_the_required_total_storage_class_dimension():
    client = _CloudWatch()
    domain = SageMakerDomain(
        domain_id="d-123",
        home_efs_file_system_id="fs-123",
    )

    _apply_efs_metrics(client, [domain], _window())

    storage = next(q for q in client.queries if q["MetricName"] == "StorageBytes")
    assert {"Name": "StorageClass", "Value": "Total"} in storage["Dimensions"]


def test_efs_metrics_of_every_domain_fit_in_one_call():
    """Eram cinco chamadas por domain; três domains custavam quinze."""
    client = _CloudWatch()
    domains = [
        SageMakerDomain(domain_id=f"d-{i}", home_efs_file_system_id=f"fs-{i}")
        for i in range(3)
    ]

    _apply_efs_metrics(client, domains, _window())

    assert len(client.calls) == 1
    assert len(client.queries) == 3 * 5


def test_a_domain_without_efs_is_not_asked_about():
    """Sem `FileSystemId` não há o que perguntar ao CloudWatch."""
    client = _CloudWatch()
    domains = [SageMakerDomain(domain_id="d-1", home_efs_file_system_id="")]

    _apply_efs_metrics(client, domains, _window())

    assert client.calls == []
