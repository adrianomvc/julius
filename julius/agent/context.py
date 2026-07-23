"""Pacote mínimo, auditável e sem credenciais enviado ao agente Devin."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from julius.pipeline import Analysis

_SECRET_PATTERNS = (
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*(['\"]?)[^,\s;'\"]+\2"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
)


def redact_secrets(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


@dataclass(frozen=True)
class AgentContext:
    schema_version: str
    account: dict
    scan_id: str
    constraints: dict
    opportunities: list[dict]
    graph_edges: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_agent_context(analysis: Analysis, *, top: int = 10) -> AgentContext:
    if top < 1 or top > 25:
        raise ValueError("top deve estar entre 1 e 25")
    opportunities = [
        _opportunity_context(analysis, opportunity)
        for opportunity in analysis.opportunities[:top]
    ]
    relevant_assets = {
        (item["asset_type"], item["asset_name"]) for item in opportunities
    }
    edges = [
        {
            "source": edge.source.id,
            "target": edge.target.id,
            "type": edge.type.value,
            "evidence": redact_secrets(edge.evidence),
            "confidence": edge.confidence,
        }
        for edge in analysis.graph.edges
        if (edge.source.kind, edge.source.name) in relevant_assets
        or (edge.target.kind, edge.target.name) in relevant_assets
    ]
    return AgentContext(
        schema_version="1.0",
        account={
            "id": analysis.account.account_id,
            "region": analysis.account.region,
            "period": analysis.account.period,
            "lookback_days": analysis.account.lookback_days,
        },
        scan_id=analysis.scan_id,
        constraints={
            "aws_access": "read-only",
            "allow_mutations": False,
            "allow_resource_deletion": False,
            "allow_email_send": False,
            "official_documentation_domain": "docs.aws.amazon.com",
            "deterministic_fields_are_immutable": [
                "estimated_gain",
                "difficulty_score",
                "confidence",
                "execution_priority",
                "strategic_priority",
            ],
        },
        opportunities=opportunities,
        graph_edges=edges,
    )


def _opportunity_context(analysis: Analysis, opportunity) -> dict:
    contextual_inputs: dict[str, object] = {}
    if opportunity.asset_type == "glue_job":
        job = analysis.account.job_by_name(opportunity.asset_name)
        if job and job.script_location:
            contextual_inputs["script_location"] = redact_secrets(job.script_location)
    elif opportunity.asset_type == "athena_query":
        query = next(
            (
                item
                for item in analysis.account.athena_queries
                if item.query_id == opportunity.asset_name
            ),
            None,
        )
        if query and query.statement:
            contextual_inputs["query_statement"] = redact_secrets(query.statement[:8000])
    return {
        "opportunity_id": opportunity.opportunity_id,
        "rule_id": opportunity.rule_id,
        "asset_type": opportunity.asset_type,
        "asset_name": opportunity.asset_name,
        "finding": redact_secrets(opportunity.finding),
        "recommended_action": redact_secrets(opportunity.recommended_action),
        "how_to_apply": redact_secrets(opportunity.how_to_apply),
        "how_to_validate": redact_secrets(opportunity.how_to_validate),
        "evidence": [redact_secrets(item) for item in opportunity.evidence],
        "missing_evidence": [
            redact_secrets(item) for item in opportunity.missing_evidence
        ],
        "risks": [redact_secrets(item) for item in opportunity.risks],
        "doc_links": opportunity.doc_links,
        "owner": opportunity.owner,
        "source_process": opportunity.source_process,
        "downstream_consumers": opportunity.downstream_consumers,
        "deterministic": {
            "estimated_gain": asdict(opportunity.estimated_gain),
            "difficulty_score": opportunity.difficulty_score,
            "confidence": opportunity.confidence,
            "execution_priority": opportunity.execution_priority,
            "strategic_priority": opportunity.strategic_priority,
            "bucket": opportunity.bucket,
        },
        "contextual_inputs": contextual_inputs,
    }
