"""O sinal precisa chegar ao usuário como tarefa, e continuar fora do dinheiro.

"Sinal", "hipótese" e `potential_range` são vocabulário do motor. O que chega ao
relatório era uma observação — *"UDF Python por linha"* — que não diz a ninguém o que
fazer. Ela vira ação quando carrega o próximo passo e o que custa dar esse passo.

O que **não** pode mudar junto: a faixa não é economia. Um sinal que ganha lugar no
ranking e, de tabela, entra em `identified_monthly` teria trocado um problema de
leitura por um de contabilidade.
"""

from __future__ import annotations

from julius.config import DEFAULT_CONFIG
from julius.findings.signal import PotentialRange, Signal
from julius.knowledge.remediation import classify
from julius.scoring.priority import investigation_priority, investigation_ranking_key


def _sinal(rule_id: str = "GLUE-CODE-SHUFFLE", **kwargs) -> Signal:
    base = {
        "kind": "code",
        "rule_id": rule_id,
        "asset_type": "glue_job",
        "asset_name": "etl",
        "observation": "join sem chave revisada",
        "question": "o padrão custa capacidade neste job?",
        "missing_evidence": ["benchmark A/B com mesmo input"],
    }
    return Signal(**{**base, **kwargs})


def _faixa(expected: float) -> PotentialRange:
    return PotentialRange(
        low=expected * 0.6,
        expected=expected,
        high=expected * 1.4,
        basis="DPU-hora da janela",
        caveat="fração típica do padrão; nenhuma medição sustenta o valor",
    )


def test_the_next_step_comes_from_what_is_missing():
    sinal = _sinal(missing_evidence=["pico de memória do driver", "script completo"])
    assert sinal.next_action == (
        "Levantar: pico de memória do driver; script completo"
    )


def test_the_next_step_is_never_to_fix_it():
    """Agir sobre um sinal antes de respondê-lo é o que o tipo existe para evitar."""
    for sinal in (_sinal(), _sinal(missing_evidence=[])):
        assert "corrigir" not in sinal.next_action.lower()
        assert "aplicar" not in sinal.next_action.lower()


def test_a_signal_without_missing_evidence_still_says_what_to_do():
    sinal = _sinal(missing_evidence=[])
    assert sinal.next_action == "Julgar o padrão contra o artefato completo"


def test_classification_fills_family_and_measurement_effort():
    sinal = classify([_sinal()])[0]
    assert sinal.remediation_family == "shuffle_partitioning"
    assert sinal.measurement_effort == 3


def test_an_unclassified_signal_is_expensive_by_default():
    """Sinal sem família não pode competir por atenção com o que já foi medido."""
    sinal = classify([_sinal(rule_id="REGRA-INEXISTENTE")])[0]
    assert sinal.remediation_family == ""
    assert sinal.measurement_effort == 5


def test_cheap_to_measure_wins_when_neither_has_a_range():
    """Sem faixa, o único critério honesto é o custo de descobrir.

    E é o certo: medir barato é justamente o que produz a faixa que falta.
    """
    barato = classify([_sinal(rule_id="SFN-RETRY-MASKING")])[0]
    caro = classify([_sinal(rule_id="SM-CODE-NO-CHECKPOINT")])[0]
    assert barato.measurement_effort < caro.measurement_effort

    ordenados = sorted(
        [caro, barato],
        key=lambda item: investigation_ranking_key(item, DEFAULT_CONFIG),
        reverse=True,
    )
    assert ordenados[0] is barato


def test_a_signal_with_a_range_comes_before_one_without():
    com = classify([_sinal(potential_range=_faixa(400.0))])[0]
    sem = classify([_sinal(rule_id="GLUE-CODE-PYTHON-UDF")])[0]
    ordenados = sorted(
        [sem, com],
        key=lambda item: investigation_ranking_key(item, DEFAULT_CONFIG),
        reverse=True,
    )
    assert ordenados[0] is com


def test_effort_divides_the_return():
    """Duas faixas iguais, esforços diferentes: a mais barata de medir rende mais."""
    barata = classify([_sinal(rule_id="ATHENA-RESULT-REUSE", potential_range=_faixa(300.0))])[0]
    cara = classify([_sinal(rule_id="SM-CODE-NO-CHECKPOINT", potential_range=_faixa(300.0))])[0]
    assert investigation_priority(barata, DEFAULT_CONFIG) > investigation_priority(
        cara, DEFAULT_CONFIG
    )


def test_a_signal_without_a_range_scores_zero_without_disappearing():
    """Zero aqui é "não sei quanto vale", nunca "não vale nada"."""
    sinal = classify([_sinal()])[0]
    assert investigation_priority(sinal, DEFAULT_CONFIG) == 0
    chave = investigation_ranking_key(sinal, DEFAULT_CONFIG)
    assert chave[0] is False and chave[2] == -3


def test_the_round_trip_survives_the_derived_field():
    """`to_dict` publica `next_action`; o construtor não o aceita.

    É o caminho real: `agent prepare` grava o pacote e `agent validate` o lê de volta.
    """
    original = classify([_sinal(potential_range=_faixa(250.0))])[0]
    voltou = Signal.from_dict(original.to_dict())
    assert voltou == original


def test_the_round_trip_rebuilds_the_range_as_a_range():
    """Um `potential_range` que volta como dicionário some do topo da lista."""
    original = classify([_sinal(potential_range=_faixa(250.0))])[0]
    voltou = Signal.from_dict(original.to_dict())
    assert isinstance(voltou.potential_range, PotentialRange)
    assert voltou.potential_range.expected == 250.0


def test_the_action_never_reaches_the_financial_total():
    """A fronteira que a Fase 1 não pode ter movido."""
    from julius.pipeline import analyze

    analise = analyze("data/sample/consumer-avi.json")
    assert analise.signals, "o sample precisa produzir sinal para este teste valer"
    for sinal in analise.signals:
        assert not hasattr(sinal, "estimated_gain")
        assert not hasattr(sinal, "include_in_portfolio")
    identificada = sum(
        item.portfolio_gain.monthly_expected
        for item in analise.opportunities
        if item.include_in_portfolio
    )
    assert identificada == analise.kpis.identified_monthly
