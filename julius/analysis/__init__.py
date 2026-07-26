"""Análise contextual: o que um provedor acrescenta ao scan determinístico.

O Julius decide sozinho o que é oportunidade, quanto vale e em que ordem
executar. Um provedor só acrescenta contexto — diagnóstico, passos, riscos — e
nunca altera número, prioridade ou conjunto de achados. A validação da resposta
é o que garante isso, e vale igual para qualquer provedor.
"""

from julius.analysis.context_builder import AgentContext, build_agent_context
from julius.analysis.guardrails import PROMPT_VERSION, RULES, build_devin_prompt
from julius.analysis.providers import (
    PROVIDERS,
    AnalysisProvider,
    DevinProvider,
    ManualFileProvider,
    Workspace,
)
from julius.analysis.response_validator import (
    DEVIN_OUTPUT_SCHEMA,
    AgentOutputError,
    ContextualAnalysis,
    ContextualRecommendation,
    DocumentationReference,
    EvidenceRef,
    SignalVerdict,
    UncoveredFinding,
    validate_agent_output,
)
from julius.analysis.rule_candidates import append_candidates
from julius.analysis.workspace import (
    load_agent_context,
    prepare_agent_workspace,
    validate_result_file,
    write_validated_result,
)

__all__ = [
    "DEVIN_OUTPUT_SCHEMA",
    "PROMPT_VERSION",
    "PROVIDERS",
    "RULES",
    "AgentContext",
    "AgentOutputError",
    "AnalysisProvider",
    "ContextualAnalysis",
    "ContextualRecommendation",
    "DevinProvider",
    "DocumentationReference",
    "EvidenceRef",
    "ManualFileProvider",
    "SignalVerdict",
    "UncoveredFinding",
    "Workspace",
    "append_candidates",
    "build_agent_context",
    "build_devin_prompt",
    "load_agent_context",
    "prepare_agent_workspace",
    "validate_agent_output",
    "validate_result_file",
    "write_validated_result",
]
