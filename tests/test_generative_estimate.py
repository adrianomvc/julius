"""O único caminho em que a IA devolve número, e tudo que o cerca.

O risco aqui não é o de sempre. Nos outros caminhos a IA escolhe um cenário e o
motor faz a conta; aqui a faixa vem dela. O que impede isso de virar número
inventado com aparência de rigor não é uma coisa, são sete, e cada uma tem teste:
elegibilidade por `rule_id`, baseline resolvido pelo motor, teto no custo do
ativo, mecanismo de cobrança conhecido, plano de validação, documentação oficial e
premissa declarada.

O teste que mais importa é o último arquivo: nada daqui entra no portfólio. Ele
espelha `test_signal_range_never_enters_portfolio` de propósito — a mesma trava,
para o caminho novo.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from julius.collection.models import Account, GlueJob, SageMakerJob
from julius.config import DEFAULT_CONFIG
from julius.findings.investigation import AIContextualEstimate
from julius.findings.maturity import Maturity, pode_entrar_no_portfolio
from julius.findings.signal import Signal
from julius.knowledge.generative_estimation import (
    baseline_for,
    eligible,
    eligible_rule_ids,
    evaluate_contextual,
)

DOC = "https://docs.aws.amazon.com/glue/latest/dg/monitor-debug-capacity.html"


def _conta() -> Account:
    return Account(
        account_id="123456789012",
        glue_jobs=[GlueJob(name="etl", dpu_seconds_window=360000)],
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
    )


def _sinal(rule_id="GLUE-CODE-PYTHON-UDF", asset="etl") -> Signal:
    return Signal(
        kind="code",
        rule_id=rule_id,
        asset_type="glue_job",
        asset_name=asset,
        observation="UDF Python",
        question="é necessária aqui?",
        artifact_sha256="deadbeef",
        lines=[42],
    )


def _faixa(**overrides) -> AIContextualEstimate:
    base = {
        "billing_mechanism": "glue_dpu_hour",
        "reasoning": "UDF por linha sobre ~40M linhas; serialização domina o estágio",
        "inputs": {"linhas_estimadas": 40_000_000, "duracao_min": 55},
        "low": 30.0,
        "expected": 60.0,
        "high": 90.0,
        "assumptions": ["substituição por função nativa preserva o resultado"],
        "validation_plan": ["benchmark A/B com o mesmo volume de entrada"],
        "documentation": [DOC],
        "missing_evidence": [],
    }
    return AIContextualEstimate(**{**base, **overrides})


# ---------------------------------------------------------------------------
# O caso que funciona
# ---------------------------------------------------------------------------


def test_a_complete_contextual_estimate_produces_a_range():
    """A contraprova de todas as travas: elas não podem barrar tudo."""
    resultado = evaluate_contextual(_conta(), _sinal(), _faixa(), DEFAULT_CONFIG)

    assert resultado.status == "estimated"
    assert resultado.estimated_low <= resultado.estimated_expected <= resultado.estimated_high
    assert resultado.maturity == Maturity.CONTEXTUAL_ESTIMATE
    assert resultado.pricing_region == DEFAULT_CONFIG.pricing.region
    assert resultado.evidence_hash == _sinal().evidence_signature()


def test_the_reasoning_and_the_baseline_source_survive_into_the_assumptions():
    """Faixa que ninguém consegue refazer é faixa acreditada, não estimada."""
    resultado = evaluate_contextual(_conta(), _sinal(), _faixa(), DEFAULT_CONFIG)

    texto = " ".join(resultado.assumptions)
    assert "serialização domina" in texto
    assert "resolvido pelo motor" in texto
    assert "glue_dpu_hour" in texto


def test_the_validation_plan_becomes_missing_evidence():
    """Enquanto o piloto não rodar, o plano é exatamente o que falta."""
    resultado = evaluate_contextual(_conta(), _sinal(), _faixa(), DEFAULT_CONFIG)

    assert any("benchmark A/B" in item for item in resultado.missing_evidence)


# ---------------------------------------------------------------------------
# A trava que mais importa
# ---------------------------------------------------------------------------


def test_a_generative_estimate_never_enters_the_portfolio():
    """Espelha `test_signal_range_never_enters_portfolio`, para o caminho novo."""
    resultado = evaluate_contextual(_conta(), _sinal(), _faixa(), DEFAULT_CONFIG)

    assert resultado.include_in_portfolio is False
    assert pode_entrar_no_portfolio(resultado.maturity) is False


def test_the_whole_path_is_off_when_the_allowlist_is_empty(monkeypatch):
    """Desligar é esvaziar o mapa — sem tocar em código nem em texto."""
    import julius.knowledge.generative_estimation as ge

    monkeypatch.setattr(ge, "_ELEGIVEIS", {})

    assert ge.eligible_rule_ids() == ()
    with pytest.raises(ValueError, match="não aceita estimativa contextual"):
        ge.evaluate_contextual(_conta(), _sinal(), _faixa(), DEFAULT_CONFIG)


def test_the_briefing_stops_offering_it_when_the_allowlist_is_empty(monkeypatch):
    """O rollback precisa alcançar o que a IA lê, não só o que o motor aceita."""
    import julius.knowledge.generative_estimation as ge
    from julius.analysis.guardrails import _generative_eligibility

    assert _generative_eligibility()
    monkeypatch.setattr(ge, "_ELEGIVEIS", {})
    assert _generative_eligibility() == ""


# ---------------------------------------------------------------------------
# As sete condições
# ---------------------------------------------------------------------------


def test_a_rule_outside_the_allowlist_is_refused():
    with pytest.raises(ValueError, match="não aceita estimativa contextual"):
        evaluate_contextual(
            _conta(), _sinal(rule_id="GLUE-CODE-SHUFFLE"), _faixa(), DEFAULT_CONFIG
        )


def test_the_engine_resolves_the_baseline_not_the_proposal():
    """Baseline proposto pela análise é a via mais curta para um número inventado."""
    valor, fonte = baseline_for(_conta(), _sinal(), DEFAULT_CONFIG)

    assert valor and valor > 0
    assert "DPU-hora" in fonte
    # E o campo do resultado é esse valor, não algo que veio na proposta.
    resultado = evaluate_contextual(_conta(), _sinal(), _faixa(), DEFAULT_CONFIG)
    assert resultado.baseline_cost == round(valor, 2)


def test_a_range_above_the_baseline_is_cut_to_it():
    """Economia acima do custo do ativo exigiria custo downstream comprovado."""
    valor, _ = baseline_for(_conta(), _sinal(), DEFAULT_CONFIG)
    assert valor is not None

    resultado = evaluate_contextual(
        _conta(), _sinal(), _faixa(high=valor * 10, expected=valor * 5, low=valor * 2), DEFAULT_CONFIG
    )

    assert resultado.estimated_high == round(valor, 2)
    assert any("reduzida ao baseline" in item for item in resultado.missing_evidence)


def test_an_asset_without_cost_produces_no_range():
    """`None` é resposta melhor que zero, que se leria como "não há o que ganhar"."""
    conta = Account(account_id="1", glue_jobs=[GlueJob(name="etl")])

    resultado = evaluate_contextual(conta, _sinal(), _faixa(), DEFAULT_CONFIG)

    assert resultado.status == "needs_evidence"
    assert resultado.estimated_high is None


@pytest.mark.parametrize(
    ("override", "esperado"),
    [
        ({"validation_plan": []}, "plano de validação"),
        ({"assumptions": []}, "premissa"),
        ({"documentation": []}, "documentação oficial"),
        ({"documentation": ["https://aws.amazon.com/glue/pricing/"]}, "documentação oficial"),
        ({"inputs": {}}, "entradas nomeadas"),
        ({"reasoning": "   "}, "raciocínio"),
        ({"billing_mechanism": "athena_bytes_scanned"}, "não corresponde"),
    ],
)
def test_each_missing_condition_downgrades_with_its_reason(override, esperado):
    """Cada condição derruba a faixa, e diz qual foi."""
    resultado = evaluate_contextual(
        _conta(), _sinal(), _faixa(**override), DEFAULT_CONFIG
    )

    assert resultado.status == "needs_evidence"
    assert any(esperado in item for item in resultado.missing_evidence)


def test_promotional_documentation_is_not_official_documentation():
    """Página de produto não é tarifa, e não sustenta mecanismo de cobrança."""
    resultado = evaluate_contextual(
        _conta(),
        _sinal(),
        _faixa(documentation=["https://aws.amazon.com/glue/pricing/"]),
        DEFAULT_CONFIG,
    )

    assert resultado.status == "needs_evidence"


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", eligible_rule_ids())
def test_every_eligible_rule_says_why_no_formula_closes(rule_id):
    """Entrada sem essa frase é a lista crescendo por hábito."""
    candidato = eligible(rule_id)

    assert candidato is not None
    assert len(candidato.why_no_formula) > 40
    assert candidato.baseline_source in {"glue_job", "sagemaker_job"}


@pytest.mark.parametrize("rule_id", eligible_rule_ids())
def test_no_eligible_rule_also_has_a_deterministic_method(rule_id):
    """Havendo fórmula, o caminho é a proposta de cenário — nunca a faixa.

    Um `rule_id` nos dois mapas deixaria a IA escolher entre dar o cenário e dar
    o número, e a escolha certa nunca seria dela.
    """
    from julius.knowledge.contextual_estimation import allowed_methods

    assert rule_id not in allowed_methods(), (
        f"{rule_id} tem fórmula no motor; a faixa contextual não se aplica"
    )


@pytest.mark.parametrize("rule_id", eligible_rule_ids())
def test_every_eligible_mechanism_exists_in_the_catalogue(rule_id):
    from julius.knowledge.billing_mechanisms import known_mechanisms

    candidato = eligible(rule_id)
    assert candidato is not None
    assert candidato.mechanism in known_mechanisms()


def test_the_sagemaker_path_resolves_its_own_baseline():
    """Os dois caminhos de baseline precisam funcionar, não só o do Glue."""
    sinal = replace(
        _sinal(rule_id="SM-CODE-FIXED-EPOCHS", asset="treino"),
        asset_type="sagemaker_training_job",
    )

    valor, fonte = baseline_for(_conta(), sinal, DEFAULT_CONFIG)

    assert valor == 400.0
    assert "modelado" in fonte
