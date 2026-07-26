"""A seta entre camadas aponta para um lado só, e isso é verificado.

As seis inversões que existiam antes da reestruturação — coleta importando
`estimation` e `agent`, modelo importando configuração — entraram uma a uma,
cada uma parecendo razoável no momento. Sem um teste, a estrutura nova junta
seis novas em dois anos.

A ordem das camadas, de baixo para cima:

    collection → findings → scoring → knowledge → grafo/estado → relatório

Cada uma só enxerga o que está abaixo. As exceções que restam estão nomeadas
em `KNOWN_COUPLING`, com o motivo de cada uma — são dívida, não permissão.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1] / "julius"

# `config` é ponto de composição: importa de baixo e ninguém de baixo importa
# dele. Os módulos de topo compõem tudo por natureza.
NEUTRAL = {"config", "pipeline", "portfolio", "cli"}

# O que cada camada pode importar.
ALLOWED: dict[str, set[str]] = {
    # Base: fala com a AWS e preenche o próprio modelo.
    "collection": set(),
    # O achado e seu ciclo de vida, sobre o inventário coletado.
    "findings": {"collection"},
    # Pontuação: transforma achado em prioridade.
    "scoring": {"collection", "findings"},
    # Conhecimento de domínio: lê inventário, devolve achados.
    "knowledge": {"collection", "findings", "scoring"},
    "graph": {"collection", "findings", "scoring", "governance"},
    "governance": {"collection"},
    "audit": {"collection"},
    "metrics": {"collection", "findings"},
    "state": {"collection", "findings", "scoring", "metrics"},
    "report": {
        "collection", "findings", "scoring", "knowledge", "graph",
        "governance", "state", "metrics", "code_analysis",
    },
    "notification": {"collection", "findings", "report"},
    "agent": {
        "collection", "findings", "scoring", "knowledge", "graph", "state",
        "report", "code_analysis",
    },
    "code_analysis": {"collection", "knowledge"},
}

# Dívida nomeada. Cada entrada é uma seta que aponta para cima e o motivo de
# ainda existir — não é permissão, é lembrete com endereço.
KNOWN_COUPLING = {
    # `findings.build` monta o ganho chamando a pontuação. A assinatura de
    # `build` é o item [SOLID/I] da fase 4: quando a regra devolver só uma
    # `Estimation` e a pontuação a transformar em ganho, esta seta some.
    ("findings", "scoring"),
    # A calibração lê o histórico persistido para ajustar estimativas.
    ("scoring", "state"),
    # O custo por processo precisa do grafo para saber quais raízes existem.
    ("scoring", "graph"),
    # O relatório embute a análise contextual produzida pelo agente.
    ("report", "agent"),
}


def _imported_packages(path: pathlib.Path) -> set[str]:
    """Pacotes `julius.<x>` que um módulo importa."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("julius."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("julius."):
                    found.add(alias.name.split(".")[1])
    return found


def _edges() -> set[tuple[str, str]]:
    """Todas as arestas `pacote → pacote` que o código tem hoje."""
    edges: set[tuple[str, str]] = set()
    for path in sorted(ROOT.rglob("*.py")):
        package = path.relative_to(ROOT).parts[0]
        if package.endswith(".py"):
            continue  # módulo de topo, não é camada
        for imported in _imported_packages(path):
            if imported != package and imported not in NEUTRAL:
                edges.add((package, imported))
    return edges


def _violations() -> list[str]:
    problems = []
    for package, imported in sorted(_edges()):
        if (package, imported) in KNOWN_COUPLING:
            continue
        if imported not in ALLOWED.get(package, set()):
            problems.append(f"julius.{package} -> julius.{imported}")
    return problems


def _reaching_up_from(package: str) -> list[str]:
    """Arquivos de `package` que importam qualquer outra camada."""
    offenders = []
    for path in sorted((ROOT / package).rglob("*.py")):
        for imported in sorted(_imported_packages(path)):
            if imported != package and imported not in NEUTRAL:
                offenders.append(
                    f"{path.relative_to(ROOT.parent).as_posix()} -> julius.{imported}"
                )
    return offenders


def test_layers_only_import_downward():
    assert _violations() == []


def test_collection_depends_on_nothing_above_it():
    """A camada base não conhece regra, pontuação, relatório nem agente."""
    assert _reaching_up_from("collection") == []


def test_collection_does_not_even_import_the_composition_root():
    """Nem `config`: a coleta recebe configuração, não a busca."""
    importers = [
        path.relative_to(ROOT.parent).as_posix()
        for path in sorted((ROOT / "collection").rglob("*.py"))
        if "config" in _imported_packages(path)
    ]
    assert importers == []


def test_findings_do_not_depend_on_the_rules_that_produce_them():
    """A entidade de achado não conhece regra nem coletor de serviço."""
    forbidden = {"knowledge", "report", "notification", "agent", "state"}
    offenders = [
        f"{path.relative_to(ROOT.parent).as_posix()} -> julius.{imported}"
        for path in sorted((ROOT / "findings").rglob("*.py"))
        for imported in sorted(_imported_packages(path))
        if imported in forbidden
    ]
    assert offenders == []


def test_every_declared_layer_exists():
    """Regra apontando para pacote inexistente para de valer em silêncio."""
    missing = [name for name in ALLOWED if not (ROOT / name).is_dir()]
    assert missing == []


def test_the_rule_actually_fails_when_a_layer_reaches_upward():
    """Um teste de arquitetura que nunca falhou não prova nada."""
    offender = ROOT / "collection" / "_direction_probe.py"
    offender.write_text(
        "from julius.report import formatters  # noqa: F401\n", encoding="utf-8"
    )
    try:
        assert _violations(), "a regra deixou de detectar uma seta invertida"
    finally:
        offender.unlink()

    assert _violations() == []


def test_known_coupling_is_still_real():
    """Dívida que já foi paga sai da lista em vez de virar folclore."""
    stale = sorted(KNOWN_COUPLING - _edges())
    assert stale == []
