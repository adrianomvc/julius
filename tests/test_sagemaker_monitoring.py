"""Contratos das novas coletas e decisões SageMaker."""

from __future__ import annotations

import json

from julius.collection.models import (
    Account,
    SageMakerApp,
    SageMakerEndpoint,
    SageMakerFeatureGroup,
    SageMakerJob,
    SageMakerMonitoringSchedule,
)
from julius.collection.normalizers.dump import account_to_dataset
from julius.collection.sagemaker_history import carry_consistent_scans
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.sagemaker import rules


def _idle_app(**overrides) -> SageMakerApp:
    values = {
        "name": "studio-analise",
        "instance_type": "ml.g3.4xlarge",
        "idle_hours_per_day": 18.0,
        "idle_shutdown_min": 0,
        "activity_metrics_available": True,
        "coverage_days": 30,
        "allocated_cost": 300.0,
        "cost_quality": "reconciled",
    }
    values.update(overrides)
    return SageMakerApp(**values)


def test_financial_finding_requires_90_days_or_three_consistent_scans():
    account = Account(account_id="123456789012", sagemaker_apps=[_idle_app()])

    assert rules.detect(account, DEFAULT_CONFIG, "scan") == []
    signal_ids = {item.rule_id for item in rules.signals(account, DEFAULT_CONFIG)}
    assert "SM-APP-IDLE-CANDIDATE" in signal_ids

    account.sagemaker_apps[0].consistent_scans = 3
    findings = rules.detect(account, DEFAULT_CONFIG, "scan")
    assert {item.rule_id for item in findings} == {"SM-APP-IDLE"}


def test_previous_output_is_the_checkpoint_for_consistent_scans(tmp_path):
    path = tmp_path / "account.json"
    previous = Account(
        account_id="123456789012",
        sagemaker_apps=[_idle_app(consistent_scans=2)],
    )
    path.write_text(
        json.dumps(account_to_dataset(previous), default=str),
        encoding="utf-8",
    )
    current = Account(account_id=previous.account_id, sagemaker_apps=[_idle_app()])

    carry_consistent_scans(current, path)

    assert current.sagemaker_apps[0].consistent_scans == 3
    current.sagemaker_apps[0].idle_shutdown_min = 120
    carry_consistent_scans(current, path)
    assert current.sagemaker_apps[0].consistent_scans == 1


def test_failed_job_is_reported_but_an_isolated_failure_is_not_annualized():
    account = Account(
        account_id="123456789012",
        sagemaker_jobs=[
            SageMakerJob(
                name="treino-falhou",
                kind="training",
                status="Failed",
                instance_type="ml.g5.xlarge",
                instance_count=1,
                duration_seconds=3600,
                modeled_cost=4.10,
                failure_category="algorithm",
            )
        ],
    )

    finding = rules.detect(account, DEFAULT_CONFIG, "scan")[0]

    assert finding.rule_id == "SM-TRAINING-FAILED-COST"
    assert finding.estimation.one_time_cost == 4.10
    assert finding.estimated_gain.monthly_expected == 0


def test_g3_is_contextual_and_never_claims_automatic_saving():
    account = Account(account_id="123456789012", sagemaker_apps=[_idle_app()])

    signals = rules.signals(account, DEFAULT_CONFIG)

    legacy = next(item for item in signals if item.rule_id == "SM-LEGACY-GPU-FAMILY")
    assert "G3" in legacy.observation
    assert all(link.startswith("https://docs.aws.amazon.com/") for link in legacy.doc_links)


def test_feature_store_only_recommends_capacity_with_cost_and_no_throttling():
    group = SageMakerFeatureGroup(
        name="clientes",
        throughput_mode="Provisioned",
        provisioned_read_capacity=20,
        provisioned_write_capacity=20,
        max_consumed_read_capacity=2,
        max_consumed_write_capacity=2,
        throttled_requests=0,
        allocated_cost=120.0,
        cost_quality="reconciled",
        coverage_days=90,
    )
    account = Account(account_id="123456789012", sagemaker_feature_groups=[group])

    assert {
        item.rule_id for item in rules.detect(account, DEFAULT_CONFIG, "scan")
    } == {"SM-FEATURE-STORE-PROVISIONED-IDLE"}

    group.throttled_requests = 1
    assert rules.detect(account, DEFAULT_CONFIG, "scan") == []


def test_zero_traffic_endpoint_needs_real_cost_not_a_generic_price():
    endpoint = SageMakerEndpoint(
        name="fraude",
        instance_type="ml.unknown.large",
        invocations=0,
        coverage_days=90,
    )
    account = Account(account_id="123456789012", sagemaker_endpoints=[endpoint])

    finding = rules.detect(account, DEFAULT_CONFIG, "scan")[0]

    assert finding.rule_id == "SM-ENDPOINT-ZERO-TRAFFIC"
    assert finding.blocked is True
    assert finding.estimated_gain.monthly_expected == 0


def test_existing_failed_model_monitor_schedule_becomes_health_signal():
    account = Account(
        account_id="123456789012",
        sagemaker_monitoring_schedules=[
            SageMakerMonitoringSchedule(
                name="drift-fraude",
                status="Scheduled",
                endpoint_name="fraude",
                last_execution_status="Failed",
                failure_reason="ProcessingJob failed",
            )
        ],
    )

    signal = next(
        item
        for item in rules.signals(account, DEFAULT_CONFIG)
        if item.rule_id == "SM-MODEL-MONITOR-HEALTH"
    )
    assert signal.asset_name == "drift-fraude"
    assert all(link.startswith("https://docs.aws.amazon.com/") for link in signal.doc_links)
