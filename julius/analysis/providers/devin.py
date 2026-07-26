"""Provedor Devin: workspace local, sem chamada a API externa.

O Julius não integra com o Devin por rede. Ele escreve um pacote — contexto,
schema e instruções — que a sessão Devin lê e responde num arquivo. Isso mantém
a análise auditável: tudo que entrou e tudo que saiu está em disco.
"""

from __future__ import annotations

from pathlib import Path

from julius.analysis.guardrails import build_devin_prompt
from julius.analysis.providers.base import AnalysisProvider, Workspace
from julius.analysis.response_validator import ContextualAnalysis
from julius.analysis.workspace import collect_result, write_package


class DevinProvider(AnalysisProvider):
    name = "Devin"

    def prepare(self, analysis, workspace: Workspace, *, top: int = 10) -> list[Path]:
        return write_package(
            analysis,
            workspace,
            top=top,
            instructions=build_devin_prompt,
        )

    def collect(self, workspace: Workspace) -> ContextualAnalysis:
        return collect_result(workspace)
