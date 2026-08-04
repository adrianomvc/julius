"""Eval que não aponta quem o cobra é prosa que envelhece sem quebrar nada.

Uma Skill nasce de lacuna observada, com dois ou três casos concretos que servem
de critério de aceitação. Adotar isso literalmente produziria, aqui, um segundo
lugar descrevendo o mesmo comportamento que os testes já verificam — e o segundo lugar é o que fica errado primeiro, porque nada falha
quando ele diverge.

Então cada eval declara `enforced_by`, e este arquivo cobra a ligação: o teste
citado precisa existir. O eval passa a ser a explicação de **por que** aquele caso
importa, com o teste ao lado provando que ele vale.
"""

from __future__ import annotations

import re

import pytest

from julius.analysis.skill_registry import (
    CASOS_OBRIGATORIOS,
    RAIZ,
    eval_problems,
    load_evals,
    load_skills,
)

TODOS = [item for skill in load_skills() for item in load_evals(skill.name)]


def test_every_skill_has_the_required_cases():
    """Confirmado, rejeitado e sem evidência. É o mínimo, e ele para aqui."""
    problemas = eval_problems()

    assert not problemas, "\n  ".join(["evals incompletos:", *problemas])


def test_there_are_evals_to_check():
    """Sem eval nenhum, todo teste deste arquivo passaria por vacuidade."""
    assert TODOS, "nenhum eval encontrado"
    assert len({item.skill for item in TODOS}) == len(load_skills())


@pytest.mark.parametrize("eval_", TODOS, ids=lambda e: f"{e.skill}/{e.path.stem}")
def test_the_test_that_enforces_the_eval_exists(eval_):
    """A ligação é o ponto. Sem ela o eval vira documentação paralela."""
    arquivo, _, funcao = eval_.enforced_by.partition("::")

    caminho = RAIZ / arquivo
    assert caminho.is_file(), f"{eval_.path.name}: {arquivo} não existe"
    assert funcao, f"{eval_.path.name}: enforced_by sem nome de teste"

    fonte = caminho.read_text(encoding="utf-8")
    assert re.search(rf"^def {re.escape(funcao)}\(", fonte, re.M), (
        f"{eval_.path.name}: {funcao} não existe em {arquivo}. "
        "Renomear um teste sem atualizar o eval quebra aqui, que é o objetivo."
    )


@pytest.mark.parametrize("eval_", TODOS, ids=lambda e: f"{e.skill}/{e.path.stem}")
def test_every_eval_names_a_real_rule(eval_):
    """`rule_id` inventado num eval descreveria um caso que não acontece."""
    fonte = "\n".join(
        caminho.read_text(encoding="utf-8")
        for caminho in (RAIZ / "julius").rglob("*.py")
    )

    assert f'"{eval_.rule_id}"' in fonte, (
        f"{eval_.path.name}: {eval_.rule_id} não existe no catálogo de regras"
    )


@pytest.mark.parametrize("eval_", TODOS, ids=lambda e: f"{e.skill}/{e.path.stem}")
def test_every_case_is_a_verdict_the_schema_accepts(eval_):
    assert eval_.case in CASOS_OBRIGATORIOS


@pytest.mark.parametrize("eval_", TODOS, ids=lambda e: f"{e.skill}/{e.path.stem}")
def test_every_eval_explains_the_gap_it_covers(eval_):
    """Sem a lacuna, o eval descreve um comportamento sem dizer por que importa."""
    corpo = eval_.path.read_text(encoding="utf-8")

    for secao in ("## lacuna", "## entrada", "## saída esperada", "## critério de aceitação"):
        assert secao in corpo, f"{eval_.path.name}: falta {secao}"

    lacuna = corpo.split("## lacuna")[1].split("##")[0].strip()
    assert len(lacuna) > 60, f"{eval_.path.name}: lacuna curta demais para explicar nada"


def test_a_skill_without_evals_fails_the_check(tmp_path, monkeypatch):
    """A contraprova: sem ela, `eval_problems` poderia devolver lista vazia sempre."""
    import julius.analysis.skill_registry as registry

    falsa = tmp_path / "skills" / "julius-sem-eval"
    falsa.mkdir(parents=True)
    (falsa / "SKILL.md").write_text(
        "---\nname: julius-sem-eval\ndescription: d\ntrigger: t\n"
        "sections_to_load:\n  - rules\n---\n\n"
        + "\n".join(
            f"## {secao}\n\ntexto\n"
            for secao in (
                "purpose",
                "inputs",
                "expected output",
                "does",
                "does not",
                "rules",
                "output contract",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "evals").mkdir()
    monkeypatch.setattr(registry, "CANONICO", tmp_path)

    problemas = registry.eval_problems()

    assert any("sem evals" in item for item in problemas)


def test_a_renamed_test_breaks_the_link(tmp_path):
    """Prova que a checagem de `enforced_by` não é decorativa."""
    from julius.analysis.skill_registry import _parse_frontmatter

    falso = tmp_path / "x.md"
    falso.write_text(
        "---\nskill: s\ncase: confirmed\nrule_id: R\n"
        "enforced_by: tests/test_evals.py::test_que_nao_existe\n---\n\ncorpo\n",
        encoding="utf-8",
    )
    dados, _ = _parse_frontmatter(falso.read_text(encoding="utf-8"), falso)
    arquivo, _, funcao = str(dados["enforced_by"]).partition("::")

    fonte = (RAIZ / arquivo).read_text(encoding="utf-8")
    assert not re.search(rf"^def {re.escape(funcao)}\(", fonte, re.M)


def test_the_economic_evals_cover_the_expensive_mistakes():
    """Os erros que produzem número bem formado e errado precisam ter caso.

    São os que ninguém detecta depois, porque o resultado não diz de onde veio.
    """
    # A Skill era separada e nunca foi instalada: `install/install.sh` publica só
    # `julius-aws-analysis`, e o briefing só nomeava essa. Os casos econômicos
    # passaram para a Skill fundida, e continuam sendo cobrados aqui pelo nome do
    # arquivo — renomear um deles quebra este teste, que é o objetivo.
    casos = {item.path.stem for item in load_evals("julius-aws-analysis")}

    for esperado in (
        "moeda-incompativel",
        "periodo-incompativel",
        "sem-preco",
        "economia-maior-que-baseline",
        "nao-entra-no-portfolio",
    ):
        assert esperado in casos, f"falta o eval {esperado}"


def test_no_eval_lives_outside_a_skill_folder():
    """Eval órfão descreve um contrato que ninguém carrega."""
    pastas = {p.name for p in (RAIZ / "docs" / "ai" / "evals").iterdir() if p.is_dir()}
    skills = {skill.name for skill in load_skills()}

    assert pastas <= skills, f"evals sem Skill correspondente: {pastas - skills}"
