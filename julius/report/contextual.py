"""Anexa somente o enriquecimento validado do Devin ao relatório."""

from __future__ import annotations

from dataclasses import asdict

from julius.analysis.response_validator import ContextualAnalysis
from julius.report.view_models import OpportunityVM, ReportViewModel


def attach_contextual_analysis(
    vm: ReportViewModel,
    analysis: ContextualAnalysis,
) -> ReportViewModel:
    if analysis.account != vm.account_id or analysis.scan_id != vm.scan_id:
        raise ValueError("análise contextual não pertence ao relatório")
    recommendations = {
        item.opportunity_id: item for item in analysis.recommendations
    }
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
