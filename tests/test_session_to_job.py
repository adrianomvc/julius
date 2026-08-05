"""Sessão que roda como job, pagando preço de sessão.

Uma sessão cobra o tempo READY inteiro — inclusive o intervalo entre um statement
e o seguinte, que é tempo de gente pensando. Um job cobra a execução. Sessão que
reabre todo dia com o mesmo trabalho é um job que ninguém escreveu.

**Por que sinal e não achado.** A parte medível do desperdício de uma sessão já é
reivindicada por `GLUE-IS-IDLE-TIMEOUT`, com cifra e contrafactual. Migrar para job
cobre esse mesmo dinheiro e mais um pouco: as duas ações são alternativas, e emitir
as duas com economia faria o portfólio somar a mesma sessão duas vezes.

O que sobra é a pergunta que nenhuma métrica responde — o trabalho é batch ou
exploração? — e essa é a definição de sinal neste produto.
"""

from __future__ import annotations

from julius.collection.models import Account
from julius.collection.models.glue import InteractiveSession
from julius.config import DEFAULT_CONFIG
from julius.knowledge.remediation import CATALOG
from julius.knowledge.rules.glue import sessions


def _sessao(**overrides) -> InteractiveSession:
    base = {
        "session_id": "sessao-1",
        "dpu": 5.0,
        "idle_timeout_min": 60,
        "status": "READY",
        "observed_runs": 12,
        "statement_ids": ["s1", "s2", "s3"],
        "allocated_cost": 400.0,
    }
    return InteractiveSession(**{**base, **overrides})


def _sinais(*sessoes) -> list:
    conta = Account(account_id="123456789012", interactive_sessions=list(sessoes))
    return [
        item
        for item in sessions.signals(conta, DEFAULT_CONFIG)
        if item.rule_id == "GLUE-IS-TO-JOB"
    ]


def test_a_recurring_session_with_work_raises_the_question():
    sinal = _sinais(_sessao())[0]

    assert sinal.asset_type == "glue_session"
    assert sinal.asset_name == "sessao-1"
    assert "batch" in sinal.question


def test_a_session_nobody_measured_is_left_alone():
    """`observed_runs` vale zero quando não foi medido, e o limiar silencia."""
    assert _sinais(_sessao(observed_runs=0)) == []


def test_the_gate_is_not_active_days_per_month():
    """Esse campo tem default 22 — "dias úteis" —, e uma sessão que ninguém mediu
    passaria por recorrente. Foi o erro que a suíte pegou."""
    assert InteractiveSession.active_days_per_month == 22
    assert _sinais(_sessao(observed_runs=1, active_days_per_month=22)) == []


def test_a_session_without_statements_is_left_alone():
    """Sem statement não há trabalho a migrar; é sessão aberta e esquecida, que
    `GLUE-IS-IDLE-TIMEOUT` já trata."""
    assert _sinais(_sessao(statement_ids=[])) == []


def test_the_range_comes_from_the_allocated_cost():
    faixa = _sinais(_sessao(allocated_cost=400.0))[0].potential_range

    assert faixa is not None
    assert faixa.baseline == 400.0
    assert 0 < faixa.expected < faixa.high <= 400.0


def test_without_an_allocated_cost_there_is_no_range():
    """`None` é resposta melhor que zero: sem custo atribuído não há ordem de
    grandeza a informar, e zero se leria como "não há o que ganhar"."""
    assert _sinais(_sessao(allocated_cost=None))[0].potential_range is None


def test_the_range_never_claims_more_than_the_session_costs():
    faixa = _sinais(_sessao(allocated_cost=100.0))[0].potential_range

    assert faixa.high <= 100.0


def test_the_caveat_says_it_overlaps_with_the_idle_timeout_action():
    """As duas ações são alternativas sobre a mesma sessão, e quem lê precisa
    saber disso antes de somar."""
    faixa = _sinais(_sessao())[0].potential_range

    assert "idle_timeout" in faixa.caveat
    assert "alternativ" in faixa.caveat


def test_it_never_becomes_money():
    """Sinal não recebe economia — é a fronteira que este produto inteiro defende."""
    conta = Account(account_id="123456789012", interactive_sessions=[_sessao()])
    achados = sessions.detect(conta, DEFAULT_CONFIG, "scan")

    assert all(item.rule_id != "GLUE-IS-TO-JOB" for item in achados)


def test_the_rule_is_classified():
    assert CATALOG["GLUE-IS-TO-JOB"] == "runtime_modality"
