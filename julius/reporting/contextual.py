"""Anexa somente o enriquecimento validado do Devin ao relatório."""

from __future__ import annotations

from dataclasses import asdict

from julius.analysis.response_validator import ContextualAnalysis
from julius.reporting.view_models import OpportunityVM, ReportViewModel


def attach_contextual_analysis(
    vm: ReportViewModel,
    analysis: ContextualAnalysis,
) -> ReportViewModel:
    if analysis.account != vm.account_id or analysis.scan_id != vm.scan_id:
        raise ValueError("análise contextual não pertence ao relatório")
    recommendations = {
        item.opportunity_id: item for item in analysis.recommendations
    }
    # O pacote leva um representante por família, então a resposta chega com o
    # id dele e precisa alcançar os irmãos — é o que faz trinta achados serem
    # cobertos por onze perguntas em vez de dez de trinta ficarem cobertos.
    #
    # O ativo de origem viaja junto. Sem ele o cartão do irmão afirmaria que
    # aquele passo foi apurado ali, e não foi.
    por_familia: dict[str, tuple[object, str]] = {}
    for opportunity in vm.table:
        contextual = recommendations.get(opportunity.id)
        if contextual is not None and opportunity.remediation_family:
            por_familia.setdefault(
                opportunity.remediation_family, (contextual, opportunity.asset)
            )
    collections = (
        vm.focus,
        vm.table,
        vm.do_now,
        vm.plan,
        vm.monitor,
        vm.investigate,
    )
    for collection in collections:
        for opportunity in collection:
            contextual = recommendations.get(opportunity.id)
            if contextual is not None:
                _attach_opportunity(opportunity, contextual)
                continue
            herdado = por_familia.get(opportunity.remediation_family)
            if herdado is not None:
                irmao, origem = herdado
                _attach_opportunity(opportunity, irmao)
                opportunity.ai_derived_from = origem

    titles = {opportunity.id: opportunity.title for opportunity in vm.table}
    vm.ai_summary = analysis.executive_summary
    vm.ai_implementation_order = [
        {
            "position": position,
            "opportunity_id": opportunity_id,
            "title": titles.get(opportunity_id, opportunity_id),
        }
        for position, opportunity_id in enumerate(
            analysis.implementation_order, start=1
        )
    ]
    vm.ai_recommendations = [asdict(item) for item in analysis.recommendations]
    # O pacote enviado ao provedor é um recorte do portfólio; dizer qual evita
    # que o silêncio sobre o resto seja lido como ausência de problema.
    vm.ai_coverage = {
        # Perguntas feitas e achados alcançados são números diferentes desde que
        # o pacote passou a ser por família. Publicar só o primeiro diria "onze
        # de trinta" com os trinta cobertos, e o silêncio sobre os dezenove seria
        # lido como ninguém ter olhado para eles.
        "answers": len(analysis.recommendations),
        "covered": sum(1 for item in vm.table if item.ai_diagnosis),
        "total": len(vm.table),
    }
    # O sinal descartado continua no result.json para auditoria, mas não ocupa
    # espaço no relatório: quem lê precisa do que sobrou de pé.
    vm.ai_signal_verdicts = [
        asdict(item)
        for item in analysis.signal_verdicts
        if item.verdict in {"confirmed", "needs_evidence"}
    ]
    vm.ai_uncovered_findings = [asdict(item) for item in analysis.uncovered_findings]
    # Aparece mesmo quando vazio: a lista vazia diz "procurei e não achei", que é
    # afirmação diferente de "não procurei".
    vm.ai_suspected_injections = [
        asdict(item) for item in analysis.suspected_injections
    ]
    return vm


def _attach_opportunity(opportunity: OpportunityVM, contextual) -> None:
    opportunity.ai_diagnosis = contextual.contextual_diagnosis
    opportunity.ai_recommendation = contextual.recommendation
    opportunity.ai_implementation_steps = contextual.implementation_steps
    opportunity.ai_validation_steps = contextual.validation_steps
    opportunity.ai_dependencies = contextual.dependencies
    opportunity.ai_conflicts = contextual.conflicts
    opportunity.ai_risks = contextual.risks
    opportunity.ai_documentation = [
        asdict(item) for item in contextual.documentation
    ]
    opportunity.ai_assumptions = contextual.assumptions
    opportunity.ai_missing_evidence = contextual.missing_evidence
