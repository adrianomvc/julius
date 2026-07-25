"""Detectores de DataBrew sem confundir node-hour com Glue DPU-hour."""

from __future__ import annotations

import calendar
from datetime import date

from julius.config import Config
from julius.inventory.model import Account
from julius.opportunities.base import Estimation, Opportunity
from julius.opportunities.detectors._build import build

_DOC = "https://docs.aws.amazon.com/databrew/latest/dg/jobs.html"


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    for job in account.databrew_jobs:
        if job.runs_in_window and job.failures_in_window:
            baseline = job.estimated_node_hours_window * config.pricing.databrew_node_hour
            ratio = job.failures_in_window / job.runs_in_window
            saving = baseline * ratio
            estimation = Estimation(
                method="databrew_failure_waste_v1",
                baseline_cost=round(baseline, 2),
                projected_cost=round(max(0.0, baseline - saving), 2),
                estimated_saving=round(min(baseline, saving), 2),
                assumptions=[
                    "node-hours são estimadas pela capacidade máxima",
                    "economia será reduzida pelo fator conservador global",
                ],
                pricing_region=config.pricing.region,
                estimation_version=config.pricing.version,
            )
            out.append(build(
                account=account.account_id,
                asset_type="databrew_job",
                asset_name=job.name,
                rule_id="DATABREW-FAILING-JOB",
                rule_version="1.0.0",
                difficulty=2,
                estimation=estimation,
                finding="DataBrew Job falha no mês atual",
                why=f"{job.failures_in_window}/{job.runs_in_window} execuções falharam.",
                recommended_action="Corrigir a causa das falhas e revisar retries",
                how_to_apply="Revisar logs e configuração; mudança somente após aprovação.",
                how_to_validate="Comparar falhas e node-hours no mês seguinte.",
                evidence=[
                    f"{job.failures_in_window}/{job.runs_in_window} falhas",
                    f"{job.estimated_node_hours_window:.2f} node-h estimadas",
                ],
                risks=["capacidade real por node não está disponível na evidência"],
                doc_links=[_DOC],
                data_sources=["DataBrew ListJobs", "ListJobRuns"],
                observed_runs=job.runs_in_window,
                coverage_days=min(account.lookback_days, 31),
                has_optional_metrics=False,
                owner_tag=job.owner_tag,
                config=config,
                scan_id=scan_id,
                blocked=True,
            ))
        if job.expected_runs_monthly:
            expected_in_window = job.expected_runs_monthly * _month_progress(account)
            deviation = abs(job.runs_in_window - expected_in_window) / max(1.0, expected_in_window)
            if deviation >= 0.5:
                out.append(
                    build(
                        account=account.account_id,
                        asset_type="databrew_job",
                        asset_name=job.name,
                        rule_id="DATABREW-SCHEDULE-RUN-MISMATCH",
                        rule_version="1.0.0",
                        difficulty=2,
                        estimation=Estimation(
                            method="databrew_schedule_run_mismatch_v1",
                            baseline_cost=0.0,
                            projected_cost=0.0,
                            estimated_saving=0.0,
                            assumptions=["economia não quantificada"],
                        ),
                        finding="DataBrew runs divergem do schedule",
                        why=(
                            f"Schedule prevê ~{expected_in_window:.1f} até a data; "
                            f"{job.runs_in_window} foram observadas."
                        ),
                        recommended_action="Reconciliar cron, falhas e disparos manuais",
                        how_to_apply="Revisar schedule e histórico sem alterar o recurso.",
                        how_to_validate="Confirmar a contagem no próximo período.",
                        evidence=[
                            f"esperadas até a data ~{expected_in_window:.1f}",
                            f"observadas {job.runs_in_window}",
                        ],
                        risks=["mês parcial pode explicar parte da diferença"],
                        doc_links=[_DOC],
                        data_sources=["DataBrew ListSchedules", "ListJobRuns"],
                        observed_runs=job.runs_in_window,
                        coverage_days=min(account.lookback_days, 31),
                        has_optional_metrics=True,
                        owner_tag=job.owner_tag,
                        config=config,
                        scan_id=scan_id,
                        blocked=True,
                    )
                )
    return out


def _month_progress(account: Account) -> float:
    try:
        current = date.fromisoformat(account.generated_at[:10])
    except (TypeError, ValueError):
        current = date.today()
    days = calendar.monthrange(current.year, current.month)[1]
    return min(1.0, current.day / days)
