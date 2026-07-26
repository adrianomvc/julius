"""A seta entre camadas aponta para um lado só, e isso é verificado.

As seis inversões que existiam antes da reestruturação — coleta importando
`estimation` e `agent`, modelo importando configuração — entraram uma a uma,
cada uma parecendo razoável no momento. Sem um teste, a estrutura nova junta
seis novas em dois anos.

Duas regras, com forças diferentes e por um motivo:

- **`collection` não importa nada acima dela.** É a camada que a fase 2 criou e
  a única cuja direção já está limpa. Regra dura.
- **O resto é catraca.** As camadas superiores ainda têm acoplamentos cruzados
  reais, listados abaixo. Enquanto as fases 3 e 4 não separam `knowledge/`,
  `findings/` e `scoring/`, o que dá para garantir é que nenhum acoplamento
  *novo* entre em silêncio.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1] / "julius"

# `config` ainda mistura parâmetros de coleta com preços e limiares de domínio,
# e os módulos de topo compõem tudo por natureza. A separação de `config` é a
# fase 3; esta lista encolhe junto com ela.
NEUTRAL = {"config", "pipeline", "portfolio", "cli"}

# Acoplamentos cruzados que existem hoje entre as camadas de cima. Cada um é uma
# dívida nomeada, não uma permissão: a fase 4 quebra `Opportunity` em
# `findings/` e `scoring/`, e é ela que deve esvaziar esta lista.
KNOWN_COUPLING = {
    # O grafo enriquece oportunidades com contexto de processo.
    ("graph", "opportunities"),
    # A calibração lê o histórico persistido para ajustar estimativas.
    ("estimation", "state"),
    # A regra de código estático consome os artefatos analisados.
    ("opportunities", "code_analysis"),
    # A validação de benefício calcula KPIs para comparar previsto × realizado.
    ("state", "metrics"),
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


def test_collection_depends_on_nothing_above_it():
    """A camada base não conhece estimativa, detecção, relatório nem agente."""
    assert _reaching_up_from("collection") == []


def test_no_new_cross_layer_coupling():
    """Catraca: acoplamento novo entre camadas superiores falha aqui."""
    layers = {package for package, _ in _edges()} | {
        imported for _, imported in _edges()
    }
    downward = {
        ("graph", "collection"),
        ("governance", "collection"),
        ("estimation", "collection"),
        ("estimation", "opportunities"),
        ("estimation", "graph"),
        ("opportunities", "collection"),
        ("opportunities", "estimation"),
        ("code_analysis", "collection"),
        ("state", "collection"),
        ("state", "opportunities"),
        ("metrics", "collection"),
        ("metrics", "opportunities"),
        ("audit", "collection"),
        ("report", "collection"),
        ("report", "opportunities"),
        ("report", "estimation"),
        ("report", "governance"),
        ("report", "graph"),
        ("report", "state"),
        ("notification", "collection"),
        ("notification", "opportunities"),
        ("notification", "report"),
        ("agent", "collection"),
        ("agent", "opportunities"),
        ("agent", "report"),
        ("agent", "graph"),
        ("agent", "state"),
        ("agent", "code_analysis"),
        ("agent", "estimation"),
        ("graph", "governance"),
        ("report", "code_analysis"),
        ("report", "metrics"),
    }
    assert layers, "nenhuma camada encontrada — o varredor quebrou"
    unexpected = sorted(_edges() - downward - KNOWN_COUPLING)
    assert unexpected == []


def test_the_rule_actually_fails_when_a_layer_reaches_upward(tmp_path):
    """Um teste de arquitetura que nunca falhou não prova nada."""
    offender = ROOT / "collection" / "_direction_probe.py"
    offender.write_text(
        "from julius.report import formatters  # noqa: F401\n", encoding="utf-8"
    )
    try:
        assert _reaching_up_from("collection"), (
            "a regra deixou de detectar uma seta invertida"
        )
    finally:
        offender.unlink()

    assert _reaching_up_from("collection") == []


def test_known_coupling_is_still_real():
    """Dívida que já foi paga sai da lista em vez de virar folclore."""
    stale = sorted(KNOWN_COUPLING - _edges())
    assert stale == []
