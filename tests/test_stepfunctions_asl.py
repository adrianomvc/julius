"""A ASL como fonte financeira, e o veredito como fato de entrada da regra.

Três coisas são verificadas aqui, e a terceira é a que fecha um ciclo que estava
aberto desde que a regra de Express foi escrita:

1. a travessia alcança estado aninhado — um `Task` dentro de `Map` é cobrado
   como qualquer outro, e uma varredura que só olha o topo perde justamente a
   parte que multiplica transições;
2. o que a definição prova sozinha vira pergunta, não cifra;
3. o veredito contextual sobre idempotência volta para o inventário e a
   migração Standard→Express passa a poder disparar.
"""

from __future__ import annotations

from julius.collection.asl import express_blockers, named_states, scan_patterns
from julius.collection.models import Account, StateMachine
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.stepfunctions import rules as stepfunctions_rules
from julius.knowledge.verdict_facts import apply_verdicts


class _Decisao:
    """O mínimo do `SignalDecision` que `apply_verdicts` lê.

    `evidence_hash` entra aqui porque o `SignalDecision` real sempre o tem —
    `SignalLedger.record_verdicts` grava `signal.evidence_signature()` em toda
    linha. Sem ele o fato seria escrito sem nada que permita invalidá-lo quando o
    artefato mudar, e é isso que `apply_confirmed` passou a recusar.
    """

    def __init__(
        self,
        rule_id: str,
        asset_name: str,
        verdict: str,
        evidence_hash: str = "a1b2c3d4",
    ) -> None:
        self.rule_id = rule_id
        self.asset_name = asset_name
        self.verdict = verdict
        self.evidence_hash = evidence_hash


def _maquina(**overrides) -> StateMachine:
    defaults = dict(
        name="orquestra",
        type="STANDARD",
        executions_per_month=45000,
        avg_duration_sec=90.0,
        avg_state_transitions=12,
        observed_runs=45000,
        coverage_days=30,
        sampled_executions=20,
    )
    defaults.update(overrides)
    return StateMachine(**defaults)


def _conta(machine: StateMachine) -> Account:
    return Account(account_id="123456789012", state_machines=[machine])


def test_the_walker_reaches_a_state_nested_inside_a_map():
    """Estado dentro de `ItemProcessor` é cobrado como qualquer outro.

    `_polling_loop_states` só examina o nível de topo, e é por isso que este
    teste existe: a varredura de padrões não pode herdar a mesma cegueira.
    """
    definition = {
        "States": {
            "ProcessAll": {
                "Type": "Map",
                "MaxConcurrency": 4,
                "ItemProcessor": {
                    "States": {
                        "Notify": {"Type": "Task", "Resource": "arn:aws:states:::sns:publish"}
                    }
                },
            }
        }
    }

    nomes = [nome for nome, _ in named_states(definition)]
    assert "ProcessAll" in nomes
    assert "ProcessAll.Notify" in nomes

    padroes = scan_patterns(definition)
    assert padroes["external_side_effect"] == ["ProcessAll.Notify"]


def test_a_side_effect_with_a_dedup_key_is_not_flagged():
    """Escrita condicional não duplica quando repetida — e a ASL prova isso."""
    definition = {
        "States": {
            "Grava": {
                "Type": "Task",
                "Resource": "arn:aws:states:::dynamodb:putItem",
                "Parameters": {"ConditionExpression": "attribute_not_exists(pk)"},
            },
            "GravaSemGuarda": {
                "Type": "Task",
                "Resource": "arn:aws:states:::dynamodb:putItem",
                "Parameters": {"TableName": "pedidos"},
            },
        }
    }

    assert scan_patterns(definition)["external_side_effect"] == ["GravaSemGuarda"]


def test_retry_without_backoff_is_the_pattern_not_retry_itself():
    """Retry é resiliência; retry colado repete o trabalho já cobrado."""
    espacado = {
        "States": {
            "Chama": {"Type": "Task", "Retry": [{"MaxAttempts": 8, "BackoffRate": 2.0}]}
        }
    }
    colado = {
        "States": {
            "Chama": {"Type": "Task", "Retry": [{"MaxAttempts": 8, "BackoffRate": 1.0}]}
        }
    }

    assert "retry_unbounded" not in scan_patterns(espacado)
    assert scan_patterns(colado)["retry_unbounded"] == ["Chama"]


def test_a_catch_that_lands_on_succeed_turns_failure_into_success():
    definition = {
        "States": {
            "Trabalha": {
                "Type": "Task",
                "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "Fim"}],
            },
            "Fim": {"Type": "Succeed"},
        }
    }

    assert scan_patterns(definition)["catch_swallow"] == ["Trabalha"]


def test_express_blockers_come_from_the_definition_not_from_judgement():
    """`.sync`, callback e timeout longo: a API do Express recusa os três."""
    definition = {
        "States": {
            "RodaJob": {
                "Type": "Task",
                "Resource": "arn:aws:states:::glue:startJobRun.sync",
            },
            "Espera": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
            },
            "Longa": {"Type": "Task", "TimeoutSeconds": 900},
        }
    }

    motivos = express_blockers(definition)

    assert any(".sync" in motivo for motivo in motivos)
    assert any("waitfortasktoken" in motivo for motivo in motivos)
    assert any("900" in motivo for motivo in motivos)
    assert express_blockers({"States": {"Rapida": {"Type": "Pass"}}}) == []


def test_a_machine_express_cannot_run_is_never_offered_the_migration():
    """Perguntar sobre uma migração impossível gasta a análise e a confiança."""
    machine = _maquina(
        idempotent=True,
        express_benchmark_duration_ms=800,
        express_benchmark_memory_mb=128,
        express_blockers=["RodaJob usa .sync"],
    )
    account = _conta(machine)

    found = stepfunctions_rules.detect(account, DEFAULT_CONFIG, "scan")
    signals = stepfunctions_rules.signals(account, DEFAULT_CONFIG)

    assert not any(item.rule_id == "SFN-STANDARD-TO-EXPRESS" for item in found)
    assert not any(item.rule_id == "SFN-STANDARD-TO-EXPRESS" for item in signals)


def test_the_idempotency_question_names_the_states_it_is_about():
    """A pergunta existia e era feita no vácuo: agora aponta onde olhar."""
    machine = _maquina(
        idempotent=None,
        asl_patterns={"external_side_effect": ["Notifica", "Cobra"]},
    )

    signals = stepfunctions_rules.signals(_conta(machine), DEFAULT_CONFIG)
    express = next(s for s in signals if s.rule_id == "SFN-STANDARD-TO-EXPRESS")

    assert "Notifica" in express.observation
    assert "Cobra" in express.observation


def test_a_confirmed_verdict_unlocks_the_express_opportunity():
    """O ciclo inteiro: pergunta, veredito, fato no inventário, cifra.

    Antes deste caminho, `idempotent` nunca era preenchido por ninguém e a
    regra de maior ganho unitário do Step Functions não disparava em conta
    real. Ela não estava errada — estava desligada.
    """
    machine = _maquina(
        idempotent=None,
        express_benchmark_duration_ms=800,
        express_benchmark_memory_mb=128,
    )
    account = _conta(machine)

    assert not any(
        item.rule_id == "SFN-STANDARD-TO-EXPRESS"
        for item in stepfunctions_rules.detect(account, DEFAULT_CONFIG, "scan")
    )

    aplicados = apply_verdicts(
        account, [_Decisao("SFN-STANDARD-TO-EXPRESS", "orquestra", "confirmed")]
    )

    assert aplicados and machine.idempotent is True
    express = next(
        item
        for item in stepfunctions_rules.detect(account, DEFAULT_CONFIG, "scan")
        if item.rule_id == "SFN-STANDARD-TO-EXPRESS"
    )
    assert express.estimated_gain.monthly_expected > 0


def test_a_rejected_verdict_does_not_decide_the_opposite():
    """"Não migre" chega com o mesmo rótulo de "não vale o esforço".

    Derivar `idempotent = False` de um "não" ambíguo seria inventar um fato a
    partir de um silêncio — e o livro de sinais já silencia a pergunta sozinho.
    """
    machine = _maquina(idempotent=None)
    account = _conta(machine)

    apply_verdicts(
        account, [_Decisao("SFN-STANDARD-TO-EXPRESS", "orquestra", "rejected")]
    )

    assert machine.idempotent is None


def test_asl_signals_do_not_repeat_a_question_another_rule_already_asks():
    """Veredito custa: o validador exige resposta para todo sinal do pacote."""
    com_loop_contado = _maquina(
        has_polling_loop=True,
        asl_patterns={"manual_polling": ["RodaJob"]},
    )
    sem_loop_contado = _maquina(
        has_polling_loop=False,
        asl_patterns={"manual_polling": ["RodaJob"]},
    )

    def ids(machine: StateMachine) -> set[str]:
        return {
            item.rule_id
            for item in stepfunctions_rules.signals(_conta(machine), DEFAULT_CONFIG)
        }

    assert "SFN-ASL-MANUAL-POLLING" not in ids(com_loop_contado)
    assert "SFN-ASL-MANUAL-POLLING" in ids(sem_loop_contado)


def test_a_timeout_burns_transitions_exactly_like_a_failure():
    """A regra contava só `FAILED` e subestimava a própria cifra."""
    so_falha = _maquina(failed_executions=100, avg_failed_state_transitions=4)
    com_timeout = _maquina(
        failed_executions=100,
        timed_out_executions=40,
        aborted_executions=10,
        avg_failed_state_transitions=4,
    )

    def custo(machine: StateMachine) -> float:
        found = stepfunctions_rules.detect(_conta(machine), DEFAULT_CONFIG, "scan")
        item = next(i for i in found if i.rule_id == "SFN-FAILED-TRANSITION-COST")
        return item.estimated_gain.monthly_expected

    assert custo(com_timeout) > custo(so_falha)

    achado = next(
        item
        for item in stepfunctions_rules.detect(_conta(com_timeout), DEFAULT_CONFIG, "scan")
        if item.rule_id == "SFN-FAILED-TRANSITION-COST"
    )
    # Um timeout não se corrige como uma falha: a composição precisa aparecer.
    assert "por timeout" in achado.why
    assert "abortadas" in achado.why


def test_a_tail_above_five_minutes_blocks_express_even_with_a_short_average():
    """Express mata a execução aos cinco minutos — não a degrada, ela falha.

    Uma máquina com média de 90s e p95 de 400s tem uma fatia de execuções que a
    migração quebraria. Barrar por média deixaria passar exatamente esse caso, e
    o estrago não seria de custo: seria de execução perdida.
    """
    from julius.collection.collectors.stepfunctions import (
        _apply_measured_express_blocker,
    )

    curta = _maquina(duration_p95_ms=120_000)
    com_cauda = _maquina(duration_p95_ms=400_000)

    _apply_measured_express_blocker([curta, com_cauda])

    assert curta.express_blockers == []
    assert any("p95" in motivo for motivo in com_cauda.express_blockers)

    # E o bloqueio medido tem o mesmo efeito do declarado: nada é oferecido.
    com_cauda.idempotent = True
    com_cauda.express_benchmark_duration_ms = 800
    com_cauda.express_benchmark_memory_mb = 128
    found = stepfunctions_rules.detect(_conta(com_cauda), DEFAULT_CONFIG, "scan")
    assert not any(item.rule_id == "SFN-STANDARD-TO-EXPRESS" for item in found)


def test_a_machine_without_the_metric_is_not_blocked_by_missing_data():
    """`None` é "não medido", não "acima do teto"."""
    from julius.collection.collectors.stepfunctions import (
        _apply_measured_express_blocker,
    )

    sem_metrica = _maquina(duration_p95_ms=None)

    _apply_measured_express_blocker([sem_metrica])

    assert sem_metrica.express_blockers == []


def test_a_wait_loop_inside_a_map_is_finally_detected():
    """O caso mais caro do padrão era o único que a varredura não via.

    Um `Map` que processa mil itens e espera dentro do `ItemProcessor` cobra o
    ciclo mil vezes. `_polling_loop_states` só olhava o nível de topo, então
    essa máquina saía com `has_polling_loop=False` e a regra que cobra por
    transição de espera não disparava.
    """
    from julius.collection.collectors.stepfunctions import _polling_loop_states

    aninhado = {
        "States": {
            "ProcessaTudo": {
                "Type": "Map",
                "ItemProcessor": {
                    "States": {
                        "Dispara": {"Type": "Task", "Next": "Espera"},
                        "Espera": {"Type": "Wait", "Seconds": 30, "Next": "Confere"},
                        "Confere": {"Type": "Task", "Next": "Espera"},
                    }
                },
            }
        }
    }

    assert _polling_loop_states(aninhado) == {"Espera", "Confere"}


def test_next_is_never_followed_across_scopes():
    """`Next` endereça só o próprio mapa: achatar criaria um ciclo inexistente.

    Os dois ramos têm um `Confere` cada. Se os escopos fossem unidos, o `Next`
    de um ramo alcançaria o estado homônimo do outro e fecharia um ciclo que a
    máquina não tem.
    """
    from julius.collection.collectors.stepfunctions import _polling_loop_states

    dois_ramos = {
        "States": {
            "EmParalelo": {
                "Type": "Parallel",
                "Branches": [
                    {"States": {"Espera": {"Type": "Wait", "Next": "Confere"},
                                "Confere": {"Type": "Task"}}},
                    {"States": {"Confere": {"Type": "Task", "Next": "Espera"},
                                "Espera": {"Type": "Wait"}}},
                ],
            }
        }
    }

    # Nenhum dos dois ramos fecha o ciclo sozinho, e cruzá-los seria invenção.
    assert _polling_loop_states(dois_ramos) == set()


def test_the_top_level_loop_keeps_working():
    """A correção amplia a cobertura; não pode trocar o caso que já funcionava."""
    from julius.collection.collectors.stepfunctions import _polling_loop_states

    topo = {
        "States": {
            "Espera": {"Type": "Wait", "Seconds": 10, "Next": "Confere"},
            "Confere": {"Type": "Task", "Next": "Espera"},
        }
    }

    assert _polling_loop_states(topo) == {"Espera", "Confere"}
