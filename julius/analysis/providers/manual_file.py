"""Provedor de preenchimento manual: uma pessoa escreve o resultado.

Existe porque a análise contextual não deveria depender de um agente estar
disponível. As regras e a validação são as mesmas; o que muda é só a instrução
que acompanha o pacote, sem a mecânica de sessão de um agente específico.
"""

from __future__ import annotations

from pathlib import Path

from julius.analysis.guardrails import build_manual_instructions
from julius.analysis.providers.base import AnalysisProvider, Workspace
from julius.analysis.response_validator import ContextualAnalysis
from julius.analysis.workspace import collect_result, write_package


class ManualFileProvider(AnalysisProvider):
    name = "manual"

    def prepare(self, analysis, workspace: Workspace, *, top: int = 10) -> list[Path]:
        return write_package(
            analysis,
            workspace,
            top=top,
            instructions=lambda context, **_: build_manual_instructions(context),
        )

    def collect(self, workspace: Workspace) -> ContextualAnalysis:
        return collect_result(workspace)
