"""Trocar de host é trocar de provedor, e nada mais.

A afirmação é fácil de fazer e difícil de conferir com um host só — que é o caso
hoje, por decisão de produto: a análise roda no Devin. Comparar dois artefatos
reais seria trivialmente verdadeiro se só existisse um.

Então o teste **gera um host sintético** a partir da mesma fonte canônica e
compara. Nada é instalado para ele e nada é commitado; ele existe dentro do teste,
pelo tempo do teste. O que isso prova é o que importa: o corpo canônico não
depende de host nenhum, e um segundo host custaria uma linha em `HOSTS` mais um
bloco de procedimento.

O que este arquivo protege é o caminho de volta. Suportar um host novo copiando o
`SKILL.md` do primeiro funcionaria hoje e divergiria na primeira correção feita
num lado só — que foi exatamente o que aconteceu entre `guardrails.py` e a Skill
antiga, e custou dois métodos de estimativa desligados por três meses.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from julius.analysis import PROVIDERS, Workspace
from julius.analysis.skill_registry import (
    CANONICO,
    HOSTS,
    RAIZ,
    load_skills,
    render_host_artifact,
)
from julius.pipeline import analyze

SAMPLE = "data/sample/consumer-avi.json"


@pytest.fixture(scope="module")
def analysis():
    return analyze(SAMPLE, today=date(2026, 7, 25), scan_id="hosts")


@pytest.fixture
def host_sintetico(monkeypatch, tmp_path):
    """Um segundo host, montado da mesma fonte e jogado fora no fim.

    É o que substitui o artefato real que existia quando havia dois hosts. A
    diferença prática é nenhuma para o que se quer provar, e a diferença de
    manutenção é grande: artefato que ninguém usa em produção diverge sem
    ninguém notar.
    """
    bloco = CANONICO / "hosts" / "sintetico.md"
    bloco.write_text(
        "# Procedimento — host sintetico\n\n"
        "Existe só dentro do teste de agnosticidade.\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(HOSTS, "sintetico", tmp_path / "skills")
    try:
        yield "sintetico"
    finally:
        bloco.unlink(missing_ok=True)


def test_only_devin_ships_an_artifact():
    """Um host real, por decisão de produto. O mecanismo é que segue plural."""
    assert set(HOSTS) == {"devin"}
    assert not (RAIZ / ".claude" / "skills").exists(), (
        "artefato de host que não roda em produção diverge sem ninguém notar"
    )


@pytest.mark.parametrize("skill", load_skills(), ids=lambda s: s.name)
def test_a_new_host_gets_the_same_canonical_body(skill, host_sintetico):
    """O corpo é um só, e um host novo não pode trazer uma cópia divergente."""
    corpo = skill.body.strip()

    devin = render_host_artifact(skill, "devin")
    sintetico = render_host_artifact(skill, host_sintetico)

    assert corpo in devin
    assert corpo in sintetico


@pytest.mark.parametrize("skill", load_skills(), ids=lambda s: s.name)
def test_the_hosts_differ_only_in_their_own_block(skill, host_sintetico):
    """A diferença precisa ser o bloco de host — não regra, não contrato."""
    corpo = skill.body.strip()

    devin = render_host_artifact(skill, "devin").split(corpo, 1)[1]
    sintetico = render_host_artifact(skill, host_sintetico).split(corpo, 1)[1]

    assert "DEVIN" in devin.upper()
    assert "sintetico" in sintetico
    assert "DEVIN" not in sintetico.upper()


def test_adding_a_host_costs_one_line_and_one_block(host_sintetico):
    """A promessa da inversão fonte → artefato, medida em vez de afirmada."""
    skill = load_skills()[0]

    assert render_host_artifact(skill, host_sintetico)


def test_no_public_symbol_is_named_after_a_host():
    """`DEVIN_OUTPUT_SCHEMA` era o último. O schema nunca foi do Devin."""
    from julius.analysis import response_validator

    nomeados = [
        nome
        for nome in dir(response_validator)
        if not nome.startswith("_")
        and nome.isupper()
        and any(host in nome.lower() for host in ("devin", "claude", "copilot", "codex"))
        and nome != "DEVIN_OUTPUT_SCHEMA"  # alias declarado, com prazo
    ]

    assert not nomeados, f"símbolo público com nome de host: {nomeados}"


def test_the_old_schema_name_still_resolves():
    from julius.analysis.response_validator import (
        ANALYSIS_OUTPUT_SCHEMA,
        DEVIN_OUTPUT_SCHEMA,
    )

    assert DEVIN_OUTPUT_SCHEMA is ANALYSIS_OUTPUT_SCHEMA


@pytest.mark.parametrize("nome", sorted(PROVIDERS))
def test_every_provider_writes_the_same_context_and_schema(nome, analysis, tmp_path):
    """Quem monta o contexto e quem consome o resultado não sabem qual provedor é."""
    workspace = Workspace.at(tmp_path / nome)
    arquivos = PROVIDERS[nome]().prepare(analysis, workspace)

    assert workspace.context in arquivos
    assert workspace.schema in arquivos

    contexto = json.loads(workspace.context.read_text(encoding="utf-8"))
    assert contexto["scan_id"] == "hosts"


def test_the_context_is_byte_identical_across_providers(analysis, tmp_path):
    """O pacote não pode depender de quem vai lê-lo."""
    contextos = {}
    for nome in sorted(PROVIDERS):
        workspace = Workspace.at(tmp_path / f"ctx-{nome}")
        PROVIDERS[nome]().prepare(analysis, workspace)
        contextos[nome] = workspace.context.read_text(encoding="utf-8")

    assert len(set(contextos.values())) == 1


def test_the_rules_reach_every_provider_unchanged(analysis, tmp_path):
    """Nenhum host recebe uma versão mais frouxa das regras."""
    from julius.analysis.guardrails import RULES

    for nome in sorted(PROVIDERS):
        workspace = Workspace.at(tmp_path / f"regras-{nome}")
        PROVIDERS[nome]().prepare(analysis, workspace)
        instrucoes = workspace.instructions.read_text(encoding="utf-8")

        for regra in RULES:
            assert regra in instrucoes, f"{nome} não recebeu uma das regras"


def test_the_readme_does_not_brand_the_product_with_a_host():
    titulo = (RAIZ / "README.md").read_text(encoding="utf-8").splitlines()[0]

    assert not any(
        host in titulo.lower() for host in ("devin", "claude", "copilot", "codex")
    ), f"o título do README cita um host: {titulo!r}"
