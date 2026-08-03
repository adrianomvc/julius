"""O que um veredito escreve no inventário é catálogo, não `if`.

O catálogo tem uma entrada, e isso é resultado: varrer o modelo atrás de campos
declarados como não-coletáveis encontra exatamente um. A diferença entre catálogo
e `if` aparece na **segunda** entrada — sem catálogo, o segundo fato entra como
mais um `if` e ninguém percebe que ele sobrescreve algo que a API já responde.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from julius.collection.models import Account, StateMachine
from julius.knowledge.semantic_facts import (
    CATALOG,
    apply_confirmed,
    by_rule,
    known_fact_types,
)
from julius.knowledge.verdict_facts import apply_verdicts

PACOTE = Path(__file__).resolve().parents[1] / "julius"


class _Decisao:
    def __init__(self, rule_id, asset_name, verdict, evidence_hash="a1b2c3"):
        self.rule_id = rule_id
        self.asset_name = asset_name
        self.verdict = verdict
        self.evidence_hash = evidence_hash


def _conta(**overrides) -> Account:
    base = {"name": "orquestra", "type": "STANDARD", "idempotent": None}
    return Account(
        account_id="123456789012",
        state_machines=[StateMachine(**{**base, **overrides})],
    )


# ---------------------------------------------------------------------------
# A direção, que é o que mais custa errar
# ---------------------------------------------------------------------------


def test_a_confirmed_verdict_asserts_the_signal_hypothesis():
    """Confirmar `SFN-STANDARD-TO-EXPRESS` é dizer que a reexecução é tolerável.

    Ler ao contrário recomendaria Express para a máquina que duplica cobrança —
    exatamente o dano que a pergunta existe para evitar. Este teste é o que
    impede a inversão de passar despercebida.
    """
    conta = _conta()

    aplicados = apply_confirmed(
        conta, [_Decisao("SFN-STANDARD-TO-EXPRESS", "orquestra", "confirmed")]
    )

    assert aplicados
    assert conta.state_machines[0].idempotent is True


def test_a_rejected_verdict_writes_nothing():
    """"Duplica cobrança" e "não vale o esforço" chegam com o mesmo rótulo."""
    conta = _conta()

    aplicados = apply_confirmed(
        conta, [_Decisao("SFN-STANDARD-TO-EXPRESS", "orquestra", "rejected")]
    )

    assert aplicados == []
    assert conta.state_machines[0].idempotent is None


def test_needs_evidence_writes_nothing():
    conta = _conta()

    assert (
        apply_confirmed(
            conta, [_Decisao("SFN-STANDARD-TO-EXPRESS", "orquestra", "needs_evidence")]
        )
        == []
    )
    assert conta.state_machines[0].idempotent is None


# ---------------------------------------------------------------------------
# Procedência
# ---------------------------------------------------------------------------


def test_a_verdict_without_an_evidence_signature_writes_nothing():
    """Fato que não pode ser invalidado é pior que fato ausente.

    Ele continua valendo sobre um artefato que já mudou. O `SignalDecision` real
    sempre carrega a assinatura — `record_verdicts` grava
    `signal.evidence_signature()` em toda linha —, então esta porta nunca dispara
    em produção; ela existe para o caminho que a contorna.
    """
    conta = _conta()

    aplicados = apply_confirmed(
        conta,
        [_Decisao("SFN-STANDARD-TO-EXPRESS", "orquestra", "confirmed", evidence_hash="")],
    )

    assert aplicados == []
    assert conta.state_machines[0].idempotent is None


def test_the_real_ledger_always_supplies_the_signature(tmp_path):
    """A contraprova da porta acima: o caminho de produção não é barrado."""
    from julius.findings.signal import Signal
    from julius.state.signal_ledger import SignalLedger

    class _Veredito:
        rule_id = "SFN-STANDARD-TO-EXPRESS"
        asset_name = "orquestra"
        verdict = "confirmed"
        rationale = "ASL sem efeito colateral não idempotente"
        recommendation = None
        estimation_proposal = None

    sinal = Signal(
        kind="config",
        rule_id="SFN-STANDARD-TO-EXPRESS",
        asset_type="state_machine",
        asset_name="orquestra",
        observation="o",
        question="q",
    )
    livro = SignalLedger(tmp_path / "ledger.json")
    livro.record_verdicts(
        [_Veredito()],
        [sinal],
        account="123456789012",
        scan_id="s1",
        prompt_version="1.10.0",
    )

    decisoes = livro.decisions_for("123456789012")
    assert decisoes and all(item.evidence_hash for item in decisoes)

    conta = _conta()
    assert apply_confirmed(conta, decisoes)
    assert conta.state_machines[0].idempotent is True


def test_an_unknown_rule_writes_nothing():
    """Só o que está no catálogo escreve; o resto é veredito sem destino."""
    conta = _conta()

    assert apply_confirmed(conta, [_Decisao("GLUE-CODE-SHUFFLE", "orquestra", "confirmed")]) == []


def test_an_asset_outside_the_inventory_writes_nothing():
    conta = _conta()

    assert apply_confirmed(conta, [_Decisao("SFN-STANDARD-TO-EXPRESS", "outra", "confirmed")]) == []


# ---------------------------------------------------------------------------
# O que o catálogo não aceita
# ---------------------------------------------------------------------------


def _atributos_escritos_por_coletores() -> set[str]:
    """Atributos que algum coletor atribui — por keyword ou por atribuição."""
    escritos: set[str] = set()
    for caminho in (PACOTE / "collection").rglob("*.py"):
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if isinstance(no, ast.keyword) and no.arg:
                escritos.add(no.arg)
            elif isinstance(no, ast.Assign):
                for alvo in no.targets:
                    if isinstance(alvo, ast.Attribute):
                        escritos.add(alvo.attr)
    return escritos


def test_no_fact_overwrites_something_the_api_already_answers():
    """A lição do `checkpoint_configured`, virada em teste.

    Ele parece candidato — a regra de spot depende dele — e é **coletável**: vem
    de `CheckpointConfig` em `describe_training_job`. Deixar um veredito
    escrevê-lo trocaria fato medido por opinião, que é o que a regra dos campos
    determinísticos imutáveis proíbe.
    """
    coletados = _atributos_escritos_por_coletores()

    invasores = [
        f"{fato.fact_type} → {fato.collection}.{fato.attribute}"
        for fato in CATALOG
        if fato.attribute in coletados
    ]

    assert not invasores, (
        f"fato semântico sobre campo que algum coletor preenche: {invasores}. "
        "Se a API responde, quem responde é a API."
    )
    # E a contraprova: o campo que ensinou a lição continua sendo detectado.
    assert "checkpoint_configured" in coletados


def test_no_fact_writes_a_number():
    """Número é campo determinístico, e a IA não os altera."""
    for fato in CATALOG:
        assert isinstance(fato.value_on_confirmed, bool), (
            f"{fato.fact_type} escreve {type(fato.value_on_confirmed).__name__}; "
            "só booleano é fato semântico"
        )


def test_no_fact_writes_ownership():
    """Owner vem de tag ou convenção de nome; inventado, cobra a pessoa errada."""
    proibidos = {"owner", "owner_email", "team", "squad", "responsavel"}

    for fato in CATALOG:
        assert fato.attribute not in proibidos, (
            f"{fato.fact_type} escreve ownership, que não é julgamento"
        )


@pytest.mark.parametrize("fato", CATALOG, ids=lambda f: f.fact_type)
def test_every_fact_says_why_the_api_cannot_answer_it(fato):
    """Entrada sem essa frase é um `if` com nome bonito."""
    assert len(fato.why_not_collectable) > 30
    assert len(fato.means) > 20


@pytest.mark.parametrize("fato", CATALOG, ids=lambda f: f.fact_type)
def test_every_fact_targets_a_field_that_exists(fato):
    """Catálogo apontando para campo inexistente falha em silêncio."""
    from julius.collection import models

    assert hasattr(Account(account_id="1"), fato.collection)
    classes = [
        obj
        for obj in vars(models).values()
        if isinstance(obj, type) and hasattr(obj, "__dataclass_fields__")
    ]
    assert any(fato.attribute in obj.__dataclass_fields__ for obj in classes), (
        f"{fato.attribute} não existe em nenhum modelo"
    )


def test_the_catalog_has_no_duplicate_fact_type():
    assert len(known_fact_types()) == len(CATALOG)


def test_lookup_by_rule_finds_the_only_entry():
    assert by_rule("SFN-STANDARD-TO-EXPRESS") is not None
    assert by_rule("NAO-EXISTE") is None


def test_the_public_entry_point_still_works():
    """`pipeline` chama `apply_verdicts`; o catálogo é detalhe interno."""
    conta = _conta()

    assert apply_verdicts(
        conta, [_Decisao("SFN-STANDARD-TO-EXPRESS", "orquestra", "confirmed")]
    )
    assert conta.state_machines[0].idempotent is True
