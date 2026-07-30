from datetime import datetime, timezone

from julius.collection.health import CollectionRecorder
from julius.collection.models import (
    Account,
    AthenaCapacityReservation,
    S3Prefix,
    StateMachine,
)
from julius.collection.policy import policy_for_profile
from julius.collection.sources import CollectionContext, Source, run
from julius.collection.telemetry import InstrumentedClient, RunTelemetry
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.athena import capacity as capacity_rules
from julius.knowledge.rules.s3 import rules as s3_rules
from julius.knowledge.rules.stepfunctions import rules as stepfunctions_rules


def test_consumer_scope_includes_redshift_and_isolates_access_failure():
    class Session:
        def client(self, _service):
            raise AssertionError("fonte fora do escopo não pode criar cliente")

    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    account = Account(account_id="123", scope_profile="consumer_datamesh")
    context = CollectionContext(
        session=Session(),
        window=AnalysisWindow.trailing(days=30, now=now),
        billing=BillingMonth.current(now=now),
        account=account,
        config=DEFAULT_CONFIG,
        scope_policy=policy_for_profile("consumer_datamesh"),
    )
    source = Source(
        name="Redshift probe",
        collect=lambda ctx: ctx.client("redshift").describe_clusters(),
        impact="",
        next_action="",
        required_capabilities=frozenset({"redshift"}),
    )
    recorder = CollectionRecorder()

    run(source, context, recorder)

    assert recorder.entries[0].status == "unavailable"
    assert recorder.entries[0].error_category == "invalid_response"


def test_athena_capacity_reduces_only_one_safe_step():
    reservation = AthenaCapacityReservation(
        name="consumer",
        status="ACTIVE",
        target_dpus=20,
        consumed_dpus_p95=6,
        query_queue_p95_ms=200,
        workgroups=["wg"],
        coverage_days=30,
        allocated_cost=1000,
        cost_quality="allocated",
    )
    account = Account(
        account_id="123", athena_capacity_reservations=[reservation]
    )

    found = capacity_rules.detect(account, DEFAULT_CONFIG, "scan")

    assert len(found) == 1
    assert "16 DPUs" in found[0].recommended_action
    assert found[0].estimated_gain.monthly_expected > 0


def test_consumer_s3_cleanup_is_evidence_without_savings():
    account = Account(
        account_id="123",
        scope_profile="consumer_datamesh",
        s3_mode="evidence_only",
        s3_prefixes=[
            S3Prefix(
                bucket="lake",
                prefix="staging/",
                kind="staging",
                stale_object_count=200,
                stale_bytes=10 * 1024**3,
            )
        ],
    )

    assert s3_rules.detect(account, DEFAULT_CONFIG, "scan") == []
    signals = s3_rules.signals(account, DEFAULT_CONFIG)
    assert any(item.rule_id == "S3-JOB-STAGING-LEFTOVER" for item in signals)


def test_express_financial_opportunity_requires_complete_benchmark():
    machine = StateMachine(
        name="flow",
        type="STANDARD",
        executions_per_month=10_000,
        avg_state_transitions=20,
        avg_duration_sec=2,
        idempotent=True,
        express_benchmark_duration_ms=250,
        express_benchmark_memory_mb=96,
        sampled_executions=20,
        observed_runs=20,
        coverage_days=30,
    )
    account = Account(account_id="123", state_machines=[machine])

    found = stepfunctions_rules.detect(account, DEFAULT_CONFIG, "scan")

    opportunity = next(
        item for item in found if item.rule_id == "SFN-STANDARD-TO-EXPRESS"
    )
    assert opportunity.estimation is not None
    assert opportunity.estimation.method == "sfn_standard_to_express_v2"
    assert opportunity.estimation.projected_cost > 0


def test_cost_explorer_calls_are_cached_and_priced_per_page():
    class Client:
        def get_cost_and_usage(self, **_kwargs):
            return {"ResultsByTime": [], "ResponseMetadata": {"RetryAttempts": 1}}

    telemetry = RunTelemetry()
    wrapped = InstrumentedClient(Client(), "ce", telemetry, {})

    wrapped.get_cost_and_usage(TimePeriod={"Start": "2026-07-01", "End": "2026-08-01"})
    wrapped.get_cost_and_usage(TimePeriod={"Start": "2026-07-01", "End": "2026-08-01"})
    telemetry.estimate(DEFAULT_CONFIG.pricing)

    stat = telemetry.api_calls["ce:get_cost_and_usage"]
    assert stat.calls == 1
    assert stat.cache_hits == 1
    assert stat.retries == 1
    assert telemetry.estimated_cost_usd == 0.01
