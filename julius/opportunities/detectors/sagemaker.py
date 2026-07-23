"""Detectores de SageMaker: apps ociosas e endpoints sem uso."""

from __future__ import annotations

from julius.config import Config, is_gpu_instance
from julius.estimation import sagemaker as sm_est
from julius.inventory.model import Account, SageMakerApp, SageMakerEndpoint
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors._build import build

_DOC_IDLE = "https://docs.aws.amazon.com/sagemaker/latest/dg/studio-jl-admin-idle.html"
_DOC_ENDPOINT = "https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints.html"
_DOC_SERVERLESS = "https://docs.aws.amazon.com/sagemaker/latest/dg/serverless-endpoints.html"


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for app in account.sagemaker_apps:
        idle_off = app.idle_shutdown_min == 0 or app.idle_shutdown_min > th.sm_idle_shutdown_high_min
        if app.status == "InService" and app.idle_hours_per_day >= th.sm_idle_hours_min and idle_off:
            out.append(_idle_app(account, app, config, scan_id))
    for ep in account.sagemaker_endpoints:
        if ep.invocations_per_month <= th.sm_endpoint_unused_invocations:
            out.append(_unused_endpoint(account, ep, config, scan_id))
    return out


def _idle_app(account: Account, app: SageMakerApp, config: Config, scan_id: str) -> Opportunity:
    est = sm_est.idle_app_saving(app, config)
    gpu = is_gpu_instance(app.instance_type)
    return build(
        account=account.account_id, asset_type="sagemaker_app", asset_name=app.name,
        rule_id="SM-APP-IDLE", rule_version="1.0.0", difficulty=1, estimation=est,
        finding=f"{app.app_type} ocioso sem idle shutdown",
        why=(
            f"{app.app_type} ({app.instance_type}) fica ~{app.idle_hours_per_day:.1f}h ociosa/dia; "
            f"idle shutdown {'desabilitado' if app.idle_shutdown_min == 0 else f'{app.idle_shutdown_min} min'}."
            + (" Instância GPU parada é especialmente cara." if gpu else "")
        ),
        recommended_action="Habilitar idle shutdown (ou reduzir o período)",
        how_to_apply="Configurar auto-shutdown (ex.: 60 min) no Studio; se for só código, usar instância menor.",
        how_to_validate="Medir horas InService ociosas por semana após a mudança.",
        evidence=[
            f"status=InService, ~{app.idle_hours_per_day:.1f}h ociosas/dia",
            f"idle_shutdown={'off' if app.idle_shutdown_min == 0 else str(app.idle_shutdown_min) + ' min'}",
            f"instância {app.instance_type}" + (" (GPU)" if gpu else ""),
        ],
        risks=["shutdown pode interromper trabalho não salvo"],
        doc_links=[_DOC_IDLE], data_sources=["SageMaker ListApps", "CloudWatch"],
        observed_runs=app.active_days_per_month, coverage_days=app.coverage_days,
        has_optional_metrics=app.idle_hours_per_day > 0, owner_tag=app.owner_tag,
        config=config, scan_id=scan_id,
    )


def _unused_endpoint(account: Account, ep: SageMakerEndpoint, config: Config, scan_id: str) -> Opportunity:
    est = sm_est.unused_endpoint_saving(ep, config)
    return build(
        account=account.account_id, asset_type="sagemaker_endpoint", asset_name=ep.name,
        rule_id="SM-ENDPOINT-UNUSED", rule_version="1.0.0", difficulty=2, estimation=est,
        finding="Endpoint em tempo real praticamente sem uso",
        why=f"Endpoint com {ep.instance_count}× {ep.instance_type} roda 24/7 mas teve {ep.invocations_per_month} invocações/mês.",
        recommended_action="Descomissionar ou migrar para Serverless/Async Inference",
        how_to_apply="Se for esporádico, usar Serverless Inference (scale-to-zero); se obsoleto, remover o endpoint.",
        how_to_validate="Confirmar ausência de invocações e a queda do custo de instância.",
        evidence=[
            f"{ep.invocations_per_month} invocações/mês",
            f"{ep.instance_count}× {ep.instance_type} sempre ligado",
            f"auto_scaling={'sim' if ep.auto_scaling else 'não'}",
        ],
        risks=["pode haver consumidor esporádico crítico"],
        doc_links=[_DOC_SERVERLESS, _DOC_ENDPOINT], data_sources=["SageMaker DescribeEndpoint", "CloudWatch Invocations"],
        observed_runs=max(1, ep.coverage_days // 7), coverage_days=ep.coverage_days,
        has_optional_metrics=ep.coverage_days >= config.thresholds.min_coverage_days,
        owner_tag=ep.owner_tag, config=config, scan_id=scan_id, risk=0.7,
    )
