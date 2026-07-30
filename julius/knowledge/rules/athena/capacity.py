"""Oportunidades e sinais de Capacity Reservations do Athena."""

from __future__ import annotations

from julius.collection.models import Account, AthenaCapacityReservation
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal

_DOC = "https://docs.aws.amazon.com/athena/latest/ug/capacity-management.html"
_DOC_METRICS = (
    "https://docs.aws.amazon.com/athena/latest/ug/athena-metrics-dimensions.html"
)


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    return [
        _low_utilization(account, item, config, scan_id)
        for item in account.athena_capacity_reservations
        if _safe_reduction(item, config)
    ]


def _safe_reduction(item: AthenaCapacityReservation, config: Config) -> bool:
    return (
        item.status.upper() == "ACTIVE"
        and item.target_dpus >= 8
        and item.utilization_p95 is not None
        and item.utilization_p95
        < config.thresholds.athena_capacity_low_utilization_p95
        and item.query_queue_p95_ms is not None
        and item.query_queue_p95_ms
        <= config.thresholds.athena_capacity_queue_safe_ms
        and bool(item.workgroups)
        and item.coverage_days >= config.thresholds.min_coverage_days
        and item.allocated_cost is not None
        and item.allocated_cost > 0
    )


def _low_utilization(account, item, config, scan_id) -> Opportunity:
    baseline = float(item.allocated_cost or 0)
    projected = baseline * (item.target_dpus - 4) / item.target_dpus
    return build(
        Finding(
            rule_id="ATHENA-CAPACITY-LOW-UTILIZATION",
            rule_version="1.0.0",
            asset_type="athena_capacity_reservation",
            asset_name=item.name,
            title="Reserva Athena com baixa utilização p95",
            why=(
                f"{item.target_dpus} DPUs reservadas; consumo p95 "
                f"{item.consumed_dpus_p95:.1f} DPU e fila p95 "
                f"{item.query_queue_p95_ms:.0f} ms."
            ),
        ),
        Recommendation(
            difficulty=2,
            action=f"Avaliar redução controlada de {item.target_dpus} para {item.target_dpus - 4} DPUs",
            how_to_apply=(
                "Alteração deve ser executada pelo time autorizado, em uma etapa "
                "de 4 DPUs; o Julius permanece read-only."
            ),
            how_to_validate="Comparar consumo p95 e fila p95 após uma janela equivalente.",
            risks=["picos não presentes na janela podem aumentar fila"],
            docs=[_DOC, _DOC_METRICS],
        ),
        Evidence(
            items=[
                f"utilização p95={item.utilization_p95:.1%}",
                f"workgroups associados={len(item.workgroups)}",
                f"cobertura={item.coverage_days} dias",
            ],
            sources=["Athena Capacity APIs", "CloudWatch AWS/Athena"],
            observed_runs=1,
            coverage_days=item.coverage_days,
            has_optional_metrics=True,
        ),
        Estimation(
            method="athena_capacity_one_step_v1",
            baseline_cost=round(baseline, 2),
            projected_cost=round(projected, 2),
            estimated_saving=round(baseline - projected, 2),
            assumptions=[
                "redução conservadora de uma etapa (4 DPU)",
                "baseline rateado da cobrança UnblendedCost do Cost Explorer",
            ],
            pricing_region=config.pricing.region,
            estimation_version=config.pricing.version,
            baseline_quality="allocated",
            saving_quality="modeled_evidence",
        ),
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )


def signals(account: Account, config: Config) -> list[Signal]:
    out = []
    for item in account.athena_capacity_reservations:
        cases = []
        if not item.workgroups:
            cases.append(
                (
                    "ATHENA-CAPACITY-UNASSIGNED",
                    "reserva sem workgroup associado",
                    "A reserva ainda é necessária?",
                )
            )
        if item.query_queue_p95_ms and (
            item.query_queue_p95_ms > config.thresholds.athena_capacity_queue_safe_ms
        ):
            cases.append(
                (
                    "ATHENA-CAPACITY-QUEUE-PRESSURE",
                    f"fila p95 de {item.query_queue_p95_ms:.0f} ms",
                    "A capacidade ou sua distribuição entre workgroups é insuficiente?",
                )
            )
        if item.idle_hours and item.idle_hours >= item.coverage_days * 12:
            cases.append(
                (
                    "ATHENA-CAPACITY-CONCENTRATED-DEMAND",
                    f"{item.idle_hours:.0f} horas sem consumo",
                    "A demanda concentrada permite reservar capacidade por menos horas?",
                )
            )
        if (
            item.utilization_p95 is not None
            and item.utilization_p95
            < config.thresholds.athena_capacity_low_utilization_p95
            and item.allocated_cost is None
        ):
            cases.append(
                (
                    "ATHENA-CAPACITY-COST-UNAVAILABLE",
                    "baixa utilização p95 sem cobrança de capacidade rateada",
                    "Qual UsageType do Cost Explorer representa a reserva nesta conta?",
                )
            )
        for rule_id, observation, question in cases:
            out.append(
                Signal(
                    kind="metric",
                    rule_id=rule_id,
                    asset_type="athena_capacity_reservation",
                    asset_name=item.name,
                    observation=observation,
                    question=question,
                    missing_evidence=["série horária e calendário de demanda"],
                    doc_links=[_DOC, _DOC_METRICS],
                )
            )
    return out
