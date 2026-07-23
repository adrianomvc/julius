"""Artefatos locais usados pelo Devin, sem integração com API externa."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from julius.agent.context import AgentContext, build_agent_context
from julius.agent.prompt import build_devin_prompt
from julius.agent.schema import (
    DEVIN_OUTPUT_SCHEMA,
    ContextualAnalysis,
    validate_agent_output,
)
from julius.pipeline import Analysis


def prepare_agent_workspace(
    analysis: Analysis,
    output_dir: str | Path,
    *,
    top: int = 10,
) -> tuple[AgentContext, list[Path]]:
    """Gera contexto, schema e instruções para a sessão Devin corrente."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    context = build_agent_context(analysis, top=top)
    context_path = output / "context.json"
    schema_path = output / "output-schema.json"
    instructions_path = output / "instructions.md"
    result_path = output / "result.json"
    context_path.write_text(
        json.dumps(context.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    schema_path.write_text(
        json.dumps(DEVIN_OUTPUT_SCHEMA, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    instructions_path.write_text(
        build_devin_prompt(
            context,
            context_file=context_path.as_posix(),
            schema_file=schema_path.as_posix(),
            result_file=result_path.as_posix(),
        ),
        encoding="utf-8",
    )
    return context, [context_path, schema_path, instructions_path]


def load_agent_context(path: str | Path) -> AgentContext:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "account",
        "scan_id",
        "constraints",
        "opportunities",
        "graph_edges",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("contexto Julius inválido")
    if not isinstance(raw["account"], dict) or not isinstance(
        raw["opportunities"], list
    ):
        raise ValueError("contexto Julius inválido")
    return AgentContext(**raw)


def validate_result_file(
    context_path: str | Path,
    result_path: str | Path,
) -> ContextualAnalysis:
    context = load_agent_context(context_path)
    payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    allowed_ids = {
        str(item["opportunity_id"]) for item in context.opportunities
    }
    return validate_agent_output(
        payload,
        account=str(context.account["id"]),
        scan_id=context.scan_id,
        allowed_opportunity_ids=allowed_ids,
    )


def write_validated_result(
    analysis: ContextualAnalysis,
    output_path: str | Path,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(analysis), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
