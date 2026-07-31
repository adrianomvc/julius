"""KPIs de produto — medem se o Julius é útil, não só se roda.

- actionability: % de oportunidades com ativo, ação, validação e responsável.
- financial_coverage: quanto do custo do serviço foi atribuído a ativos específicos.
- precision_at_10: das 10 principais (por execução), quantas foram validadas por
  especialistas (requer rótulos; None se ausentes).
- false_positive_rate_at_10: proporção das revisadas no Top 10 classificadas
  como falso positivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.models import Account
from julius.findings.opportunity import Opportunity
from julius.scoring.priority import ranking_key

# serviço AWS por tipo de ativo (para cobertura financeira).
_SERVICE_OF = {
    "glue_job": "AWS Glue",
    "glue_session": "AWS Glue",
    "athena_query": "Amazon Athena",
    "athena_capacity_reservation": "Amazon Athena",
    "state_machine": "AWS Step Functions",
    "sagemaker_app": "Amazon SageMaker",
    "sagemaker_space": "Amazon SageMaker",
    "sagemaker_domain": "Amazon SageMaker",
    "sagemaker_endpoint": "Amazon SageMaker",
    "sagemaker_notebook": "Amazon SageMaker",
    "sagemaker_job": "Amazon SageMaker",
    "sagemaker_feature_group": "Amazon SageMaker",
    "redshift_cluster": "Amazon Redshift",
    "s3": "Amazon S3",
    "s3_prefix": "Amazon S3",
}


@dataclass
class ProductKPIs:
    total: int
    actionable: int
    actionability_rate: float
    ownership_rate: float
    coverage_by_service: dict[str, float] = field(default_factory=dict)
    coverage_overall: float = 0.0
    precision_at_10: float | None = None
    reviewed_at_10: int = 0
    false_positives_at_10: int = 0
    false_positive_rate_at_10: float | None = None
    # Precisão separada por quem produziu o achado: `rule` para a regra
    # determinística, `ai_confirmed` para o sinal que a análise contextual
    # sustentou. Sem separar, um erro de julgamento da IA some na média das
    # regras — e a pergunta sobre qual camada acerta mais fica sem resposta.
    precision_by_origin: dict[str, float] = field(default_factory=dict)
    reviewed_by_origin: dict[str, int] = field(default_factory=dict)
    false_positive_rate_by_origin: dict[str, float] = field(default_factory=dict)
    identified_monthly: float = 0.0
    committed_monthly: float = 0.0
    realized_monthly: float = 0.0
    realization_rate: float | None = None
    detected_to_accepted_days: float | None = None
    accepted_to_implemented_days: float | None = None
    implemented_to_validated_days: float | None = None


def _is_actionable(o: Opportunity) -> bool:
    return bool(
        o.actionable
        and o.asset_name
        and o.recommended_action
        and o.how_to_validate
        and (o.owner or o.actor)
    )


def _precision_by_origin(
    opportunities: list[Opportunity], labels: dict[str, bool] | None
) -> tuple[dict[str, float], dict[str, int], dict[str, float]]:
    """Agrupa os rótulos humanos pela camada que produziu cada achado.

    Ao contrário de `precision_at_10`, aqui não há corte no Top 10: um achado
    promovido de sinal nasce bloqueado e com economia zero, então quase nunca
    chegaria ao topo do ranking — e o julgamento da IA ficaria sem medida
    justamente onde ele acontece.
    """
    if not labels:
        return {}, {}, {}
    judged: dict[str, list[bool]] = {}
    for opportunity in opportunities:
        label = labels.get(opportunity.opportunity_id)
        if label is None:
            continue
        judged.setdefault(opportunity.origin, []).append(label)
    precision = {
        origin: round(sum(1 for value in values if value) / len(values), 3)
        for origin, values in judged.items()
    }
    reviewed = {origin: len(values) for origin, values in judged.items()}
    false_positive_rate = {
        origin: round(sum(1 for value in values if not value) / len(values), 3)
        for origin, values in judged.items()
    }
    return precision, reviewed, false_positive_rate


def compute_kpis(
    account: Account,
    opportunities: list[Opportunity],
    labels: dict[str, bool] | None = None,
    *,
    realized_monthly: float = 0.0,
    detected_to_accepted_days: float | None = None,
    accepted_to_implemented_days: float | None = None,
    implemented_to_validated_days: float | None = None,
) -> ProductKPIs:
    total = len(opportunities)
    actionable = sum(1 for o in opportunities if _is_actionable(o))
    with_owner = sum(1 for o in opportunities if o.owner or o.actor)

    # Cobertura financeira: custo-base atribuído por serviço ÷ custo do serviço.
    in_scope_services = set(_SERVICE_OF.values())
    cost_by_service = {
        s.name: s.monthly_cost
        for s in account.services
        if s.name in in_scope_services
    }
    attributed: dict[str, float] = {}
    for o in opportunities:
        svc = _SERVICE_OF.get(o.asset_type)
        if svc and o.estimation:
            attributed[svc] = attributed.get(svc, 0.0) + o.estimation.baseline_cost

    coverage_by_service: dict[str, float] = {}
    for svc, cost in cost_by_service.items():
        if cost > 0:
            coverage_by_service[svc] = round(
                min(1.0, attributed.get(svc, 0.0) / cost), 3
            )

    analyzable_total = sum(cost_by_service.values())
    coverage_overall = (
        round(min(1.0, sum(attributed.values()) / analyzable_total), 3)
        if analyzable_total
        else 0.0
    )

    precision = None
    reviewed = 0
    false_positives = 0
    false_positive_rate = None
    if labels:
        # Mesma ordem da tabela do relatório. Medir precisão sobre um Top 10
        # ordenado de outro jeito seria avaliar uma lista que ninguém viu.
        top10 = sorted(opportunities, key=ranking_key, reverse=True)[:10]
        judged = [labels.get(o.opportunity_id) for o in top10 if o.opportunity_id in labels]
        if judged:
            reviewed = len(judged)
            false_positives = sum(1 for value in judged if not value)
            precision = round(sum(1 for v in judged if v) / len(judged), 3)
            false_positive_rate = round(false_positives / reviewed, 3)

    by_origin = _precision_by_origin(opportunities, labels)

    identified_monthly = sum(
        opportunity.portfolio_gain.monthly_expected
        for opportunity in opportunities
        if opportunity.include_in_portfolio
    )
    committed_monthly = sum(
        opportunity.portfolio_gain.monthly_expected
        for opportunity in opportunities
        if opportunity.include_in_portfolio
        and opportunity.status in {"accepted", "planned", "implemented", "validated"}
    )

    return ProductKPIs(
        total=total,
        actionable=actionable,
        actionability_rate=round(actionable / total, 3) if total else 0.0,
        ownership_rate=round(with_owner / total, 3) if total else 0.0,
        coverage_by_service=coverage_by_service,
        coverage_overall=coverage_overall,
        precision_at_10=precision,
        reviewed_at_10=reviewed,
        false_positives_at_10=false_positives,
        false_positive_rate_at_10=false_positive_rate,
        precision_by_origin=by_origin[0],
        reviewed_by_origin=by_origin[1],
        false_positive_rate_by_origin=by_origin[2],
        identified_monthly=round(identified_monthly, 2),
        committed_monthly=round(committed_monthly, 2),
        realized_monthly=round(realized_monthly, 2),
        realization_rate=round(realized_monthly / committed_monthly, 3)
        if committed_monthly > 0
        else None,
        detected_to_accepted_days=detected_to_accepted_days,
        accepted_to_implemented_days=accepted_to_implemented_days,
        implemented_to_validated_days=implemented_to_validated_days,
    )
