"""As proibições da estimativa como guarda, não como prosa.

Elas existiam no plano, no briefing e em comentário. Nada disso recusa um número.
Aqui cada uma é uma checagem sobre a `ContextualEstimate` pronta, e violá-la
rebaixa o resultado para `needs_evidence` com o motivo anexado — a informação de
*por que* aquele sinal não virou cifra vale mais que o silêncio.

As três misturas — período, moeda e região — são as que mais merecem teste,
porque nenhuma delas dá erro em lugar nenhum: somar dólar com real, mês com
trimestre ou tarifa de duas regiões produz um número bem formado e errado, que
ninguém detecta depois porque o resultado não diz de onde veio.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from julius.findings.investigation import ContextualEstimate
from julius.findings.maturity import Maturity, pode_entrar_no_portfolio
from julius.knowledge.billing_mechanisms import (
    UnknownBillingMechanismError,
    known_mechanisms,
    mechanism,
    mechanism_for_method,
)
from julius.knowledge.estimate_contract import enforce, problems


def _valida(**overrides) -> ContextualEstimate:
    """Uma estimativa que passa em tudo. Cada teste quebra exatamente um campo."""
    base = {
        "method": "glue_shuffle_reduction_v1",
        "status": "estimated",
        "baseline_cost": 1000.0,
        "estimated_low": 100.0,
        "estimated_expected": 150.0,
        "estimated_high": 200.0,
        "maturity": Maturity.PILOT_REQUIRED,
        "pricing_region": "sa-east-1",
        "currency": "USD",
        "period": "monthly",
        "method_version": "v1",
        "evidence_hash": "abc123",
        "assumptions": ["benchmark A/B com o mesmo volume"],
    }
    return ContextualEstimate(**{**base, **overrides})


def test_a_complete_estimate_passes():
    """A contraprova de todos os outros: a trava não pode barrar tudo."""
    assert problems(_valida()) == []
    assert enforce(_valida()).status == "estimated"


def test_saving_above_baseline_is_refused():
    """Economia maior que o custo do ativo exige custo downstream comprovado."""
    achados = problems(_valida(estimated_high=1500.0))

    assert any("acima do baseline" in item for item in achados)


def test_a_range_out_of_order_is_refused():
    achados = problems(_valida(estimated_low=300.0))

    assert any("fora de ordem" in item for item in achados)


def test_a_missing_region_is_refused():
    """Sem região não há como recusar a soma de duas tarifas diferentes."""
    achados = problems(_valida(pricing_region=""))

    assert any("região" in item for item in achados)


def test_a_currency_that_differs_from_the_baseline_is_refused():
    achados = problems(_valida(currency="BRL"), baseline_currency="USD")

    assert any("moeda" in item for item in achados)


def test_an_incomparable_period_is_refused():
    """Converter trimestre em mês exigiria premissa que ninguém declarou."""
    achados = problems(_valida(period="quarterly"))

    assert any("período" in item for item in achados)


def test_a_method_without_a_version_is_refused():
    """Sem versão a conta deixa de ser reproduzível depois que a fórmula muda."""
    achados = problems(_valida(method_version=""))

    assert any("versão" in item for item in achados)


def test_an_estimate_without_an_evidence_signature_is_refused():
    """É a assinatura que permite invalidar quando o script muda de hash."""
    achados = problems(_valida(evidence_hash=""))

    assert any("evidência" in item for item in achados)


def test_a_method_without_a_declared_billing_mechanism_is_refused():
    """Estimar sem saber a unidade cobrada é estimar sobre nada."""
    achados = problems(_valida(method="metodo_inventado_v1"))

    assert any("mecanismo de cobrança" in item for item in achados)


def test_an_estimate_without_assumptions_is_refused():
    """Percentual sem justificativa é o que a faixa existe para não ser."""
    achados = problems(_valida(assumptions=[]))

    assert any("premissa" in item for item in achados)


def test_a_missing_baseline_is_refused():
    """Valor ausente não vira zero: vira pergunta."""
    achados = problems(_valida(baseline_cost=None))

    assert any("baseline" in item for item in achados)


def test_portfolio_flag_and_maturity_cannot_disagree():
    """Os dois dizem a mesma coisa; discordar é bug silencioso de dinheiro."""
    achados = problems(_valida(include_in_portfolio=True))

    assert any("include_in_portfolio" in item for item in achados)


def test_a_violation_downgrades_instead_of_raising():
    """Cenário que não fecha não é erro de programação — é evidência faltando."""
    rebaixada = enforce(_valida(estimated_high=1500.0))

    assert rebaixada.status == "needs_evidence"
    assert rebaixada.maturity == Maturity.PILOT_REQUIRED
    assert rebaixada.include_in_portfolio is False
    assert rebaixada.estimated_high is None
    assert any("acima do baseline" in item for item in rebaixada.missing_evidence)


def test_the_reason_survives_the_downgrade():
    """Sem o motivo, o relatório mostra um sinal sem número e sem explicação."""
    rebaixada = enforce(_valida(pricing_region="", currency=""))

    assert len(rebaixada.missing_evidence) >= 2


def test_needs_evidence_is_left_alone():
    """Não há o que verificar num resultado que já diz que falta evidência."""
    sem_numero = ContextualEstimate(
        method="glue_shuffle_reduction_v1",
        status="needs_evidence",
        missing_evidence=["shuffle medido"],
    )

    assert problems(sem_numero) == []
    assert enforce(sem_numero) is sem_numero


# ---------------------------------------------------------------------------
# Maturidade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "estado",
    [Maturity.POTENTIAL, Maturity.CONTEXTUAL_ESTIMATE, Maturity.PILOT_REQUIRED],
)
def test_the_immature_states_never_reach_the_portfolio(estado):
    assert pode_entrar_no_portfolio(estado) is False


@pytest.mark.parametrize("estado", [Maturity.VALIDATED_MODEL, Maturity.MEASURED])
def test_only_a_validated_model_or_a_measurement_may_be_summed(estado):
    assert pode_entrar_no_portfolio(estado) is True


def test_maturity_is_not_evidence_quality():
    """Fundir as duas escalas produziria uma terceira que não responde nenhuma.

    Uma estimativa pode ter evidência forte — baseline vindo da fatura
    reconciliada — e ainda assim não poder ser somada, porque o cenário nunca
    foi testado.
    """
    from julius.scoring.evidence_quality import EvidenceQuality

    assert {item.value for item in Maturity}.isdisjoint(
        {item.name.lower() for item in EvidenceQuality} - {"measured"}
    )


# ---------------------------------------------------------------------------
# Mecanismo de cobrança
# ---------------------------------------------------------------------------


def test_every_allowed_method_declares_how_the_service_charges():
    """Método sem mecanismo é conta sem unidade — e a estimativa é recusada."""
    from julius.knowledge.contextual_estimation import allowed_methods

    faltando = [
        metodo
        for metodo in sorted(set(allowed_methods().values()))
        if mechanism_for_method(metodo) is None
    ]

    assert not faltando, f"método sem mecanismo de cobrança declarado: {faltando}"


def test_an_unknown_mechanism_fails_loudly():
    with pytest.raises(UnknownBillingMechanismError):
        mechanism("nao_existe")


def test_every_mechanism_carries_its_minimum_and_its_documentation():
    """O mínimo faturável é o campo que mais evita economia que não acontece."""
    for chave in known_mechanisms():
        item = mechanism(chave)
        assert item.unit.strip(), f"{chave} sem unidade"
        assert item.caveat.strip(), f"{chave} sem ressalva"
        assert item.doc.startswith("https://docs.aws.amazon.com/"), (
            f"{chave} sem documentação oficial"
        )


def test_the_athena_minimum_is_the_one_that_bites():
    """10 MB por query é o que faz a query de 2 MB não custar um quinto."""
    assert "10 MB" in mechanism("athena_bytes_scanned").minimum


def test_the_cold_class_minimum_is_declared():
    """128 KB por objeto é o que torna prefixo de arquivo pequeno mais caro."""
    assert "128 KB" in mechanism("s3_storage_gb_month").minimum


# ---------------------------------------------------------------------------
# Procedência ponta a ponta
# ---------------------------------------------------------------------------


def test_the_engine_stamps_provenance_on_every_method():
    """Cada método preenchendo a sua é como um deles acaba sem região."""
    from julius.collection.models import Account, GlueJob
    from julius.config import DEFAULT_CONFIG
    from julius.findings.investigation import AIEstimationProposal
    from julius.findings.signal import Signal
    from julius.knowledge.contextual_estimation import evaluate_proposal

    conta = Account(
        account_id="123456789012",
        glue_jobs=[
            GlueJob(
                name="etl",
                shuffle_write_bytes=8 * 1024**3,
                has_spill_evidence=True,
                dpu_seconds_window=360000,
            )
        ],
    )
    sinal = Signal(
        kind="code",
        rule_id="GLUE-CODE-SHUFFLE",
        asset_type="glue_job",
        asset_name="etl",
        observation="shuffle",
        question="é evitável?",
        artifact_sha256="deadbeef",
        lines=[10],
    )

    resultado = evaluate_proposal(
        conta,
        sinal,
        AIEstimationProposal(
            method="glue_shuffle_reduction_v1", target={"expected_reduction": 0.2}
        ),
        DEFAULT_CONFIG,
    )

    assert resultado.pricing_region == DEFAULT_CONFIG.pricing.region
    assert resultado.currency == DEFAULT_CONFIG.pricing.currency
    assert resultado.period == "monthly"
    assert resultado.method_version == "v1"
    assert resultado.evidence_hash == sinal.evidence_signature()
    assert resultado.maturity == Maturity.PILOT_REQUIRED
    assert resultado.include_in_portfolio is False


def test_a_changed_artifact_changes_the_signature_that_invalidates_it():
    """É assim que uma estimativa deixa de valer quando o script muda."""
    from julius.findings.signal import Signal

    antes = Signal(
        kind="code",
        rule_id="GLUE-CODE-SHUFFLE",
        asset_type="glue_job",
        asset_name="etl",
        observation="o",
        question="q",
        artifact_sha256="aaa",
        lines=[10],
    )
    depois = replace(antes, artifact_sha256="bbb")

    assert antes.evidence_signature() != depois.evidence_signature()
