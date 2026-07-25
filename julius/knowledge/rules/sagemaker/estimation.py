"""Modelos financeiros para SageMaker (apps ociosas e endpoints sem uso)."""

from __future__ import annotations

from julius.collection.models import SageMakerApp, SageMakerEndpoint
from julius.config import Config
from julius.opportunities.base import Estimation

_HOURS_MONTH = 730.0


def idle_app_saving(app: SageMakerApp, config: Config) -> Estimation:
    """App Studio/Notebook ociosa cobra por hora enquanto está InService."""
    pricing = config.pricing
    hourly = config.pricing.sagemaker_hourly(app.instance_type)
    idle_hours_month = app.idle_hours_per_day * app.active_days_per_month
    saving = idle_hours_month * hourly
    return Estimation(
        method="sm_idle_app_v1",
        baseline_cost=round(idle_hours_month * hourly, 2),
        projected_cost=0.0,
        estimated_saving=round(saving, 2),
        assumptions=[
            f"{app.instance_type} a USD {hourly:.2f}/h",
            f"~{app.idle_hours_per_day:.1f}h ociosas/dia × {app.active_days_per_month} dias",
            "idle shutdown recupera as horas ociosas",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )


def unused_endpoint_saving(ep: SageMakerEndpoint, config: Config) -> Estimation:
    """Endpoint em tempo real sem invocações roda 24/7 pagando as instâncias."""
    pricing = config.pricing
    hourly = config.pricing.sagemaker_hourly(ep.instance_type)
    monthly = ep.instance_count * hourly * _HOURS_MONTH
    return Estimation(
        method="sm_unused_endpoint_v1",
        baseline_cost=round(monthly, 2),
        projected_cost=0.0,
        estimated_saving=round(monthly, 2),
        assumptions=[
            f"{ep.instance_count}× {ep.instance_type} a USD {hourly:.2f}/h, 24/7",
            f"{ep.invocations_per_month} invocações/mês (praticamente sem uso)",
            "descomissionar ou migrar para Serverless/Async recupera o custo",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
