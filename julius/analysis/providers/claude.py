"""Provedor Claude Code: mesmo pacote, mesma validação, outro host.

Este arquivo é curto, e o tamanho dele é o resultado que a inversão fonte →
artefato produziu. Antes de `docs/ai/` ser canônico, suportar um segundo host
significava copiar um `SKILL.md` de 296 linhas com o procedimento de sessão do
Devin dentro dele. Agora significa uma linha em `HOSTS`, um bloco em
`docs/ai/hosts/` e esta classe.

O que de fato difere entre um host e outro é onde a Skill está instalada. Regras,
contrato, perguntas por tipo de ativo e critérios são idênticos, porque vêm do
mesmo corpo canônico — e `tests/test_skill_registry_drift.py` cobra essa
igualdade em vez de confiar nela.

Nenhuma integração por rede, igual ao provedor Devin: o Julius escreve contexto,
schema e instruções em disco e lê de volta um arquivo de resultado. É o que
mantém a análise auditável, e é o que `test_provider_has_no_mutation_capability`
verifica.
"""

from __future__ import annotations

from pathlib import Path

from julius.analysis.guardrails import build_agent_prompt
from julius.analysis.providers.base import AnalysisProvider, Workspace
from julius.analysis.response_validator import ContextualAnalysis
from julius.analysis.workspace import collect_result, write_package


class ClaudeProvider(AnalysisProvider):
    name = "Claude"

    def prepare(self, analysis, workspace: Workspace, *, top: int = 10) -> list[Path]:
        return write_package(
            analysis,
            workspace,
            top=top,
            instructions=lambda context, **kwargs: build_agent_prompt(
                context, host="claude", **kwargs
            ),
        )

    def collect(self, workspace: Workspace) -> ContextualAnalysis:
        return collect_result(workspace)
