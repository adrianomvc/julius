"""Pacote mínimo, auditável e sem credenciais enviado ao agente Devin."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from julius.collection.redaction import redact_secrets
from julius.knowledge.rules import families_without_evidence, missing_evidence
from julius.pipeline import Analysis


@dataclass(frozen=True)
class AgentContext:
    schema_version: str
    # A versão do briefing que acompanha este pacote. Sem ela não dá para dizer
    # qual pergunta produziu qual julgamento, e comparar precisão entre dois
    # prompts diferentes seria comparar duas perguntas diferentes.
    prompt_version: str
    account: dict
    scan_id: str
    constraints: dict
    # Quanto do portfólio este pacote representa. Sem isso o recorte de `top`
    # fica invisível e as oportunidades não enviadas parecem não existir.
    portfolio: dict
    opportunities: list[dict]
    graph_edges: list[dict] = field(default_factory=list)
    technical_artifacts: list[dict] = field(default_factory=list)
    # Hipóteses a julgar, não achados a executar.
    signals: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_agent_context(
    analysis: Analysis,
    *,
    top: int = 10,
    technical_artifacts: list[dict] | None = None,
) -> AgentContext:
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
    from julius.analysis.guardrails import PROMPT_VERSION

    return AgentContext(
        schema_version="1.3",
        prompt_version=PROMPT_VERSION,
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
            "collection_status": analysis.account.collection_status,
            "collection_health": [
                {
                    "source": item.source,
                    "status": item.status,
                    "coverage": item.coverage,
                    "error_category": item.error_category,
                    "impact": item.impact,
                }
                for item in analysis.account.collection_health
            ],
            # Silêncio explicado: uma família cujo inventário chegou vazio não
            # produziu nada porque não teve o que ler — não porque está tudo bem.
            "rule_families_without_evidence": [
                {
                    "service": family.service,
                    "name": family.name,
                    "requires": list(family.requires),
                    "reason": missing_evidence(analysis.account, family),
                }
                for family in families_without_evidence(analysis.account)
            ],
            "official_documentation_domain": "docs.aws.amazon.com",
            "deterministic_fields_are_immutable": [
                "estimated_gain",
                "difficulty_score",
                "confidence",
                "execution_priority",
                "strategic_priority",
            ],
        },
        portfolio={
            "total_opportunities": len(analysis.opportunities),
            "analyzed": len(opportunities),
            "selection": f"top_{top}_by_execution_priority",
        },
        opportunities=opportunities,
        graph_edges=edges,
        technical_artifacts=technical_artifacts or [],
        signals=_signals_context(analysis, technical_artifacts or []),
    )


def _signals_context(analysis: Analysis, technical_artifacts: list[dict]) -> list[dict]:
    """Ancora cada sinal no artefato que o responde, quando existe um.

    O sinal de código já nasce com o hash do script, porque a regra estática o
    tinha em mãos. O sinal de configuração não: quem o emitiu olhou o
    inventário, não o arquivo. Mas a pergunta que ele faz — a ASL tolera
    reexecução? — só se responde lendo a definição, então o hash é ligado aqui,
    onde o bundle de artefatos é conhecido. É esse hash que o validador vai
    exigir de volta no veredito.
    """
    by_asset = {
        str(item.get("asset_name") or ""): str(item.get("sha256") or "")
        for item in technical_artifacts
        if item.get("sha256")
    }
    out: list[dict] = []
    for signal in analysis.signals:
        payload = signal.to_dict()
        payload["observation"] = redact_secrets(signal.observation)
        payload["question"] = redact_secrets(signal.question)
        if not payload.get("artifact_sha256"):
            payload["artifact_sha256"] = by_asset.get(signal.asset_name, "")
        out.append(payload)
    return out


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
