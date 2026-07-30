from __future__ import annotations

from datetime import datetime, timezone

import pytest

from julius.collection.models import Account, SageMakerJob, StateMachine
from julius.collection.window import AnalysisWindow
from julius.config import DEFAULT_CONFIG
from julius.findings.investigation import AIEstimationProposal
from julius.findings.lifecycle import LifecycleEvent
from julius.findings.opportunity import EstimatedGain, Estimation, Opportunity
from julius.findings.signal import Signal
from julius.knowledge.contextual_estimation import evaluate_proposal
from julius.state.history import HistoryStore
from julius.state.validation import validate_benefit


def _opportunity(expected: float = 100) -> Opportunity:
    return Opportunity(
        opportunity_id="RULE-job",
        account="consumer",
        asset_type="glue_job",
        asset_name="job",
        category="cost",
        rule_id="RULE",
        finding="finding",
        recommended_action="action",
        estimated_gain=EstimatedGain(
            monthly_low=expected * 0.8,
            monthly_expected=expected,
            monthly_high=expected * 1.2,
        ),
        estimation=Estimation(
            method="test",
            baseline_cost=expected,
            projected_cost=0,
            estimated_saving=expected,
        ),
    )


def _signal(rule_id: str, asset_type: str, asset_name: str) -> Signal:
    return Signal(
        kind="config",
        rule_id=rule_id,
        asset_type=asset_type,
        asset_name=asset_name,
        observation="padrão observado",
        question="o cenário é aplicável?",
    )


def test_spot_contextual_estimate_is_never_included_in_portfolio():
    account = Account("123")
    account.sagemaker_jobs = [
        SageMakerJob(
            name="train",
            kind="training",
            checkpoint_configured=True,
            allocated_cost=100,
            duration_seconds=3600,
            instance_count=1,
        )
    ]
    estimate = evaluate_proposal(
        account,
        _signal(
            "SM-TRAINING-SPOT-CANDIDATE", "sagemaker_training_job", "train"
        ),
        AIEstimationProposal("sagemaker_managed_spot_training_v1"),
        DEFAULT_CONFIG,
    )
    assert estimate.status == "estimated"
    assert (estimate.estimated_low, estimate.estimated_expected, estimate.estimated_high) == (
        50,
        70,
        90,
    )
    assert estimate.include_in_portfolio is False


def test_express_estimate_requires_real_benchmark():
    account = Account("123")
    account.state_machines = [
        StateMachine(
            name="flow",
            executions_per_month=10_000,
            avg_duration_sec=5,
            avg_state_transitions=8,
        )
    ]
    estimate = evaluate_proposal(
        account,
        _signal("SFN-STANDARD-TO-EXPRESS", "state_machine", "flow"),
        AIEstimationProposal("sfn_standard_to_express_v1"),
        DEFAULT_CONFIG,
    )
    assert estimate.status == "needs_evidence"
    assert "benchmark de duração Express" in estimate.missing_evidence


def test_proposal_method_is_allowlisted_per_rule():
    with pytest.raises(ValueError, match="não é permitido"):
        evaluate_proposal(
            Account("123"),
            _signal("SFN-STANDARD-TO-EXPRESS", "state_machine", "flow"),
            AIEstimationProposal("sagemaker_managed_spot_training_v1"),
            DEFAULT_CONFIG,
        )


def test_monthly_window_is_a_complete_utc_calendar_month():
    window = AnalysisWindow.calendar_month("2026-02")
    assert window.start.isoformat() == "2026-02-01T00:00:00+00:00"
    assert window.end.isoformat() == "2026-03-01T00:00:00+00:00"
    assert window.days == 28
    previous = AnalysisWindow.previous_calendar_month(
        now=datetime(2026, 7, 29, tzinfo=timezone.utc)
    )
    assert previous.start_date.isoformat() == "2026-06-01"
    assert previous.data_through.isoformat() == "2026-06-30"


def test_absolute_comparison_is_recorded_but_cannot_train_calibration():
    result = validate_benefit(
        _opportunity(expected=100),
        baseline_cost=100,
        after_cost=70,
        actor="finops",
        output_equivalent=True,
    )
    assert result.realized_monthly == 30
    assert result.eligible_for_calibration is False


def test_monthly_validation_waits_for_a_full_stable_month(tmp_path):
    opportunity = _opportunity()
    with HistoryStore(tmp_path / "history.duckdb") as history:
        history.record_lifecycle_event(
            LifecycleEvent(
                fingerprint=opportunity.fingerprint(),
                account=opportunity.account,
                opportunity_id=opportunity.opportunity_id,
                from_status="planned",
                to_status="implemented",
                actor="finops",
                reason="piloto aprovado",
                occurred_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            )
        )
        august = history.validation_window_status(
            opportunity.fingerprint(), "2026-08"
        )
        september = history.validation_window_status(
            opportunity.fingerprint(), "2026-09"
        )
    assert august[0] is False
    assert september == (True, "mês completo estável")
