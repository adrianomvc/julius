"""Escrita e leitura do pacote de análise, comum a todos os provedores.

Nenhuma integração por rede: o Julius escreve contexto, schema e instruções em
disco, e lê de volta um arquivo de resultado. Tudo que entrou e tudo que saiu
fica auditável.

Toda falha de leitura vira `AgentOutputError`. É o que sustenta o contrato de
substituição: quem chama trata um tipo de erro, não os de cada provedor.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from julius.analysis.context_builder import AgentContext, build_agent_context
from julius.analysis.providers.base import Workspace
from julius.analysis.response_validator import (
    ANALYSIS_OUTPUT_SCHEMA,
    AgentOutputError,
    ContextualAnalysis,
    validate_agent_output,
)
from julius.pipeline import Analysis

REQUIRED_CONTEXT_KEYS = {
    "schema_version",
    "prompt_version",
    "account",
    "scan_id",
    "constraints",
    "portfolio",
    "opportunities",
    "graph_edges",
    "technical_artifacts",
    "signals",
}


def write_package(
    analysis: Analysis,
    workspace: Workspace,
    *,
    top: int = 25,
    instructions: Callable[..., str],
    artifacts_manifest: str | Path | None = None,
) -> list[Path]:
    """Escreve contexto, schema e as instruções que o provedor definir."""
    technical_artifacts = (
        load_technical_artifacts(artifacts_manifest, analysis.account.account_id)
        if artifacts_manifest
        else []
    )
    context = build_agent_context(
        analysis, top=top, technical_artifacts=technical_artifacts
    )
    workspace.context.write_text(
        json.dumps(context.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    workspace.schema.write_text(
        json.dumps(ANALYSIS_OUTPUT_SCHEMA, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    workspace.instructions.write_text(
        instructions(
            context,
            context_file=workspace.context.as_posix(),
            schema_file=workspace.schema.as_posix(),
            result_file=workspace.result.as_posix(),
        ),
        encoding="utf-8",
    )
    return [workspace.context, workspace.schema, workspace.instructions]


def collect_result(workspace: Workspace) -> ContextualAnalysis:
    """Lê e valida o resultado deixado pelo provedor."""
    return validate_result_file(workspace.context, workspace.result)


def load_agent_context(path: str | Path) -> AgentContext:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentOutputError(
            f"contexto Julius ilegível: {type(exc).__name__}"
        ) from exc
    if not isinstance(raw, dict) or set(raw) != REQUIRED_CONTEXT_KEYS:
        raise AgentOutputError("contexto Julius inválido")
    if not isinstance(raw["account"], dict) or not isinstance(
        raw["opportunities"], list
    ):
        raise AgentOutputError("contexto Julius inválido")
    return AgentContext(**raw)


def validate_result_file(
    context_path: str | Path, result_path: str | Path
) -> ContextualAnalysis:
    context = load_agent_context(context_path)
    try:
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentOutputError(
            f"resultado da análise ilegível: {type(exc).__name__}"
        ) from exc
    allowed_ids = {str(item["opportunity_id"]) for item in context.opportunities}
    return validate_agent_output(
        payload,
        account=str(context.account["id"]),
        scan_id=context.scan_id,
        allowed_opportunity_ids=allowed_ids,
        expected_signals={
            (str(item["rule_id"]), str(item["asset_name"])): str(
                item.get("artifact_sha256") or ""
            )
            for item in context.signals
        },
        known_artifact_hashes={
            str(item["sha256"])
            for item in context.technical_artifacts
            if item.get("sha256")
        },
        # Regra que já existe não é lacuna de catálogo.
        known_rule_ids={str(item["rule_id"]) for item in context.opportunities}
        | {str(item["rule_id"]) for item in context.signals},
    )


def write_validated_result(
    analysis: ContextualAnalysis, output_path: str | Path
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(analysis), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_technical_artifacts(
    manifest_path: str | Path, account_id: str
) -> list[dict]:
    path = Path(manifest_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != "1.0"
        or raw.get("account_id") != account_id
        or raw.get("read_only") is not True
        or not isinstance(raw.get("artifacts"), list)
    ):
        raise ValueError(
            "manifesto de artefatos não pertence à conta ou não é read-only"
        )
    references = []
    root = path.parent.resolve()
    for item in raw["artifacts"]:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ValueError("entrada inválida no manifesto de artefatos")
        artifact_path = (root / item["file"]).resolve()
        if root not in artifact_path.parents or not artifact_path.is_file():
            raise ValueError(
                f"arquivo técnico ausente ou fora do bundle: {item['file']}"
            )
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        expected = str(item.get("sha256") or "")
        if expected and digest != expected:
            raise ValueError(f"hash divergente no artefato técnico: {item['file']}")
        references.append(
            {
                "kind": item.get("kind"),
                "asset_name": item.get("asset_name"),
                "source": item.get("source"),
                "sha256": item.get("sha256"),
                "truncated": bool(item.get("truncated")),
                "path": artifact_path.as_posix(),
            }
        )
    return references


def prepare_agent_workspace(
    analysis: Analysis,
    output_dir: str | Path,
    *,
    top: int = 25,
    artifacts_manifest: str | Path | None = None,
) -> tuple[AgentContext, list[Path]]:
    """Compatibilidade: o caminho Devin, que era a única opção."""
    from julius.analysis.guardrails import build_devin_prompt

    workspace = Workspace.at(output_dir)
    files = write_package(
        analysis,
        workspace,
        top=top,
        instructions=build_devin_prompt,
        artifacts_manifest=artifacts_manifest,
    )
    return load_agent_context(workspace.context), files
