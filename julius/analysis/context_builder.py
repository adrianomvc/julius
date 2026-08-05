"""Pacote mínimo, auditável e sem credenciais entregue ao provedor de análise."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from julius.collection.redaction import redact_secrets
from julius.knowledge.rules import families_without_evidence, missing_evidence
from julius.pipeline import Analysis

#: Os campos que a análise contextual recebe resolvidos e não pode recalcular.
#:
#: Estava embutido no dicionário de `constraints`, e a Skill repetia a mesma
#: lista em prosa. Duas cópias da mesma regra divergem em silêncio: o contexto
#: dizia cinco campos e o texto dizia "nunca sobrescreva um campo
#: determinístico", sem enumerar qual. Aqui é uma constante, o contexto a
#: publica e o artefato da Skill a recebe gerada.
DETERMINISTIC_FIELDS = (
    "estimated_gain",
    "difficulty_score",
    "confidence",
    "execution_priority",
    "strategic_priority",
)


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
    # Teto de **famílias**, não de achados. O catálogo tem 23, então 25 não morde
    # na prática — a flag deixa de recortar o portfólio e vira escape hatch para
    # quem quiser pacote pequeno de propósito.
    top: int = 25,
    technical_artifacts: list[dict] | None = None,
) -> AgentContext:
    if top < 1 or top > 25:
        raise ValueError("top deve estar entre 1 e 25")
    representantes = _one_per_family(analysis.opportunities)[:top]
    opportunities = [
        _opportunity_context(analysis, opportunity, irmaos)
        for opportunity, irmaos in representantes
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
                    "iam_gaps": [
                        {
                            "service": gap.service,
                            "operation": gap.operation,
                            "iam_action": gap.iam_action,
                            "category": gap.category,
                            "affected_resources": gap.affected_resources,
                            "examples": gap.examples,
                        }
                        for gap in item.iam_gaps
                    ],
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
            "deterministic_fields_are_immutable": list(DETERMINISTIC_FIELDS),
        },
        portfolio={
            "total_opportunities": len(analysis.opportunities),
            "analyzed": len(opportunities),
            # Quantos achados as respostas alcançam, que não é o número de
            # perguntas. Sem esta linha o pacote diria "onze de trinta" quando os
            # trinta são cobertos, e o silêncio sobre os dezenove viraria leitura
            # de que ninguém olhou para eles.
            "covered": sum(1 + len(irmaos) for _, irmaos in representantes),
            "selection": f"one_per_remediation_family_max_{top}",
        },
        opportunities=opportunities,
        graph_edges=edges,
        technical_artifacts=technical_artifacts or [],
        signals=_signals_context(analysis, technical_artifacts or []),
    )


#: Que tipo de artefato responde a pergunta de cada tipo de ativo. Sem isto o
#: casamento era só pelo nome, e nome não é único entre tipos: uma máquina de
#: estados e um job Glue podem se chamar igual, e o `dict` guardava o último a
#: entrar. O sinal receberia o hash do artefato errado, o validador exigiria esse
#: hash de volta, e a análise concluiria sobre um arquivo que não é o dela.
_ARTIFACT_KIND_BY_ASSET = {
    "glue_job": "glue_script",
    "athena_query": "athena_sql",
    "state_machine": "stepfunctions_asl",
}


def _artifact_kind(asset_type: str) -> str:
    if asset_type.startswith("sagemaker_"):
        return "sagemaker_script"
    return _ARTIFACT_KIND_BY_ASSET.get(asset_type, "")


def _signals_context(analysis: Analysis, technical_artifacts: list[dict]) -> list[dict]:
    """Ancora cada sinal no artefato que o responde, quando existe um.

    O sinal de código já nasce com o hash do script, porque a regra estática o
    tinha em mãos. O sinal de configuração não: quem o emitiu olhou o
    inventário, não o arquivo. Mas a pergunta que ele faz — a ASL tolera
    reexecução? — só se responde lendo a definição, então o hash é ligado aqui,
    onde o bundle de artefatos é conhecido. É esse hash que o validador vai
    exigir de volta no veredito.
    """
    by_kind_asset = {
        (str(item.get("kind") or ""), str(item.get("asset_name") or "")): str(
            item.get("sha256") or ""
        )
        for item in technical_artifacts
        if item.get("sha256")
    }
    out: list[dict] = []
    for signal in analysis.signals:
        payload = signal.to_dict()
        payload["observation"] = redact_secrets(signal.observation)
        payload["question"] = redact_secrets(signal.question)
        if not payload.get("artifact_sha256"):
            payload["artifact_sha256"] = by_kind_asset.get(
                (_artifact_kind(signal.asset_type), signal.asset_name), ""
            )
        out.append(payload)
    return out


def _one_per_family(opportunities: list) -> list[tuple]:
    """Um representante por família de remediação, com os irmãos que ele alcança.

    O recorte era `[:top]` sobre a lista ordenada por prioridade de execução, e o
    efeito era duplo. Quatro `GLUE-TIMEOUT-EXCESSIVE` são a **mesma correção** e
    gastavam quatro das dez vagas para dizer a mesma coisa; e o que ficasse na
    posição onze nunca era enriquecido, com a fronteira invisível para quem lê.

    Agrupando pela família — que `remediation.classify_opportunities` já carimbou
    em todo achado — o pacote encolhe e cobre tudo: trinta achados viram onze
    perguntas na conta de exemplo.

    O representante é o primeiro da família na lista, e ela vem ordenada por
    `scoring.priority.ranking_key` — cujo **primeiro** critério é entrar no
    portfólio, não a prioridade de execução. O efeito é que a análise recebe o
    achado com cifra medida da família, e não o de maior prioridade: em
    `failure_waste`, o representante tem prioridade 6 e três irmãos têm 63.

    É a escolha certa, e vale dizer por quê em vez de deixar por conta do acaso.
    Quem entra no portfólio tem baseline resolvido e evidência completa; irmão
    bloqueado é bloqueado justamente por faltar a medição. Analisar o bloqueado
    daria à IA menos material sobre a mesma correção.

    Achado cuja regra o catálogo não conhece vira família de um, em vez de cair
    fora do pacote por não estar classificado.
    """
    por_familia: dict[str, list] = {}
    for opportunity in opportunities:
        # Sem família, a chave é o próprio id: garante entrada própria e nunca
        # funde dois achados só porque ambos estão sem classificação.
        chave = opportunity.remediation_family or f"\0{opportunity.opportunity_id}"
        por_familia.setdefault(chave, []).append(opportunity)
    return [(grupo[0], grupo[1:]) for grupo in por_familia.values()]


def _opportunity_context(analysis: Analysis, opportunity, siblings=()) -> dict:
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
        "remediation_family": opportunity.remediation_family,
        # Os outros achados que esta mesma resposta vai alcançar. Sem a lista, a
        # análise escreveria "reduzir para 12 workers" — verdade sobre um job e
        # mentira sobre os outros três. Com ela, dá para escrever o passo para a
        # família e nomear o que varia por ativo.
        "applies_to": [
            {
                "opportunity_id": irmao.opportunity_id,
                "asset_name": irmao.asset_name,
                "rule_id": irmao.rule_id,
            }
            for irmao in siblings
        ],
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
