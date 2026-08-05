"""Quem lê "Indisponível" precisa ler, na mesma linha, por que e quem destrava.

O relatório mostrava "Indisponível" e enterrava o motivo em
`estimation.assumptions`, que só a planilha exibia — e ainda assim sem dizer quem
resolve. Seis perguntas chegaram na forma *"está sem valor de economia, por quê?"*,
e todas tinham a mesma resposta possível: falta uma medição, e ela vem de uma fonte
que alguém pode conceder.

`portfolio_exclusion_reasons` já dizia o **estado** — `blocked`,
`saving_not_validated` —, que é vocabulário do motor. Estes campos dizem o que
fazer a respeito.
"""

from __future__ import annotations

from julius.collection.models import Account, CollectionHealth, GlueJob
from julius.findings.opportunity import EstimatedGain, Estimation, Opportunity
from julius.reporting.pending import blocked_reason, unblocker_for


def _achado(**overrides) -> Opportunity:
    base = {
        "opportunity_id": "GLUE-OVERPROVISIONED-x",
        "account": "123456789012",
        "asset_type": "glue_job",
        "asset_name": "etl",
        "category": "custo",
        "rule_id": "GLUE-OVERPROVISIONED",
        "finding": "capacidade superdimensionada",
        "recommended_action": "reduzir workers",
        "remediation_family": "capacity_sizing",
        "blocked": True,
        "estimated_gain": EstimatedGain(),
        "estimation": Estimation(
            method="m",
            baseline_cost=1000.0,
            projected_cost=1000.0,
            estimated_saving=0.0,
            assumptions=[
                "mesmo volume processado",
                "economia não quantificada, falta: max_memory_used_pct "
                "(fonte CloudWatch Glue Observability)",
            ],
        ),
    }
    return Opportunity(**{**base, **overrides})


def _conta(saude: list[CollectionHealth] | None = None) -> Account:
    return Account(
        account_id="123456789012",
        collection_health=saude or [],
        glue_jobs=[GlueJob(name="etl")],
    )


def test_a_finding_with_a_figure_has_no_reason():
    """`None` e não string vazia: ter cifra não é um motivo de não ter."""
    achado = _achado(
        blocked=False,
        estimated_gain=EstimatedGain(monthly_expected=500.0),
        estimation=Estimation(
            method="m",
            baseline_cost=1000.0,
            projected_cost=500.0,
            estimated_saving=500.0,
            saving_quality="measured",
        ),
    )

    assert achado.include_in_portfolio is True
    assert blocked_reason(_conta(), achado) is None


def test_the_reason_carries_the_sentence_the_rule_wrote():
    motivo = blocked_reason(_conta(), _achado())

    assert "max_memory_used_pct" in motivo.missing
    assert "CloudWatch Glue Observability" in motivo.missing


def test_a_collection_gap_makes_it_the_cheapest_next_step():
    conta = _conta(
        [
            CollectionHealth(
                source="CloudWatch Glue Observability",
                status="unavailable",
                next_action="habilitar Glue Observability e validar as métricas",
            )
        ]
    )
    motivo = blocked_reason(conta, _achado())

    assert motivo.unblocked_by == "coleta"
    assert motivo.blocked_source == "CloudWatch Glue Observability"
    assert "Observability" in motivo.source_next_action


def test_without_a_gap_the_family_owner_decides():
    """`capacity_sizing` fecha com o time: exige execução controlada."""
    motivo = blocked_reason(_conta(), _achado())

    assert motivo.unblocked_by == "time"
    assert motivo.blocked_source == ""


def test_it_falls_back_to_the_missing_evidence():
    """Regra que não escreveu a premissa ainda tem o que dizer."""
    achado = _achado(
        estimation=Estimation(
            method="m", baseline_cost=0.0, projected_cost=0.0, estimated_saving=0.0
        )
    )
    achado.missing_evidence = ["benchmark A/B com o mesmo volume"]

    assert blocked_reason(_conta(), achado).missing == "benchmark A/B com o mesmo volume"


def test_it_never_pretends_to_know():
    achado = _achado(
        estimation=Estimation(
            method="m", baseline_cost=0.0, projected_cost=0.0, estimated_saving=0.0
        )
    )
    achado.missing_evidence = []

    assert "não declarada" in blocked_reason(_conta(), achado).missing


def test_signal_and_finding_share_the_same_cascade():
    """Duas cópias divergiriam, e a que divergisse ficaria calada — não errada,
    calada, que é pior."""
    conta = _conta(
        [CollectionHealth(source="Spark Event Logs", status="partial", next_action="x")]
    )
    from julius.reporting.pending import _gaps_by_source

    gaps = _gaps_by_source(conta)

    assert unblocker_for("glue_job", "capacity_sizing", gaps) == (
        "coleta",
        "Spark Event Logs",
        "x",
    )


# --- a saída ---------------------------------------------------------------


def test_the_report_carries_it_per_opportunity():
    from julius.pipeline import analyze

    analise = analyze("data/sample/consumer-avi.json")
    sem_cifra = [item for item in analise.vm.table if not item.include_in_portfolio]

    assert sem_cifra, "o sample precisa ter achado sem cifra para este teste valer"
    assert all(item.blocked_missing for item in sem_cifra), (
        "achado sem cifra e sem motivo é o defeito que isto conserta"
    )
    assert all(item.blocked_unblocked_by for item in sem_cifra)


def test_a_finding_with_a_figure_carries_nothing():
    from julius.pipeline import analyze

    analise = analyze("data/sample/consumer-avi.json")
    com_cifra = [item for item in analise.vm.table if item.include_in_portfolio]

    assert com_cifra
    assert all(not item.blocked_missing for item in com_cifra)


def test_the_json_publishes_it_next_to_the_estimation():
    import json

    from julius.pipeline import analyze
    from julius.reporting.renderer import render_json

    analise = analyze("data/sample/consumer-avi.json")
    payload = json.loads(render_json(analise.vm, analise.opportunities))
    bloqueados = [
        item for item in payload["opportunities"] if item.get("blocked_reason")
    ]

    assert bloqueados
    assert set(bloqueados[0]["blocked_reason"]) == {
        "missing",
        "unblocked_by",
        "source_next_action",
    }


def test_the_spreadsheet_has_the_three_columns():
    from julius.reporting.excel import COLUMNS

    rotulos = [label for label, _, _ in COLUMNS]

    assert "Por que sem cifra" in rotulos
    assert "Quem destrava" in rotulos
    assert "Como destravar" in rotulos
    # Ao lado da economia, e não três abas adiante: quem lê "Indisponível"
    # pergunta por quê na mesma linha.
    assert rotulos.index("Por que sem cifra") == rotulos.index("Economia/mês") + 1


def test_explaining_does_not_change_the_total():
    """A fronteira: isto é explicação, não cifra."""
    from julius.pipeline import analyze

    analise = analyze("data/sample/consumer-avi.json")
    identificada = sum(
        item.portfolio_gain.monthly_expected
        for item in analise.opportunities
        if item.include_in_portfolio
    )

    assert identificada == analise.kpis.identified_monthly
