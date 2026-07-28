"""A skill que a IA segue tem que descrever o CLI que existe.

`SKILL.md` é o contrato operacional do Devin: é dele que a IA tira como
instalar, como invocar e quais comandos rodar. Quando o CLI muda e a skill não,
o agente não falha em silêncio — ele **improvisa**. Foi o que aconteceu: a
skill mandava instalar com `pip install -e` cru, o lançador `julius` não ficava
no `PATH`, e a IA inventou `python julius/cli.py` — um arquivo que deixou de
existir quando o CLI virou pacote.

Nenhum teste olhava para este arquivo. Estes olham.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from julius.cli import app

SKILL = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "julius-aws-analysis"
    / "SKILL.md"
)


def _texto() -> str:
    return SKILL.read_text(encoding="utf-8")


def _comandos_do_cli() -> set[str]:
    nomes = {
        command.name or command.callback.__name__.replace("_", "-")
        for command in app.registered_commands
    }
    for grupo in app.registered_groups:
        nomes.add(grupo.name)
        nomes.update(
            f"{grupo.name} {command.name or command.callback.__name__}"
            for command in grupo.typer_instance.registered_commands
        )
    return nomes


def test_the_skill_file_exists_where_agents_md_points():
    """`AGENTS.md` manda o agente ler este caminho exato."""
    assert SKILL.is_file()
    assert ".agents/skills/julius-aws-analysis/SKILL.md" in Path("AGENTS.md").read_text(
        encoding="utf-8"
    )


def test_the_skill_never_tells_the_agent_to_run_a_file_inside_the_package():
    """`julius/cli.py` não existe desde que o CLI virou pacote.

    O que se proíbe é a **forma de comando** — `python julius/…`. Citar o
    caminho para dizer que ele não existe é o oposto disso, e é justamente o que
    tira o agente do improviso.
    """
    assert not re.search(r"python\s+julius/", _texto()), (
        "a skill não pode mandar executar arquivo de dentro do pacote; "
        "as formas válidas são `julius <comando>` e `python -m julius.cli`"
    )


def test_the_skill_warns_that_the_old_entry_point_is_gone():
    """O agente errou porque ninguém disse a ele que aquele caminho morreu."""
    assert "julius/cli.py" in _texto()


def test_the_skill_installs_through_the_installer():
    """`pip install -e` cru não põe o lançador no PATH, e o agente trava depois."""
    texto = _texto()

    assert "install/install.sh" in texto
    assert not re.search(r"^\s*python -m pip install", texto, re.MULTILINE)


def test_the_skill_declares_the_fallback_that_needs_no_path():
    """Sem uma segunda forma declarada, `command not found` vira improviso."""
    assert "python -m julius.cli" in _texto()


@pytest.mark.parametrize(
    "comando",
    # A segunda palavra só entra quando é subcomando (`agent prepare`); flag
    # começa com `-` e fica de fora.
    sorted(
        set(
            re.findall(
                r"^\s*julius\s+([a-z]+(?:\s+[a-z][a-z-]*)?)", _texto(), re.MULTILINE
            )
        )
    ),
)
def test_every_command_the_skill_names_exists_in_the_cli(comando):
    """A skill não pode mandar rodar comando que o CLI não tem.

    Prende o par nos dois sentidos: renomear um comando quebra aqui antes de
    quebrar numa execução real da IA.
    """
    disponiveis = _comandos_do_cli()
    primeira_palavra = comando.split()[0]

    assert comando in disponiveis or primeira_palavra in disponiveis, (
        f"a skill manda rodar `julius {comando}`, que não existe no CLI. "
        f"Comandos disponíveis: {sorted(disponiveis)}"
    )
