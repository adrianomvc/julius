"""Contrato estruturado da análise contextual produzida pelo Devin."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


DEVIN_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "account",
        "scan_id",
        "executive_summary",
        "implementation_order",
        "recommendations",
    ],
    "properties": {
        "account": {"type": "string"},
        "scan_id": {"type": "string"},
        "executive_summary": {"type": "string"},
        "implementation_order": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "opportunity_id",
                    "contextual_diagnosis",
                    "recommendation",
                    "implementation_steps",
                    "validation_steps",
                    "dependencies",
                    "conflicts",
                    "risks",
                    "documentation",
                    "assumptions",
                    "missing_evidence",
                ],
                "properties": {
                    "opportunity_id": {"type": "string"},
                    "contextual_diagnosis": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "implementation_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "validation_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "conflicts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "risks": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "documentation": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["title", "url", "relevance"],
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "relevance": {"type": "string"},
                            },
                        },
                    },
                    "assumptions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "missing_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

_LIST_FIELDS = (
    "implementation_steps",
    "validation_steps",
    "dependencies",
    "conflicts",
    "risks",
    "assumptions",
    "missing_evidence",
)


class AgentOutputError(ValueError):
    """Saída do agente não respeita o contrato ou as evidências do scan."""


@dataclass(frozen=True)
class DocumentationReference:
    title: str
    url: str
    relevance: str


@dataclass(frozen=True)
class ContextualRecommendation:
    opportunity_id: str
    contextual_diagnosis: str
    recommendation: str
    implementation_steps: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    documentation: list[DocumentationReference] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ContextualAnalysis:
    account: str
    scan_id: str
    executive_summary: str
    implementation_order: list[str]
    recommendations: list[ContextualRecommendation]


def validate_agent_output(
    payload: object,
    *,
    account: str,
    scan_id: str,
    allowed_opportunity_ids: set[str],
) -> ContextualAnalysis:
    """Valida estrutura, vínculo ao scan e links oficiais antes de aceitar a IA."""
    if not isinstance(payload, dict):
        raise AgentOutputError("saída estruturada deve ser um objeto JSON")
    expected_top = {
        "account",
        "scan_id",
        "executive_summary",
        "implementation_order",
        "recommendations",
    }
    if set(payload) != expected_top:
        raise AgentOutputError("campos de topo ausentes ou não permitidos")
    if payload.get("account") != account or payload.get("scan_id") != scan_id:
        raise AgentOutputError("saída não pertence à conta e ao scan solicitados")
    summary = payload.get("executive_summary")
    order = payload.get("implementation_order")
    recommendations = payload.get("recommendations")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentOutputError("executive_summary ausente")
    if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
        raise AgentOutputError("implementation_order inválida")
    if len(order) != len(set(order)) or not set(order) <= allowed_opportunity_ids:
        raise AgentOutputError("implementation_order contém IDs inválidos ou duplicados")
    if not isinstance(recommendations, list):
        raise AgentOutputError("recommendations inválida")

    parsed: list[ContextualRecommendation] = []
    seen: set[str] = set()
    expected_recommendation = {
        "opportunity_id",
        "contextual_diagnosis",
        "recommendation",
        *_LIST_FIELDS,
        "documentation",
    }
    for raw in recommendations:
        if not isinstance(raw, dict):
            raise AgentOutputError("recomendação deve ser um objeto")
        if set(raw) != expected_recommendation:
            raise AgentOutputError("campos da recomendação ausentes ou não permitidos")
        opportunity_id = raw.get("opportunity_id")
        if opportunity_id not in allowed_opportunity_ids or opportunity_id in seen:
            raise AgentOutputError(f"opportunity_id inválido ou duplicado: {opportunity_id}")
        seen.add(opportunity_id)
        diagnosis = raw.get("contextual_diagnosis")
        recommendation = raw.get("recommendation")
        if not isinstance(diagnosis, str) or not isinstance(recommendation, str):
            raise AgentOutputError("diagnóstico e recomendação devem ser texto")
        list_values: dict[str, list[str]] = {}
        for key in _LIST_FIELDS:
            value = raw.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise AgentOutputError(f"{key} deve ser uma lista de textos")
            list_values[key] = value
        docs_raw = raw.get("documentation")
        if not isinstance(docs_raw, list):
            raise AgentOutputError("documentation deve ser uma lista")
        docs: list[DocumentationReference] = []
        for item in docs_raw:
            if not isinstance(item, dict):
                raise AgentOutputError("referência de documentação inválida")
            if set(item) != {"title", "url", "relevance"}:
                raise AgentOutputError("campos de documentação ausentes ou não permitidos")
            title, url, relevance = (
                item.get("title"),
                item.get("url"),
                item.get("relevance"),
            )
            if not all(isinstance(value, str) and value.strip() for value in (title, url, relevance)):
                raise AgentOutputError("referência de documentação incompleta")
            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or parsed_url.hostname != "docs.aws.amazon.com":
                raise AgentOutputError(f"documentação fora do domínio oficial permitido: {url}")
            docs.append(DocumentationReference(title, url, relevance))
        parsed.append(
            ContextualRecommendation(
                opportunity_id=opportunity_id,
                contextual_diagnosis=diagnosis,
                recommendation=recommendation,
                documentation=docs,
                **list_values,
            )
        )
    if seen != allowed_opportunity_ids or set(order) != seen:
        raise AgentOutputError(
            "a análise e a ordem devem cobrir todas as oportunidades do contexto"
        )
    return ContextualAnalysis(
        account=account,
        scan_id=scan_id,
        executive_summary=summary,
        implementation_order=order,
        recommendations=parsed,
    )
