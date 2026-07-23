"""Ciclo fechado do MVP 3: lifecycle, diff, validação e calibração."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from julius.config import DEFAULT_CONFIG
from julius.estimation.calibration import apply_calibrations
from julius.ingest import load_account
from julius.opportunities.base import EstimatedGain, Estimation, Opportunity
from julius.opportunities.lifecycle import can_transition
from julius.pipeline import analyze_account
from julius.state import BacklogStore, HistoryStore, validate_benefit

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "consumer-avi.json"


def _opportunity(
    name: str = "job-a",
    *,
    expected: float = 1300.0,
    evidence: list[str] | None = None,
) -> Opportunity:
    return Opportunity(
        opportunity_id=f"RULE-{name}",
        account="consumer",
        asset_type="glue_job",
        asset_name=name,
        category="cost_optimization",
        rule_id="RULE",
        finding="capacidade ociosa",
        recommended_action="reduzir capacidade",
        how_to_validate="comparar custo por execução",
        evidence=evidence or ["CPU 20%"],
        estimated_gain=EstimatedGain(
            monthly_low=expected * 0.8,
            monthly_expected=expected,
            monthly_high=expected * 1.2,
            annual_potential=expected * 12,
            realizable_year=expected * 10,
        ),
        estimation=Estimation(
            method="test",
            baseline_cost=expected,
            projected_cost=0,
            estimated_saving=expected,
        ),
        confidence=0.9,
        gain_score=100,
        execution_priority=90,
        strategic_priority=60,
        owner="Squad",
    )


def test_lifecycle_transitions_and_reopen_on_new_evidence(tmp_path):
    backlog = BacklogStore(tmp_path / "backlog.json")
    first = _opportunity(evidence=["CPU 20%"])
    backlog.reconcile([first], "scan-1", date(2026, 7, 1), account_id="consumer")

    event = backlog.transition(
        first.fingerprint(),
        "dismissed",
        actor="especialista",
        reason="carga sazonal conhecida",
    )
    assert event.from_status == "detected"
    assert backlog.status_for(first.fingerprint()) == "dismissed"

    unchanged = _opportunity(evidence=["CPU 20%"])
    rec = backlog.reconcile(
        [unchanged], "scan-2", date(2026, 7, 2), account_id="consumer"
    )
    assert unchanged.fingerprint() in rec.suppressed

    changed = _opportunity(evidence=["CPU 20%", "30 execuções novas"])
    rec = backlog.reconcile(
        [changed], "scan-3", date(2026, 7, 3), account_id="consumer"
    )
    assert changed.status == "detected"
    assert changed.fingerprint() in rec.reopened
    assert changed.fingerprint() not in rec.suppressed


def test_full_manual_lifecycle(tmp_path):
    backlog = BacklogStore(tmp_path / "backlog.json")
    opportunity = _opportunity()
    backlog.reconcile([opportunity], "scan-1", account_id="consumer")
    for status in ("reviewed", "accepted", "planned", "implemented"):
        event = backlog.transition(
            opportunity.fingerprint(),
            status,
            actor="owner",
            reason=f"avançar para {status}",
        )
        assert event.to_status == status
    assert can_transition("implemented", "validated")
    with pytest.raises(ValueError, match="Transição inválida"):
        backlog.transition(
            opportunity.fingerprint(),
            "accepted",
            actor="owner",
            reason="retrocesso inválido",
        )


def test_validated_stays_suppressed_until_evidence_changes(tmp_path):
    backlog = BacklogStore(tmp_path / "backlog.json")
    original = _opportunity(evidence=["CPU 20%"])
    backlog.reconcile([original], "scan-1", account_id="consumer")
    for status in ("reviewed", "accepted", "planned", "implemented", "validated"):
        backlog.transition(
            original.fingerprint(),
            status,
            actor="owner",
            reason=f"avançar para {status}",
        )

    same = _opportunity(evidence=["CPU 20%"])
    rec = backlog.reconcile([same], "scan-2", account_id="consumer")
    assert same.fingerprint() in rec.suppressed
    assert same.status == "validated"

    changed = _opportunity(evidence=["CPU 20%", "custo voltou a subir"])
    rec = backlog.reconcile([changed], "scan-3", account_id="consumer")
    assert changed.fingerprint() in rec.reopened
    assert changed.status == "detected"


def test_two_runs_emit_worsened_and_disappeared_events(tmp_path):
    backlog = BacklogStore(tmp_path / "backlog.json")
    with HistoryStore(tmp_path / "history.duckdb") as history:
        first_account = load_account(SAMPLE)
        first = analyze_account(
            first_account,
            store=backlog,
            history=history,
            today=date(2026, 7, 1),
        )
        assert any(event.event_type == "new_opportunity" for event in first.events)

        second_account = load_account(SAMPLE)
        target = next(
            job for job in second_account.glue_jobs if job.name == "processa_interacoes"
        )
        target.number_of_workers = 40
        target.avg_cpu_load = 0.1
        second = analyze_account(
            second_account,
            store=backlog,
            history=history,
            today=date(2026, 7, 2),
        )
        assert any(
            event.event_type in {"worsened", "new_evidence"}
            and event.asset_name == "processa_interacoes"
            for event in second.events
        )

        empty = load_account(SAMPLE)
        empty.glue_jobs = []
        empty.interactive_sessions = []
        empty.athena_queries = []
        empty.state_machines = []
        empty.sagemaker_apps = []
        empty.sagemaker_endpoints = []
        empty.tables = []
        third = analyze_account(
            empty,
            store=backlog,
            history=history,
            today=date(2026, 7, 3),
        )
        assert any(event.event_type == "disappeared" for event in third.events)


def test_validation_absolute_and_normalized():
    opportunity = _opportunity(expected=1300)
    absolute = validate_benefit(
        opportunity,
        baseline_cost=4500,
        after_cost=3100,
        actor="finops",
    )
    assert absolute.realized_monthly == 1400
    assert absolute.estimation_precision == pytest.approx(0.9231)

    normalized = validate_benefit(
        opportunity,
        baseline_cost=4500,
        after_cost=3100,
        baseline_volume=10,
        after_volume=5,
        actor="finops",
    )
    assert normalized.baseline_cost_per_unit == 450
    assert normalized.after_cost_per_unit == 620
    assert normalized.normalized_saving == -850
    assert normalized.realized_monthly == -850


def test_three_validations_calibrate_future_estimate(tmp_path):
    with HistoryStore(tmp_path / "history.duckdb") as history:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for index in range(3):
            opportunity = _opportunity(f"job-{index}", expected=100)
            result = validate_benefit(
                opportunity,
                baseline_cost=100,
                after_cost=20,
                actor="finops",
                validated_at=start + timedelta(days=index),
            )
            history.record_validation(result)

        calibration = history.calibration_for("RULE")
        assert calibration is not None
        assert calibration.sample_count == 3
        assert calibration.factor == 0.8

        future = _opportunity("future", expected=100)
        apply_calibrations([future], history, DEFAULT_CONFIG)
        assert future.estimated_gain.monthly_expected == 80
        assert future.estimation.estimated_saving == 80
        assert future.calibration_factor == 0.8
