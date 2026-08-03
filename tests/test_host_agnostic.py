"""Trocar de host é trocar de provedor, e nada mais.

A afirmação era fácil de fazer e impossível de conferir enquanto existia um host
só. Com dois, ela vira teste: os dois artefatos precisam carregar exatamente o
mesmo corpo canônico, os provedores precisam produzir pacotes com o mesmo
contexto e o mesmo schema, e a diferença precisa caber no bloco de host.

O que este arquivo protege é o caminho de volta. Suportar um segundo host copiando
o `SKILL.md` do primeiro funcionaria hoje e divergiria na primeira correção feita
num lado só — que foi exatamente o que aconteceu entre `guardrails.py` e a Skill
antiga, e custou dois métodos de estimativa desligados por três meses.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from julius.analysis import PROVIDERS, Workspace
from julius.analysis.skill_registry import HOSTS, RAIZ, load_skills
from julius.pipeline import analyze

SAMPLE = "data/sample/consumer-avi.json"


@pytest.fixture(scope="module")
def analysis():
    return analyze(SAMPLE, today=date(2026, 7, 25), scan_id="hosts")


def test_more_than_one_host_exists():
    """Sem o segundo host, tudo abaixo passaria sem provar nada."""
    assert len(HOSTS) >= 2, "a agnosticidade só é verificável com dois hosts"


@pytest.mark.parametrize("skill", load_skills(), ids=lambda s: s.name)
def test_every_host_artifact_carries_the_same_canonical_body(skill):
    """O corpo é um só. Se divergir, alguém editou o artefato em vez da fonte."""
    corpo = skill.body.strip()

    for host, destino in HOSTS.items():
        artefato = destino / skill.name / "SKILL.md"
        assert artefato.is_file(), f"artefato de {host} ausente: {artefato}"
        assert corpo in artefato.read_text(encoding="utf-8"), (
            f"o artefato de {host} não carrega o corpo canônico de {skill.name}"
        )


@pytest.mark.parametrize("skill", load_skills(), ids=lambda s: s.name)
def test_the_hosts_differ_only_in_their_own_block(skill):
    """A diferença precisa ser o bloco de host — não regra, não contrato."""
    textos = {
        host: (destino / skill.name / "SKILL.md").read_text(encoding="utf-8")
        for host, destino in HOSTS.items()
    }
    corpo = skill.body.strip()

    for host, texto in textos.items():
        bloco = texto.split(corpo, 1)[1]
        outros = [h for h in HOSTS if h != host]
        for outro in outros:
            assert f"docs/ai/hosts/{outro}.md" not in bloco, (
                f"o artefato de {host} cita o procedimento de {outro}"
            )
        # O host se identifica no próprio bloco, e só nele.
        assert host.lower() in bloco.lower(), (
            f"o bloco de {host} não diz de qual host é"
        )


def test_no_public_symbol_is_named_after_a_host():
    """`DEVIN_OUTPUT_SCHEMA` era o último. O schema nunca foi do Devin."""
    from julius.analysis import response_validator

    publicos = [
        nome
        for nome in dir(response_validator)
        if not nome.startswith("_") and nome.isupper()
    ]
    nomeados = [
        nome
        for nome in publicos
        if any(host in nome.lower() for host in ("devin", "claude", "copilot", "codex"))
        and nome != "DEVIN_OUTPUT_SCHEMA"  # alias declarado, com prazo
    ]

    assert not nomeados, f"símbolo público com nome de host: {nomeados}"


def test_the_old_schema_name_still_resolves():
    """Renomear não pode quebrar quem importava — o alias é o contrato."""
    from julius.analysis.response_validator import (
        ANALYSIS_OUTPUT_SCHEMA,
        DEVIN_OUTPUT_SCHEMA,
    )

    assert DEVIN_OUTPUT_SCHEMA is ANALYSIS_OUTPUT_SCHEMA


@pytest.mark.parametrize("nome", sorted(PROVIDERS))
def test_every_provider_writes_the_same_context_and_schema(nome, analysis, tmp_path):
    """Quem monta o contexto e quem consome o resultado não sabem qual provedor é.

    É o contrato declarado em `providers/base.py`, e com três provedores ele
    passa a ser verificável em vez de prometido.
    """
    workspace = Workspace.at(tmp_path / nome)
    arquivos = PROVIDERS[nome]().prepare(analysis, workspace)

    assert workspace.context in arquivos
    assert workspace.schema in arquivos

    contexto = json.loads(workspace.context.read_text(encoding="utf-8"))
    schema = json.loads(workspace.schema.read_text(encoding="utf-8"))

    assert contexto["scan_id"] == "hosts"
    assert schema["required"], "o schema precisa chegar ao provedor"


def test_the_context_is_byte_identical_across_providers(analysis, tmp_path):
    """O pacote não pode depender de quem vai lê-lo.

    Se o contexto mudasse por provedor, comparar dois julgamentos deixaria de
    comparar duas análises e passaria a comparar dois pacotes.
    """
    contextos = {}
    for nome in sorted(PROVIDERS):
        workspace = Workspace.at(tmp_path / f"ctx-{nome}")
        PROVIDERS[nome]().prepare(analysis, workspace)
        contextos[nome] = workspace.context.read_text(encoding="utf-8")

    assert len(set(contextos.values())) == 1, (
        f"o contexto difere entre provedores: {sorted(contextos)}"
    )


def test_the_instructions_point_at_the_right_host_skill(analysis, tmp_path):
    """Cada provedor manda ler a Skill instalada no host dele."""
    esperado = {
        "devin": ".agents/skills/julius-aws-analysis/SKILL.md",
        "claude": ".claude/skills/julius-aws-analysis/SKILL.md",
    }

    for nome, caminho in esperado.items():
        workspace = Workspace.at(tmp_path / f"inst-{nome}")
        PROVIDERS[nome]().prepare(analysis, workspace)
        instrucoes = workspace.instructions.read_text(encoding="utf-8")

        assert caminho in instrucoes, f"{nome} não aponta a própria Skill"
        for outro, alheio in esperado.items():
            if outro != nome:
                assert alheio not in instrucoes, f"{nome} aponta a Skill de {outro}"


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
    """O título era "MVP 4: IA no Devin" — o documento mais lido do repositório."""
    titulo = (RAIZ / "README.md").read_text(encoding="utf-8").splitlines()[0]

    assert not any(
        host in titulo.lower() for host in ("devin", "claude", "copilot", "codex")
    ), f"o título do README cita um host: {titulo!r}"
