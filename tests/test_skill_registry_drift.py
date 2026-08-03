"""A fonte canônica é `docs/ai/`; `.agents/skills/` é o que sai dela.

Antes a direção era a oposta, e o custo aparecia de duas formas. Trocar de host
significava copiar um arquivo que tinha o procedimento do Devin dentro. E as
mesmas regras existiam de novo, com outro texto e em outro idioma, dentro de
`guardrails.py`, sem nada comparando as duas versões — foi assim que a lista de
métodos de estimativa ficou três meses desatualizada.

Inverter a direção só vale se o artefato não puder ser editado à mão sem que
apareça. É o que este arquivo cobra.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from julius.analysis.skill_registry import (
    CANONICO,
    HOSTS,
    RAIZ,
    SkillSourceError,
    check,
    expected_files,
    load_skills,
    render_host_artifact,
)


def test_the_generated_artifacts_are_up_to_date():
    """`--check` é o que transforma 'é gerado' em fato verificável."""
    problemas = check()

    assert not problemas, (
        "artefato de Skill divergente da fonte canônica:\n  "
        + "\n  ".join(problemas)
        + "\nRegenere com: python scripts/generate_skill_registry.py"
    )


def test_editing_the_artifact_by_hand_is_caught(tmp_path, monkeypatch):
    """A contraprova: sem ela, o teste acima passaria com o gerador quebrado."""
    alvo = RAIZ / ".agents" / "skills" / "julius-aws-analysis" / "SKILL.md"
    original = alvo.read_text(encoding="utf-8")
    try:
        alvo.write_text(original + "\nlinha acrescentada à mão\n", encoding="utf-8")
        assert check(), "editar o artefato à mão precisa ser detectado"
    finally:
        alvo.write_text(original, encoding="utf-8")

    assert not check(), "o estado original precisa voltar limpo"


def test_the_check_command_fails_loudly_on_drift():
    """O comando existe e é usável fora do pytest — é o que o dev roda."""
    resultado = subprocess.run(
        [sys.executable, "scripts/generate_skill_registry.py", "--check"],
        cwd=RAIZ,
        capture_output=True,
        text=True,
    )

    assert resultado.returncode == 0, resultado.stderr


def test_the_canonical_skill_carries_no_host_procedure():
    """Fonte canônica não menciona host. Se mencionar, deixou de ser canônica."""
    ofensores = []
    for skill in load_skills():
        for termo in ("devin", "claude", "copilot", "codex"):
            if re.search(termo, skill.body, re.IGNORECASE):
                ofensores.append(f"{skill.path.name}: {termo}")

    assert not ofensores, (
        f"Skill canônica cita host específico: {ofensores}. "
        "O procedimento de host vive em docs/ai/hosts/."
    )


def test_the_engine_fields_reach_the_artifact():
    """De nada adianta gerar se o valor do motor não chega no arquivo final."""
    from julius.knowledge.contextual_estimation import allowed_methods

    artefato = (
        RAIZ / ".agents" / "skills" / "julius-aws-analysis" / "SKILL.md"
    ).read_text(encoding="utf-8")
    frontmatter = artefato.split("---")[1]

    for rule_id, metodo in allowed_methods().items():
        assert metodo in frontmatter, f"método ausente do artefato: {metodo}"
        assert rule_id in frontmatter, f"rule_id ausente do artefato: {rule_id}"


def test_the_generator_holds_no_prose_of_its_own():
    """O gerador monta; ele não guarda o texto.

    O antipadrão é o gerador que carrega a prosa inteira dentro do `.py`: o
    arquivo passa a ser "gerado" e o conteúdo passa a morar no gerador — uma
    segunda fonte de verdade, escondida onde ninguém procura. Aqui nenhuma frase
    do corpo canônico pode aparecer no módulo que o monta.
    """
    fonte = (
        RAIZ / "julius" / "analysis" / "skill_registry.py"
    ).read_text(encoding="utf-8")

    for skill in load_skills():
        for linha in skill.body.splitlines():
            texto = linha.strip()
            # Frases, não títulos nem marcadores curtos.
            if len(texto) < 40 or texto.startswith(("#", "|", "-", "`")):
                continue
            assert texto not in fonte, (
                f"prosa da Skill dentro do gerador: {texto[:60]!r}. "
                "O gerador monta o artefato; o texto mora em docs/ai/."
            )


def test_a_skill_without_the_required_sections_is_refused():
    """O contrato de corpo é cobrado na carga, não na revisão."""
    from julius.analysis.skill_registry import load_skill

    incompleta = CANONICO / "skills" / "_fixture_incompleta.md"
    incompleta.write_text(
        "---\nname: x\ndescription: y\ntrigger: z\nsections_to_load:\n  - rules\n---\n\n## purpose\n\nsó isto.\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(SkillSourceError, match="seção obrigatória"):
            load_skill(incompleta)
    finally:
        incompleta.unlink()


def test_every_host_gets_the_same_canonical_body():
    """Trocar de host troca o bloco do host, não o que a Skill diz.

    Hoje só existe um host. O teste vale mesmo assim: ele é o que impede o
    segundo host de nascer com uma cópia divergente do corpo.
    """
    for skill in load_skills():
        corpo = skill.body.rstrip()
        for host in HOSTS:
            artefato = render_host_artifact(skill, host)
            assert corpo in artefato, (
                f"o artefato de {host} não carrega o corpo canônico de {skill.name}"
            )


def test_the_artifact_declares_that_it_is_generated():
    """Quem abrir o arquivo precisa saber onde editar de verdade."""
    for caminho, conteudo in expected_files().items():
        if caminho.suffix != ".md":
            continue
        assert "GERADO" in conteudo or "Gerado por" in conteudo, (
            f"artefato sem aviso de geração: {Path(caminho).name}"
        )
