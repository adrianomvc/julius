"""Serializa uma Account para o schema de dataset exportado (inverso do loader).

Permite que a coleta ao vivo (boto3) grave um dataset inspecionável, que flui
pelo mesmo pipeline offline.

**Toda coleção do `Account` precisa aparecer aqui.** Uma que falte não dá erro:
o loader repõe o default, o inventário chega vazio do outro lado e o relatório
não tem como distinguir "não foi coletado" de "não existe na conta" — foi o que
aconteceu com S3 e Redshift, coletados e descartados entre `collect` e `report`.
`tests/test_dump_roundtrip.py` cobra a cobertura contra o próprio `Account`.
"""

from __future__ import annotations

from dataclasses import asdict

from julius.collection.models import Account
from julius.collection.settings import DATASET_SCHEMA_VERSION

# Campos internos (properties/derivados) que não fazem parte do schema exportado.
_DROP = {
    "dpu_per_worker",
    "window_dpu_hours",
    "modeled_window_dpu_hours",
    "monthly_dpu_hours",
    "monthly_node_hours",
    "monthly_factor",
    "monthly_cost",
    "runs_per_month",
    "expected_runs_in_window",
    "glue_version_num",
    "monthly_bytes_scanned",
    "unattributed_cost",
    "instance_hours",
}


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _DROP and v is not None}


def account_to_dataset(account: Account) -> dict:
    return {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "account": account.account_id,
        "scope": {
            "profile": account.scope_profile,
            "s3_mode": account.s3_mode,
        },
        "region": account.region,
        "period": account.period,
        "lookback_days": account.lookback_days,
        "generated_at": account.generated_at,
        **({"scan_id": account.scan_id} if account.scan_id else {}),
        "window": {
            "start": account.window_start,
            "end": account.window_end,
            "days": account.window_days,
        },
        "collection_health": [
            _clean(asdict(item)) for item in account.collection_health
        ],
        "run_telemetry": asdict(account.run_telemetry),
        "currency": account.currency,
        "cost_explorer": {"services": [asdict(s) for s in account.services]},
        "glue_jobs": [_clean(asdict(j)) for j in account.glue_jobs],
        "interactive_sessions": [_clean(asdict(s)) for s in account.interactive_sessions],
        "glue_crawlers": [_clean(asdict(c)) for c in account.glue_crawlers],
        "glue_triggers": [_clean(asdict(t)) for t in account.glue_triggers],
        "databrew_jobs": [_clean(asdict(j)) for j in account.databrew_jobs],
        "process_costs": [_clean(asdict(p)) for p in account.process_costs],
        "athena_queries": [_clean(asdict(q)) for q in account.athena_queries],
        "athena_capacity_reservations": [
            _clean(asdict(r)) for r in account.athena_capacity_reservations
        ],
        "athena_coverage": _clean(asdict(account.athena_coverage))
        if account.athena_coverage else None,
        "athena_actor_usage": [_clean(asdict(a)) for a in account.athena_actor_usage],
        "glue_cost_coverage": _clean(asdict(account.glue_cost_coverage))
        if account.glue_cost_coverage else None,
        "state_machines": [_clean(asdict(s)) for s in account.state_machines],
        "stepfunctions_operational": {
            "map_backlog": account.stepfunctions_map_backlog,
            "open_executions": account.stepfunctions_open_executions,
            "service_integration_failures": (
                account.stepfunctions_service_integration_failures
            ),
            "service_integration_timeouts": (
                account.stepfunctions_service_integration_timeouts
            ),
        },
        "sagemaker_apps": [_clean(asdict(a)) for a in account.sagemaker_apps],
        "sagemaker_spaces": [_clean(asdict(s)) for s in account.sagemaker_spaces],
        "sagemaker_domains": [_clean(asdict(d)) for d in account.sagemaker_domains],
        "sagemaker_endpoints": [_clean(asdict(e)) for e in account.sagemaker_endpoints],
        "sagemaker_notebooks": [
            _clean(asdict(n)) for n in account.sagemaker_notebooks
        ],
        "sagemaker_jobs": [_clean(asdict(j)) for j in account.sagemaker_jobs],
        "sagemaker_feature_groups": [
            _clean(asdict(g)) for g in account.sagemaker_feature_groups
        ],
        "sagemaker_pipelines": [
            _clean(asdict(p)) for p in account.sagemaker_pipelines
        ],
        "sagemaker_monitoring_schedules": [
            _clean(asdict(s)) for s in account.sagemaker_monitoring_schedules
        ],
        "sagemaker_inference_recommendations": [
            _clean(asdict(r)) for r in account.sagemaker_inference_recommendations
        ],
        "sagemaker_cost_coverage": _clean(asdict(account.sagemaker_cost_coverage))
        if account.sagemaker_cost_coverage else None,
        "sagemaker_savings_plans": _clean(asdict(account.sagemaker_savings_plans))
        if account.sagemaker_savings_plans else None,
        "redshift_clusters": [_clean(asdict(c)) for c in account.redshift_clusters],
        "redshift_cost_coverage": _clean(asdict(account.redshift_cost_coverage))
        if account.redshift_cost_coverage else None,
        "s3_buckets": [_clean(asdict(b)) for b in account.s3_buckets],
        "s3_prefixes": [_clean(asdict(p)) for p in account.s3_prefixes],
        "s3_multipart": [_clean(asdict(m)) for m in account.s3_multipart],
        "s3_bucket_configs": [_clean(asdict(c)) for c in account.s3_bucket_configs],
        "s3_cost_coverage": _clean(asdict(account.s3_cost_coverage))
        if account.s3_cost_coverage else None,
        "tables": [_clean(asdict(t)) for t in account.tables],
        "schedules": [_clean(asdict(s)) for s in account.schedules],
        "actor_events": [_clean(asdict(e)) for e in account.actor_events],
        "governance": {
            "producer_candidates": [asdict(p) for p in account.producer_candidates],
            "previous_results": [asdict(r) for r in account.previous_results],
        },
    }


_DOMAIN_SINGLE_FIELDS = {
    "athena_coverage",
    "glue_cost_coverage",
    "s3_cost_coverage",
    "sagemaker_cost_coverage",
    "sagemaker_savings_plans",
    "redshift_cost_coverage",
}


def account_fields_to_dataset(account: Account, fields: tuple[str, ...]) -> dict:
    """Congela somente campos de um domínio no mesmo schema do dataset.

    Checkpoints fecham enquanto outros serviços ainda coletam. Serializar a
    conta inteira nesse instante desperdiçava CPU e lia listas ainda mutáveis;
    este recorte mantém exatamente a representação pública dos campos pedidos.
    """
    out: dict = {}
    for field in fields:
        if field == "stepfunctions_operational":
            out[field] = {
                "map_backlog": account.stepfunctions_map_backlog,
                "open_executions": account.stepfunctions_open_executions,
                "service_integration_failures": (
                    account.stepfunctions_service_integration_failures
                ),
                "service_integration_timeouts": (
                    account.stepfunctions_service_integration_timeouts
                ),
            }
            continue
        value = getattr(account, field)
        if field in _DOMAIN_SINGLE_FIELDS:
            out[field] = _clean(asdict(value)) if value else None
        else:
            out[field] = [_clean(asdict(item)) for item in value]
    return out
