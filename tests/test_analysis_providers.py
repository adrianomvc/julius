"""Provedores de análise contextual são substituíveis, e isso é verificado.

É o primeiro ponto do projeto onde substituição importa: quem monta o contexto
e quem consome o resultado não podem saber qual provedor está em uso. Trocar
Devin por preenchimento manual não pode mudar o formato do resultado, nem o tipo
do erro, nem exigir preparo extra do chamador.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from julius.analysis import (
    PROVIDERS,
    AgentOutputError,
    AnalysisProvider,
    ContextualAnalysis,
    Workspace,
)
from julius.pipeline import analyze

SAMPLE = "data/sample/consumer-avi.json"


@pytest.fixture(scope="module")
def analysis():
    return analyze(SAMPLE, today=date(2026, 7, 25), scan_id="providers")


def _result_for(workspace: Workspace) -> dict:
    """Resposta mínima e válida para o contexto que foi escrito.

    O validador exige cobertura total: um provedor não pode analisar só as
    oportunidades fáceis e deixar o resto fora sem dizer. O mesmo vale para os
    sinais — todos precisam voltar julgados.
    """
    context = json.loads(workspace.context.read_text(encoding="utf-8"))
    ids = [item["opportunity_id"] for item in context["opportunities"]]
    return {
        "account": context["account"]["id"],
        "scan_id": context["scan_id"],
        "executive_summary": "resumo",
        "implementation_order": ids,
        "recommendations": [
            {
                "opportunity_id": opportunity_id,
                "contextual_diagnosis": "diagnóstico",
                "recommendation": "recomendação",
                "implementation_steps": ["passo"],
                "validation_steps": ["validação"],
                "dependencies": [],
                "conflicts": [],
                "risks": [],
                "documentation": [
                    {
                        "title": "Glue",
                        "url": "https://docs.aws.amazon.com/glue/latest/dg/",
                        "relevance": "tarifa",
                    }
                ],
                "assumptions": [],
                "missing_evidence": [],
            }
            for opportunity_id in ids
        ],
        "signal_verdicts": [
            {
                "rule_id": signal["rule_id"],
                "asset_name": signal["asset_name"],
                "verdict": "rejected",
                "rationale": "o padrão é adequado ao volume observado",
                "evidence_ref": {
                    "sha256": signal["artifact_sha256"],
                    "lines": signal["lines"],
                },
            }
            for signal in context["signals"]
        ],
        "uncovered_findings": [],
    }


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_every_provider_writes_the_same_package(name, analysis, tmp_path):
    """Contexto e schema saem iguais; só a instrução é do provedor."""
    provider = PROVIDERS[name]()
    workspace = Workspace.at(tmp_path / name)

    written = provider.prepare(analysis, workspace)

    assert workspace.context in written
    assert workspace.schema in written
    assert workspace.instructions in written
    assert workspace.context.is_file() and workspace.schema.is_file()


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_every_provider_returns_the_same_validated_type(name, analysis, tmp_path):
    provider = PROVIDERS[name]()
    workspace = Workspace.at(tmp_path / name)
    provider.prepare(analysis, workspace)
    workspace.result.write_text(
        json.dumps(_result_for(workspace)), encoding="utf-8"
    )

    collected = provider.collect(workspace)

    assert isinstance(collected, ContextualAnalysis)
    assert collected.recommendations


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_every_provider_fails_with_the_same_error_type(name, analysis, tmp_path):
    """Quem chama trata um tipo de erro, não os de cada provedor."""
    provider = PROVIDERS[name]()
    workspace = Workspace.at(tmp_path / name)
    provider.prepare(analysis, workspace)

    # Resultado ausente.
    with pytest.raises(AgentOutputError):
        provider.collect(workspace)

    # Resultado ilegível.
    workspace.result.write_text("{ não é json", encoding="utf-8")
    with pytest.raises(AgentOutputError):
        provider.collect(workspace)

    # Resultado válido em forma, mas apontando para outra conta.
    payload = _result_for(workspace)
    payload["account"] = "999999999999"
    workspace.result.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AgentOutputError):
        provider.collect(workspace)


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_no_provider_requires_extra_setup_from_the_caller(name):
    """Construtor sem argumento obrigatório: o workspace basta."""
    import inspect

    provider_class = PROVIDERS[name]
    required = [
        parameter
        for parameter in inspect.signature(provider_class).parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        not in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
    ]
    assert required == []
    assert issubclass(provider_class, AnalysisProvider)
    assert provider_class.name != AnalysisProvider.name, (
        "o provedor precisa se identificar no relatório"
    )


def test_a_provider_cannot_change_what_the_scan_decided(analysis, tmp_path):
    """Provedor acrescenta contexto; número e prioridade são do Julius."""
    provider = PROVIDERS["devin"]()
    workspace = Workspace.at(tmp_path / "guard")
    provider.prepare(analysis, workspace)

    payload = _result_for(workspace)
    payload["recommendations"].append(dict(payload["recommendations"][0]))
    payload["recommendations"][-1]["opportunity_id"] = "INVENTADA-000"
    payload["implementation_order"].append("INVENTADA-000")

    workspace.result.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AgentOutputError):
        provider.collect(workspace)


def test_instructions_tell_the_provider_what_is_decided_and_what_to_look_for(
    analysis, tmp_path
):
    """Só proibição produz resposta defensiva e vazia.

    O pacote precisa dizer o que o Julius já resolveu — para o provedor não
    refazer conta — e o que procurar em cada ativo, para a resposta não virar
    texto genérico sobre um achado que já vinha explicado.
    """
    from julius.analysis.guardrails import DETERMINISTIC, SCOPE

    for name in sorted(PROVIDERS):
        workspace = Workspace.at(tmp_path / name)
        PROVIDERS[name]().prepare(analysis, workspace)
        instructions = workspace.instructions.read_text(encoding="utf-8")

        assert "já está decidido" in instructions.lower()
        for asset, questions in SCOPE:
            assert asset in instructions
            for question in questions:
                assert question in instructions
        for item in DETERMINISTIC:
            assert item in instructions
        # E o recorte do portfólio continua visível.
        assert "no portfólio" in instructions
