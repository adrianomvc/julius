"""Modelo financeiro para Glue Interactive Sessions ociosas."""

from __future__ import annotations

from julius.collection.models import InteractiveSession
from julius.config import Config
from julius.opportunities.base import Estimation


def idle_saving(session: InteractiveSession, config: Config) -> Estimation:
    """Economia = tempo ocioso mensal × DPU × preço DPU-hora.

    Reduzir o idle_timeout evita cobrança do tempo READY ocioso.
    """
    pricing = config.pricing
    idle_hours_month = session.idle_hours_per_day * session.active_days_per_month
    baseline = idle_hours_month * session.dpu * pricing.glue_dpu_hour
    # Ainda sobra um pequeno idle aceitável (novo timeout ~1h/dia).
    residual_hours = min(idle_hours_month, session.active_days_per_month * 1.0)
    projected = residual_hours * session.dpu * pricing.glue_dpu_hour
    saving = max(0.0, baseline - projected)
    return Estimation(
        method="glue_session_idle_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(projected, 2),
        estimated_saving=round(saving, 2),
        assumptions=[
            f"idle atual {session.idle_hours_per_day:.1f}h/dia × {session.active_days_per_month} dias",
            "cobrança inclui tempo READY ocioso até o stop",
            "novo idle_timeout ~60 min",
        ],
        pricing_region=pricing.region,
        estimation_version=pricing.version,
    )
