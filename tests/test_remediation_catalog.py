"""Regra nova sem família de remediação não passa daqui.

O catálogo em `knowledge/remediation.py` só serve para agrupar se cobrir tudo. Uma
regra sem família não desaparece do relatório — ela aparece sozinha, fora de qualquer
grupo, e o leitor a conta como um trabalho a mais. O sintoma é indistinguível de uma
regra que legitimamente não tem par, e é por isso que a checagem precisa ser
automática em vez de disciplina de revisão.

A varredura é por AST sobre o fonte, a mesma técnica de `tests/test_read_only.py`: o
catálogo não pode importar `knowledge.rules` — a seta aponta ao contrário —, então a
completude não cabe numa checagem de importação como a de `collection/sources.py`.

**O que esta rede não pega**, dito para ninguém confiar nela além do que ela cobre:
identificador montado em tempo de execução por concatenação. Todo `rule_id` do produto
hoje é literal, e um construído dinamicamente escaparia — mas também escaparia da
busca de qualquer pessoa lendo o código, que é o problema maior.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from julius.knowledge.remediation import (
    CATALOG,
    ESFORCO_MAXIMO,
    ESFORCO_MINIMO,
    FAMILIES,
    RESOLVEDORES,
    family_for,
)

RAIZ = Path(__file__).resolve().parents[1] / "julius"

#: Onde um `rule_id` pode nascer. `pipeline` entra porque emite dois sinais próprios
#: — regressão de eficiência e processo não recorrente — que não moram em regra
#: nenhuma.
FONTES = (RAIZ / "knowledge" / "rules", RAIZ / "pipeline.py")

#: Um identificador de regra tem prefixo de serviço com pelo menos duas letras e ao
#: menos um segmento depois. O piso de duas letras é o que separa `S3-SMALL-FILES` de
#: `M-DPU`, que é unidade de capacidade do Glue e não regra.
IDENTIFICADOR = re.compile(r"^[A-Z][A-Z0-9]+(-[A-Z0-9]+)+$")


def _arquivos() -> list[Path]:
    saida: list[Path] = []
    for fonte in FONTES:
        saida.extend(sorted(fonte.rglob("*.py")) if fonte.is_dir() else [fonte])
    return saida


def _rule_ids() -> dict[str, set[str]]:
    """Todo literal com forma de `rule_id`, e em que arquivo ele aparece."""
    encontrados: dict[str, set[str]] = {}
    for arquivo in _arquivos():
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if (
                isinstance(no, ast.Constant)
                and isinstance(no.value, str)
                and IDENTIFICADOR.match(no.value)
            ):
                encontrados.setdefault(no.value, set()).add(arquivo.name)
    return encontrados


def test_every_rule_has_a_remediation_family():
    sem_familia = {
        rule_id: sorted(arquivos)
        for rule_id, arquivos in _rule_ids().items()
        if rule_id not in CATALOG
    }
    assert not sem_familia, (
        "regra sem família de remediação — ela apareceria sozinha no relatório e o "
        f"leitor a contaria como trabalho a mais: {sem_familia}"
    )


def test_the_catalog_has_no_rule_that_the_source_no_longer_emits():
    """Entrada órfã é pior que ausente: sugere cobertura que não existe."""
    conhecidos = set(_rule_ids())
    orfas = sorted(set(CATALOG) - conhecidos)
    assert not orfas, (
        f"família declarada para regra que o fonte não emite mais: {orfas}"
    )


def test_every_family_is_used_by_at_least_one_rule():
    usadas = set(CATALOG.values())
    ociosas = sorted(set(FAMILIES) - usadas)
    assert not ociosas, f"família sem nenhuma regra: {ociosas}"


def test_every_family_declares_why_it_is_one_action():
    """A frase é o que impede a família de virar gaveta de coisas parecidas."""
    for family in FAMILIES.values():
        assert family.why.strip(), f"{family.id} sem justificativa"
        assert family.label.strip(), f"{family.id} sem rótulo"
        assert family.measurement.strip(), f"{family.id} sem forma de medir"


def test_effort_stays_inside_the_declared_scale():
    for family in FAMILIES.values():
        assert ESFORCO_MINIMO <= family.effort <= ESFORCO_MAXIMO, (
            f"{family.id} com esforço {family.effort} fora da escala"
        )


def test_every_family_says_who_confirms_it():
    for family in FAMILIES.values():
        assert family.resolved_by in RESOLVEDORES, (
            f"{family.id} com resolvedor desconhecido: {family.resolved_by!r}"
        )


def test_an_unknown_rule_is_expensive_by_default():
    """O desconhecido não pode ser oferecido como se fosse barato.

    Se `family_for` devolvesse uma família genérica, uma regra nova entraria no
    relatório agrupada com ações que não são a mesma correção — e barata, competindo
    por atenção com o que já foi medido.
    """
    from julius.knowledge.remediation import measurement_effort, resolved_by

    assert family_for("REGRA-QUE-NAO-EXISTE") is None
    assert measurement_effort("REGRA-QUE-NAO-EXISTE") == ESFORCO_MAXIMO
    assert resolved_by("REGRA-QUE-NAO-EXISTE") == "time"


def test_the_families_that_motivated_the_catalog_are_together():
    """Os pares que `_has_runtime_correlation` já tratava como a mesma evidência.

    Este teste é o motivo de o catálogo existir: quatro sinais do mesmo script Glue
    que se resolvem com duas mudanças. Se alguém os separar, o relatório volta a
    mostrar quatro trabalhos onde há dois.
    """
    assert CATALOG["GLUE-CODE-SHUFFLE"] == CATALOG["GLUE-CODE-SINGLE-PARTITION"]
    assert (
        CATALOG["GLUE-CODE-DRIVER-MATERIALIZATION"]
        == CATALOG["GLUE-CODE-CACHE-LIFECYCLE"]
    )
    assert (
        CATALOG["GLUE-CODE-REPEATED-ACTIONS"]
        == CATALOG["GLUE-CODE-CACHE-LIFECYCLE"]
    )
    # E o par entre camadas: o achado de código e o de configuração sobre o mesmo
    # arquivo pequeno, que o pipeline já desduplica à mão em `analyze_account`.
    assert CATALOG["GLUE-CODE-SMALL-FILES"] == CATALOG["GLUE-SMALL-FILES-OUTPUT"]
