"""Zero não é medição ausente, em dois achados de Glue.

Os dois emitiam `US$ 0,00` onde a resposta certa era "não dá para estimar". A
diferença importa porque zero se lê como "não compensa" — e o time ignora o
achado — enquanto "não mensurável" convida a medir.

Nenhum dos dois passa a entrar no portfólio: economia exige frequência ou
consumo observado, e nenhum dos dois tem.
"""

from __future__ import annotations

from julius.collection.models import GlueJob
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.glue.estimation import (
    python_shell_migration_saving,
    timeout_guardrail_saving,
)


def _job(**extra) -> GlueJob:
    base = {
        "name": "processa_interacoes",
        "worker_type": "G.1X",
        "number_of_workers": 10,
        "timeout_min": 2880,
        "p95_execution_sec": 3600,
        "avg_execution_sec": 3600,
        "window_days": 30,
        "coverage_days": 30,
    }
    base.update(extra)
    return GlueJob(**base)


# --------------------------------------------------------------------------
# Timeout muito acima da duração
# --------------------------------------------------------------------------


def test_without_an_observed_timeout_the_exposure_replaces_the_zero():
    """Uma trava custaria X; quantas travas haverá é o que não se sabe."""
    job = _job()

    est = timeout_guardrail_saving(job, DEFAULT_CONFIG)

    assert est.estimated_saving == 0.0
    assert est.saving_quality == "unavailable"
    assert est.is_strategic is True
    # 2880 − 60 min = 2820 min de desperdício, 10 workers G.1X = 10 DPU.
    assert est.exposure_cost == round(2820 / 60 * 10 * DEFAULT_CONFIG.pricing.glue_dpu_hour, 2)
    assert any("uma trava até o timeout custaria" in a for a in est.assumptions)
    assert any("sem frequência observada" in a for a in est.assumptions)


def test_the_exposure_grows_with_the_gap_and_the_capacity():
    """É a fórmula do estrago: minutos ociosos × DPU × tarifa."""
    curto = timeout_guardrail_saving(_job(timeout_min=120), DEFAULT_CONFIG)
    longo = timeout_guardrail_saving(_job(timeout_min=2880), DEFAULT_CONFIG)
    largo = timeout_guardrail_saving(
        _job(timeout_min=2880, number_of_workers=40), DEFAULT_CONFIG
    )

    assert curto.exposure_cost < longo.exposure_cost < largo.exposure_cost


def test_a_timeout_that_actually_happened_still_produces_a_saving():
    """A exposição não substitui a medição quando ela existe."""
    job = _job(
        failure_categories={"timeout": 2},
        failed_runs_in_window=2,
        estimated_failed_dpu_hours_window=20.0,
    )

    est = timeout_guardrail_saving(job, DEFAULT_CONFIG)

    assert est.estimated_saving > 0
    assert est.saving_quality != "unavailable"
    assert est.exposure_cost is None


def test_a_timeout_no_higher_than_the_duration_has_no_exposure():
    job = _job(timeout_min=60)

    assert timeout_guardrail_saving(job, DEFAULT_CONFIG).exposure_cost == 0.0


# --------------------------------------------------------------------------
# Migração para Python Shell
# --------------------------------------------------------------------------


def test_a_job_that_did_not_run_cannot_have_its_migration_estimated():
    """Baseline é DPU-hora da janela × tarifa: sem execução, é zero.

    Emitir economia zero se lê como "não compensa migrar", quando o correto é
    "não dá para estimar nesta janela".
    """
    job = _job(runs_in_window=0, last_run_at="2026-03-15T10:00:00+00:00")

    est = python_shell_migration_saving(job, DEFAULT_CONFIG)

    assert est.saving_quality == "unavailable"
    assert est.is_strategic is True
    assert any("nenhuma execução na janela" in a for a in est.assumptions)
    assert any("2026-03-15" in a for a in est.assumptions)
    assert any("desligamento" in a for a in est.assumptions)


def test_a_job_never_seen_running_says_so():
    job = _job(runs_in_window=0, last_run_at="")

    est = python_shell_migration_saving(job, DEFAULT_CONFIG)

    assert any("nenhuma execução conhecida" in a for a in est.assumptions)


def test_a_job_that_ran_keeps_producing_the_migration_estimate():
    job = _job(runs_in_window=20, estimated_dpu_hours_window=200.0)

    est = python_shell_migration_saving(job, DEFAULT_CONFIG)

    assert est.baseline_cost > 0
    assert est.estimated_saving > 0
    assert est.saving_quality != "unavailable"
