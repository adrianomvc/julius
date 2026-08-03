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


def test_the_ai_chooses_the_scenario_and_the_engine_runs_the_formula():
    """Os métodos novos seguem o contrato: proposta é cenário, não número."""
    from julius.collection.models import GlueJob, SageMakerJob
    from julius.findings.investigation import AIEstimationProposal
    from julius.knowledge.contextual_estimation import evaluate_proposal

    account = Account(
        account_id="123456789012",
        sagemaker_jobs=[
            SageMakerJob(
                name="treino",
                kind="training",
                instance_type="ml.p3.2xlarge",
                instance_count=1,
                modeled_cost=400.0,
                cost_quality="modeled",
            )
        ],
        glue_jobs=[
            GlueJob(
                name="etl",
                shuffle_write_bytes=8 * 1024**3,
                has_spill_evidence=True,
                dpu_seconds_window=360000,
            )
        ],
    )

    gpu = evaluate_proposal(
        account,
        _signal("SM-CODE-CPU-ONLY-ON-GPU", "sagemaker_training_job", "treino"),
        AIEstimationProposal(method="sagemaker_gpu_to_cpu_instance_v1", target={}),
        DEFAULT_CONFIG,
    )
    assert gpu.status == "estimated"
    assert gpu.estimated_low < gpu.estimated_expected <= gpu.estimated_high

    shuffle = evaluate_proposal(
        account,
        _signal("GLUE-CODE-SHUFFLE", "glue_job", "etl"),
        AIEstimationProposal(
            method="glue_shuffle_reduction_v1", target={"expected_reduction": 0.2}
        ),
        DEFAULT_CONFIG,
    )
    assert shuffle.status == "estimated"
    assert shuffle.estimated_high == round(shuffle.baseline_cost * 0.2, 2)


def test_a_method_cannot_be_proposed_for_a_signal_it_does_not_answer():
    """`_ALLOWED` é o que impede a proposta de virar carta branca."""
    import pytest

    from julius.findings.investigation import AIEstimationProposal
    from julius.knowledge.contextual_estimation import evaluate_proposal

    with pytest.raises(ValueError):
        evaluate_proposal(
            Account(account_id="123456789012"),
            _signal("GLUE-CODE-SHUFFLE", "glue_job", "etl"),
            AIEstimationProposal(
                method="sagemaker_managed_spot_training_v1", target={}
            ),
            DEFAULT_CONFIG,
        )


def test_every_method_the_engine_accepts_is_announced_to_the_ai():
    """Método que o motor aceita e o briefing não cita é cálculo desligado.

    Foi o que aconteceu: `_ALLOWED` cresceu para cinco métodos, o texto do
    briefing continuou com os três originais escritos à mão, e
    `glue_shuffle_reduction_v1` e `sagemaker_gpu_to_cpu_instance_v1` ficaram
    impossíveis de propor — a IA não tinha o nome, e `evaluate_proposal` recusa
    qualquer outro. Implementados, testados e inalcançáveis.
    """
    from julius.analysis.guardrails import _division_of_labour
    from julius.knowledge.contextual_estimation import allowed_methods

    briefing = _division_of_labour()
    ausentes = sorted(
        {method for method in allowed_methods().values() if method not in briefing}
    )

    assert not ausentes, (
        f"método aceito pelo motor e ausente do briefing: {ausentes}. "
        "A lista é gerada de `contextual_estimation.allowed_methods()`; "
        "se ela deixou de aparecer, alguém voltou a escrevê-la à mão."
    )


def test_the_briefing_pairs_each_rule_id_with_the_method_that_answers_it():
    """O motor recusa método que não responde àquele sinal.

    Anunciar só a lista de nomes obrigaria a IA a adivinhar o pareamento, e o
    erro sairia como veredito rejeitado em vez de estimativa.
    """
    from julius.analysis.guardrails import _division_of_labour
    from julius.knowledge.contextual_estimation import allowed_methods

    briefing = _division_of_labour()

    for rule_id, method in allowed_methods().items():
        assert f"`{rule_id}` → `{method}`" in briefing, (
            f"o briefing não diz que {rule_id} é respondido por {method}"
        )


def test_a_method_that_reads_a_target_declares_which_key_it_needs():
    """Alvo exigido pela validação e não anunciado faz a proposta nascer morta.

    A varredura é sobre o próprio módulo de estimativa: toda chave lida de
    `proposal.target` precisa estar declarada em `_TARGET`, senão o briefing
    anuncia o método sem dizer o que ele exige e `evaluate_proposal` levanta
    `ValueError` na primeira tentativa.
    """
    import ast
    from pathlib import Path

    from julius.knowledge import contextual_estimation

    fonte = Path(contextual_estimation.__file__).read_text(encoding="utf-8")
    lidas = set()
    for no in ast.walk(ast.parse(fonte)):
        # `proposal.target.get("expected_reduction")`
        if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)):
            continue
        if no.func.attr != "get" or not isinstance(no.func.value, ast.Attribute):
            continue
        if no.func.value.attr != "target":
            continue
        if no.args and isinstance(no.args[0], ast.Constant):
            lidas.add(no.args[0].value)

    declaradas = {chave for chave, _ in contextual_estimation._TARGET.values()}

    assert lidas, "a varredura não encontrou leitura de target — o teste cegou"
    assert lidas <= declaradas, (
        f"chave lida de target e não declarada em `_TARGET`: {sorted(lidas - declaradas)}"
    )


def test_the_target_a_method_declares_is_the_one_the_engine_enforces():
    """Declarar o alvo errado é pior que não declarar: manda a IA errar."""
    from julius.knowledge.contextual_estimation import (
        allowed_methods,
        target_parameter,
    )

    assert target_parameter("glue_shuffle_reduction_v1")[0] == "expected_reduction"
    assert (
        target_parameter("glue_interactive_capacity_reduction_v1")[0] == "target_dpu"
    )
    # Os demais resolvem o cenário pelo inventário e recebem `target` vazio.
    sem_alvo = {
        method
        for method in allowed_methods().values()
        if target_parameter(method) is None
    }
    assert sem_alvo == {
        "sagemaker_managed_spot_training_v1",
        "sagemaker_gpu_to_cpu_instance_v1",
        "sfn_standard_to_express_v1",
    }
