"""A terceira pergunta: quanto ainda não sei, e quem descobre.

Um número único — "potencial em investigação: US$ 3.100" — chega ao usuário como
conta a pagar. Boa parte não custa nada ao time dele: falta uma fonte que o Julius
sabe ler e não leu, e o scan seguinte responde sozinho quando a permissão existir.

O teste que fecha o ciclo é o do `blocked_source`: a fonte que voltou com lacuna já
gravou na saúde da coleta o próximo passo, e essa frase é a resposta. A informação
existia nos dois lados e ninguém as juntou.
"""

from __future__ import annotations

from julius.collection.models import Account, CollectionHealth
from julius.findings.signal import PotentialRange, Signal
from julius.knowledge.remediation import (
    _UNBLOCKING_BY_PREFIX,
    UNBLOCKING_SOURCES,
    classify,
    unblocking_sources,
)
from julius.reporting import pending


def _conta(saude: list[CollectionHealth] | None = None) -> Account:
    return Account(account_id="123456789012", collection_health=saude or [])


def _sinal(rule_id: str, asset_type: str = "glue_job", **kwargs) -> Signal:
    base = {
        "kind": "code",
        "rule_id": rule_id,
        "asset_type": asset_type,
        "asset_name": "etl",
        "observation": "",
        "question": "",
        "missing_evidence": ["benchmark A/B"],
    }
    return classify([Signal(**{**base, **kwargs})])[0]


def test_every_declared_source_exists_in_the_collector():
    """Nome de fonte errado nunca casaria com a saúde, e a linha sumiria calada."""
    from julius.collection.sources import SOURCES

    reais = {source.name for source in SOURCES}
    declaradas = {
        nome
        for fontes in UNBLOCKING_SOURCES.values()
        for nome in fontes
    } | {nome for _, fontes in _UNBLOCKING_BY_PREFIX for nome in fontes}
    assert declaradas <= reais, f"fonte inexistente: {sorted(declaradas - reais)}"


def test_asset_types_that_subdivide_are_matched_by_prefix():
    assert unblocking_sources("sagemaker_endpoint") == unblocking_sources(
        "sagemaker_training_job"
    )
    assert unblocking_sources("s3_prefix") == unblocking_sources("s3_bucket")
    assert unblocking_sources("tipo_desconhecido") == ()


def test_a_collection_gap_makes_it_the_cheapest_next_step():
    """Oferecer o benchmark antes da permissão IAM é mandar alguém medir o que o
    scan seguinte mediria de graça."""
    conta = _conta(
        [
            CollectionHealth(
                source="Spark Event Logs",
                status="unavailable",
                next_action="validar s3:GetObject e o --spark-event-logs-path dos jobs",
            )
        ]
    )
    resumo = pending.build(conta, [_sinal("GLUE-CODE-SHUFFLE")])
    item = resumo.items[0]
    assert item.unblocked_by == "coleta"
    assert item.blocked_source == "Spark Event Logs"
    assert "s3:GetObject" in item.source_next_action


def test_without_a_gap_the_family_owner_decides():
    resumo = pending.build(_conta(), [_sinal("GLUE-CODE-SHUFFLE", kind="config")])
    assert resumo.items[0].unblocked_by == "time"


def test_a_code_signal_can_be_settled_by_reading_it():
    """Descartar sem medir é metade do valor, e não custa sprint de ninguém."""
    resumo = pending.build(_conta(), [_sinal("GLUE-CODE-SHUFFLE", kind="code")])
    assert resumo.items[0].unblocked_by == "analise"


def test_a_healthy_source_does_not_become_a_gap():
    conta = _conta(
        [CollectionHealth(source="Spark Event Logs", status="ok", next_action="x")]
    )
    resumo = pending.build(conta, [_sinal("GLUE-CODE-SHUFFLE", kind="config")])
    assert resumo.items[0].blocked_source == ""


def test_not_applicable_is_not_a_gap():
    """A fonte fora do perfil não deve nada: ela não se aplica àquela conta."""
    conta = _conta(
        [CollectionHealth(source="Spark Event Logs", status="not_applicable")]
    )
    resumo = pending.build(conta, [_sinal("GLUE-CODE-SHUFFLE", kind="config")])
    assert resumo.items[0].unblocked_by == "time"


def test_the_ceiling_sums_only_the_ranges_that_exist():
    faixa = PotentialRange(
        low=60.0, expected=100.0, high=140.0, basis="b", caveat="c", baseline=1000.0
    )
    resumo = pending.build(
        _conta(),
        [
            _sinal("GLUE-CODE-SHUFFLE", potential_range=faixa),
            _sinal("GLUE-CODE-PYTHON-UDF"),
        ],
    )
    assert resumo.ceiling == 100.0


def test_the_sentence_says_it_may_already_be_counted():
    """O teto não soma com a economia identificada — as duas saem do mesmo custo."""
    resumo = pending.build(_conta(), [_sinal("GLUE-CODE-SHUFFLE")])
    assert "pode já estar" in resumo.sentence


def test_an_empty_account_says_so_instead_of_showing_zero():
    assert "Nenhuma medição pendente" in pending.build(_conta(), []).sentence


def test_the_report_carries_the_block():
    from julius.pipeline import analyze

    analise = analyze("data/sample/consumer-avi.json")
    bloco = analise.vm.pending
    assert bloco["items"], "o sample precisa produzir medição pendente"
    assert set(bloco) == {
        "sentence",
        "ceiling",
        "by_owner",
        "count_by_owner",
        "items",
    }
    assert sum(bloco["count_by_owner"].values()) == len(bloco["items"])


def test_the_json_publishes_it_next_to_the_signals():
    import json

    from julius.pipeline import analyze
    from julius.reporting.renderer import render_json

    analise = analyze("data/sample/consumer-avi.json")
    payload = json.loads(render_json(analise.vm, analise.opportunities))
    assert payload["pending_measurements"]["items"]
    # E continua fora do total financeiro, que é a fronteira inteira desta fase.
    assert "pending" not in payload["summary"]
