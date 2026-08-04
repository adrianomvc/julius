"""A análise pode dizer que dois sinais são a mesma correção — e discordar do motor.

O motor já sabe a família de cada regra: está no catálogo. O valor de pedir a opinião
da análise não é substituir a resposta — família agrupa dinheiro e é campo
determinístico. É a discordância: quando quem leu o artefato inteiro conclui que a
correção é outra, isso é erro de catálogo aparecendo, e catálogo errado funde ações
que não são a mesma.
"""

from __future__ import annotations

import pytest

from julius.analysis.response_validator import AgentOutputError, validate_agent_output


def _pacote(**overrides) -> dict:
    veredito = {
        "rule_id": "GLUE-CODE-SHUFFLE",
        "asset_name": "etl",
        "verdict": "rejected",
        "rationale": "o join é sobre uma tabela de lookup de 200 linhas",
        "evidence_ref": {"sha256": "a" * 64, "lines": [42]},
    }
    veredito.update(overrides)
    return {
        "account": "123456789012",
        "scan_id": "scan-1",
        "executive_summary": "resumo",
        "implementation_order": [],
        "recommendations": [],
        "signal_verdicts": [veredito],
        "uncovered_findings": [],
        "suspected_injections": [],
    }


def _valida(payload: dict):
    return validate_agent_output(
        payload,
        account="123456789012",
        scan_id="scan-1",
        allowed_opportunity_ids=set(),
        expected_signals={("GLUE-CODE-SHUFFLE", "etl"): "a" * 64},
        known_artifact_hashes={"a" * 64},
        known_rule_ids={"GLUE-CODE-SHUFFLE"},
    )


def test_the_field_is_optional():
    """Pacote antigo, sem o campo, continua válido: a expansão é aditiva."""
    resultado = _valida(_pacote())
    assert resultado.signal_verdicts[0].remediation_family == ""
    assert resultado.signal_verdicts[0].family_matches_catalog is None


def test_an_agreeing_verdict_is_recorded_as_agreeing():
    resultado = _valida(_pacote(remediation_family="shuffle_partitioning"))
    assert resultado.signal_verdicts[0].family_matches_catalog is True


def test_a_disagreement_is_kept_and_not_applied():
    """Discordância é informação, não sobrescrita: família é campo determinístico."""
    resultado = _valida(_pacote(remediation_family="driver_memory_cache"))
    veredito = resultado.signal_verdicts[0]
    assert veredito.remediation_family == "driver_memory_cache"
    assert veredito.family_matches_catalog is False


def test_an_invented_family_is_refused():
    with pytest.raises(AgentOutputError, match="remediation_family desconhecida"):
        _valida(_pacote(remediation_family="familia_inventada"))


def test_an_unknown_field_is_still_refused():
    """A troca de conjuntos exatos por obrigatórias+opcionais não afrouxou nada."""
    with pytest.raises(AgentOutputError, match="não permitidos"):
        _valida(_pacote(campo_inventado="x"))


def test_a_missing_required_field_says_which_one():
    payload = _pacote()
    del payload["signal_verdicts"][0]["rationale"]
    with pytest.raises(AgentOutputError, match="faltando.*rationale"):
        _valida(payload)


def test_the_schema_declares_the_field():
    """Campo que o parser aceita e o schema não declara é campo que ninguém usa."""
    from julius.analysis.response_validator import ANALYSIS_OUTPUT_SCHEMA

    propriedades = ANALYSIS_OUTPUT_SCHEMA["properties"]["signal_verdicts"]["items"][
        "properties"
    ]
    assert "remediation_family" in propriedades


def test_the_families_are_part_of_the_contract():
    """O validador recusa família fora da lista, então a lista é contrato."""
    from julius.analysis.skill_registry import engine_fields
    from julius.knowledge.remediation import FAMILIES

    assert engine_fields()["remediation_families"] == sorted(FAMILIES)
