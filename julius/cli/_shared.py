"""Valores e verificações que mais de um comando usa."""

from __future__ import annotations

import json

import typer

from julius.analysis import (
    AgentOutputError,
    load_agent_context,
    validate_result_file,
)

app = typer.Typer(
    add_completion=False,
    help="Julius — oportunidades de otimização AWS.",
)
agent_app = typer.Typer(
    add_completion=False,
    help="Pacote de análise contextual para um provedor preencher.",
)
app.add_typer(agent_app, name="agent")

pricing_app = typer.Typer(
    add_completion=False,
    help="Tabela de preço por região: inspecionar a Price List API e regerar.",
)
app.add_typer(pricing_app, name="pricing")

_DEFAULT_INPUT = "data/sample/consumer-avi.json"
_DEFAULT_STORE = "data/state/backlog.json"
_DEFAULT_HISTORY = "data/state/julius.duckdb"
_DEFAULT_PARQUET = "data/state/parquet"


def _load_agent_enrichment(
    context_path: str,
    result_path: str,
):
    if bool(context_path) != bool(result_path):
        raise typer.BadParameter(
            "--agent-context e --agent-result devem ser informados juntos."
        )
    if not context_path:
        return None, None
    try:
        context = load_agent_context(context_path)
        contextual = validate_result_file(context_path, result_path)
    except (
        AgentOutputError,
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(str(exc)) from exc
    return context, contextual


def _require_same_opportunity_set(context, analysis) -> None:
    if context is None:
        return
    expected = {
        str(item["opportunity_id"]) for item in context.opportunities
    }
    available = {
        item.opportunity_id for item in analysis.opportunities
    }
    missing = sorted(expected - available)
    if missing:
        raise typer.BadParameter(
            "o relatório não reproduziu as oportunidades do contexto; "
            "informe o mesmo --artifacts-manifest usado no agent prepare "
            f"(ausentes: {', '.join(missing[:3])})"
        )
