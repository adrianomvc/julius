"""O README precisa descrever o CLI que existe — nos dois sentidos.

`tests/test_skill_contract.py` já cobra a Skill: comando que a IA lê tem de
existir. O README tinha o defeito simétrico e ninguém olhava: o CLI cresceu para
23 comandos e o texto documentava 16. `scan`, `validate-pilot`,
`signals coverage`, os três de `pricing` e três de `agent` existiam sem uma linha
de documentação.

A direção que incomoda é **CLI → README**. Sem ela, o arquivo volta a ficar sete
comandos atrás na próxima entrega, porque ninguém lembra de documentar o que
acabou de escrever. Com ela, comando novo sem menção falha aqui.

É o que geração de conteúdo daria, sem acrescentar passo de build a um arquivo
que as pessoas editam à mão.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from julius.cli import app

RAIZ = Path(__file__).resolve().parents[1]
README = RAIZ / "README.md"

#: Comando citado em bloco de shell. A segunda palavra só entra quando é
#: subcomando (`agent prepare`); flag começa com `-` e fica de fora.
#:
#: O separador é `[ \t]`, e não `\s`, porque `\s` casa quebra de linha: duas
#: linhas seguidas de `julius diff` e `julius validate` viravam o comando
#: `diff\njulius`. E o primeiro termo aceita hífen, senão `validate-pilot` entra
#: como `validate`. `test_skill_contract.py` tem a mesma extração e o mesmo
#: defeito latente — lá ele não morde porque nenhum comando com hífen aparece.
_CITADO = re.compile(
    r"^[ \t]*julius[ \t]+([a-z][a-z-]*(?:[ \t]+[a-z][a-z-]*)?)", re.MULTILINE
)

#: Grupos do Typer aparecem como nome solto (`agent`, `pricing`, `signals`) e não
#: são comandos executáveis: citar o grupo não documenta o subcomando.
_GRUPOS = {grupo.name for grupo in app.registered_groups}


def _texto() -> str:
    return README.read_text(encoding="utf-8")


def _comandos_do_cli() -> set[str]:
    nomes = {
        command.name or command.callback.__name__.replace("_", "-")
        for command in app.registered_commands
    }
    for grupo in app.registered_groups:
        nomes.update(
            f"{grupo.name} {command.name or command.callback.__name__}"
            for command in grupo.typer_instance.registered_commands
        )
    return nomes


def _citados() -> set[str]:
    return {item for item in _CITADO.findall(_texto()) if item not in _GRUPOS}


@pytest.mark.parametrize("comando", sorted(_citados()))
def test_every_command_the_readme_names_exists_in_the_cli(comando):
    """Renomear um comando quebra aqui antes de quebrar para quem seguiu o README."""
    assert comando in _comandos_do_cli(), (
        f"o README manda rodar `julius {comando}`, que o CLI não tem"
    )


def test_every_command_in_the_cli_appears_in_the_readme():
    """A direção que pega o defeito real: comando novo sem documentação.

    Falha listando o que ficou de fora, porque a correção é escrever essas linhas
    — e saber quais são é metade do trabalho.
    """
    ausentes = sorted(_comandos_do_cli() - _citados())

    assert not ausentes, (
        "comando do CLI sem menção no README: "
        + ", ".join(f"`julius {item}`" for item in ausentes)
    )


def test_every_relative_link_points_at_a_file_that_exists():
    """Link quebrado é a forma mais barata de documentação mentir."""
    alvos = re.findall(r"\]\((?!https?://|#)([^)#]+)(?:#[^)]*)?\)", _texto())
    quebrados = [alvo for alvo in alvos if not (RAIZ / alvo).exists()]

    assert not quebrados, f"link relativo sem destino: {quebrados}"


def test_the_readme_does_not_repeat_the_rules_the_agent_obeys():
    """As regras da IA têm uma fonte: `docs/ai/`, de onde a Skill é gerada.

    O README aponta para lá. Repetir o texto criaria a terceira cópia — e o
    projeto inteiro é construído sobre duas cópias divergirem em silêncio.
    """
    texto = _texto()

    assert "AGENTS.md" in texto, "o README precisa remeter o agente a AGENTS.md"
    assert "docs/ai/" in texto, "o README precisa apontar a fonte canônica da Skill"


def test_the_version_in_the_readme_matches_the_engine():
    """Versão citada e esquecida é pior que versão ausente."""
    from julius.config import JULIUS_VERSION

    citadas = set(re.findall(r"\bJulius (\d+\.\d+\.\d+)\b", _texto()))

    assert not (citadas - {JULIUS_VERSION}), (
        f"o README cita versão que não é a do motor ({JULIUS_VERSION}): "
        f"{sorted(citadas - {JULIUS_VERSION})}"
    )


def test_the_counts_in_the_readme_come_from_the_engine():
    """Número no README envelhece calado, e este envelheceu.

    O texto anunciava 40 fontes, 135 regras e 22 famílias enquanto o motor já
    tinha 41, 137 e 23 — a defasagem entrou junto com regras e coletores novos,
    e nada falhou. Quem lê o README para saber o tamanho do motor recebia a
    resposta de três PRs atrás.

    A checagem é por número solto e não por frase inteira: a prosa ao redor pode
    ser reescrita à vontade, o que não pode é a contagem discordar do código.
    """
    from julius.collection.sources import SOURCES
    from julius.knowledge.remediation import CATALOG, FAMILIES

    texto = _texto()
    esperado = {
        "fontes": len(SOURCES),
        "regras": len(CATALOG),
        "famílias": len(FAMILIES),
    }

    for nome, quantidade in esperado.items():
        assert re.search(rf"\b{quantidade}\b[^\n]*{nome}|{nome}[^\n]*\b{quantidade}\b", texto), (
            f"o README não diz {quantidade} {nome} — o motor tem {quantidade}, "
            "e o texto ficou para trás"
        )


def test_the_readme_does_not_claim_a_count_the_engine_contradicts():
    """A direção que falta na anterior: número velho ainda presente no texto.

    Sem isto, trocar `135 regras` por `137 regras` em um lugar e esquecer o outro
    passaria — a busca acharia o certo e ignoraria o errado ao lado.
    """
    from julius.collection.sources import SOURCES
    from julius.knowledge.remediation import CATALOG, FAMILIES

    texto = _texto()
    for nome, atual in (
        ("fontes", len(SOURCES)),
        ("regras", len(CATALOG)),
        ("famílias", len(FAMILIES)),
    ):
        citados = {int(n) for n in re.findall(rf"(\d+)\s+{nome}\b", texto)}
        assert citados <= {atual}, (
            f"o README ainda cita {sorted(citados - {atual})} {nome}; o motor tem {atual}"
        )
