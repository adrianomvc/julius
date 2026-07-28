"""Análise é para processo que repete; o que não repete não se ajusta.

Dimensionar worker, ligar Auto Scaling, apertar timeout — toda recomendação de
ajuste parte de um comportamento observado várias vezes. Sobre uma execução
única não existe média nem perfil: existe uma amostra, e recomendar a partir
dela é opinião com cara de número.

O que este arquivo cobra, além da supressão, é o contrário dela: o job caro que
não repete **não pode sumir**. Ele vira sinal — hipótese sem economia atribuída
e fora do ranking. Suprimir a análise sem suprimir o dinheiro junto é o ponto
inteiro do desenho, e sem o teste do sinal a regra viraria uma forma elegante de
perder achado caro.
"""

from __future__ import annotations

import pytest

from julius.collection.models import Account, GlueJob, StateMachine
from julius.collection.settings import ANALYSIS_WINDOW_DAYS
from julius.config import DEFAULT_CONFIG
from julius.knowledge.recurrence import (
    NON_RECURRING_DPU_HOURS_MIN,
    is_recurrent,
    runs_per_month,
)
from julius.pipeline import analyze_account

_MINIMO = DEFAULT_CONFIG.thresholds.recurring_runs_min


def _job(nome: str, *, runs: int, dpu_seconds: float = 0.0, **extras) -> GlueJob:
    """Um job com todo defeito que as regras de capacidade procuram."""
    padrao = dict(
        worker_type="G.2X",
        number_of_workers=20,
        auto_scaling=False,
        job_bookmark=False,
        glue_version="2.0",
        avg_cpu_load=0.05,
        timeout_min=2880,
        avg_execution_sec=300,
        coverage_days=ANALYSIS_WINDOW_DAYS,
        window_days=ANALYSIS_WINDOW_DAYS,
    )
    padrao.update(extras)
    return GlueJob(
        name=nome,
        runs_in_window=runs,
        observed_runs=runs,
        dpu_seconds_window=dpu_seconds,
        **padrao,
    )


# --------------------------------------------------------------------------
# A conta de execuções por mês
# --------------------------------------------------------------------------


def test_runs_are_normalized_by_the_observed_window():
    """Sem normalizar, mudar `--lookback-days` mudaria quem é recorrente."""
    quinzena = GlueJob(name="j", runs_in_window=3, window_days=15)

    assert runs_per_month(quinzena) == pytest.approx(6.09, abs=0.01)
    assert is_recurrent(quinzena, _MINIMO) is True


def test_a_state_machine_reports_its_own_monthly_rate():
    """A máquina de estado já entrega execuções/mês; não recalcular."""
    assert runs_per_month(StateMachine(name="sm", executions_per_month=2)) == 2.0
    assert is_recurrent(StateMachine(name="sm", executions_per_month=2), _MINIMO) is False
    assert is_recurrent(StateMachine(name="sm", executions_per_month=9), _MINIMO) is True


def test_a_process_that_never_ran_is_not_recurrent():
    assert is_recurrent(GlueJob(name="j", runs_in_window=0), _MINIMO) is False


# --------------------------------------------------------------------------
# A supressão
# --------------------------------------------------------------------------


def _analise(*jobs: GlueJob):
    return analyze_account(
        Account(account_id="123456789012", glue_jobs=list(jobs)),
        DEFAULT_CONFIG,
        scan_id="scan-teste",
    )


def test_a_sporadic_process_produces_no_finding():
    analise = _analise(_job("mensal", runs=2))

    assert [o for o in analise.opportunities if o.asset_name == "mensal"] == []


def test_the_same_defects_do_produce_findings_when_it_repeats():
    """O contraste: sem ele, o teste acima passaria com a regra quebrada."""
    analise = _analise(_job("diario", runs=30))

    assert [o for o in analise.opportunities if o.asset_name == "diario"]


def test_suppressing_one_process_does_not_touch_the_others():
    analise = _analise(_job("mensal", runs=2), _job("diario", runs=30))

    nomes = {o.asset_name for o in analise.opportunities}
    assert "mensal" not in nomes
    assert "diario" in nomes


# --------------------------------------------------------------------------
# O que é caro não some junto
# --------------------------------------------------------------------------


def test_an_expensive_sporadic_process_becomes_a_signal():
    """Duas execuções por mês queimando DPU-hora é dinheiro, não silêncio."""
    caro = _job("mensal-caro", runs=2, dpu_seconds=3600 * 600)

    analise = _analise(caro)

    sinal = next(
        s for s in analise.signals if s.rule_id == "PROCESS-NON-RECURRING-COST"
    )
    assert sinal.asset_name == "mensal-caro"
    assert sinal.asset_type == "glue_job"
    assert "execuções/mês" in sinal.observation
    assert "DPU-hora" in sinal.observation
    # Sinal não carrega economia — a classe não tem o campo, por desenho.
    assert not hasattr(sinal, "estimated_saving")


def test_a_cheap_sporadic_process_becomes_nothing():
    """Abaixo do piso, apagar o processo inteiro não paga a atenção de ninguém."""
    barato = _job("mensal-barato", runs=2, dpu_seconds=3600 * 1)

    analise = _analise(barato)

    assert [s for s in analise.signals if s.rule_id == "PROCESS-NON-RECURRING-COST"] == []
    assert [o for o in analise.opportunities if o.asset_name == "mensal-barato"] == []


def test_the_expensive_threshold_is_the_declared_one():
    """O piso é DPU-hora, não moeda: região e câmbio não decidem escopo."""
    no_limiar = _job("no-limiar", runs=2, dpu_seconds=3600 * NON_RECURRING_DPU_HOURS_MIN)
    abaixo = _job("abaixo", runs=2, dpu_seconds=3600 * (NON_RECURRING_DPU_HOURS_MIN - 1))

    analise = _analise(no_limiar, abaixo)

    sinalizados = {
        s.asset_name
        for s in analise.signals
        if s.rule_id == "PROCESS-NON-RECURRING-COST"
    }
    assert sinalizados == {"no-limiar"}


def test_a_recurrent_process_never_becomes_this_signal():
    analise = _analise(_job("diario", runs=30, dpu_seconds=3600 * 600))

    assert [s for s in analise.signals if s.rule_id == "PROCESS-NON-RECURRING-COST"] == []
