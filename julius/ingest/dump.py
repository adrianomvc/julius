"""Serializa uma Account para o schema de dataset exportado (inverso do loader).

Permite que a coleta ao vivo (boto3) grave um dataset inspecionável, que flui
pelo mesmo pipeline offline.
"""

from __future__ import annotations

from dataclasses import asdict

from julius.inventory.model import Account

# Campos internos (properties/derivados) que não fazem parte do schema exportado.
_DROP = {
    "dpu_per_worker",
    "monthly_dpu_hours",
    "historical_monthly_dpu_hours",
    "glue_version_num",
    "monthly_bytes_scanned",
    "unattributed_cost",
}


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _DROP and v is not None}


def account_to_dataset(account: Account) -> dict:
    return {
        "account": account.account_id,
        "region": account.region,
        "period": account.period,
        "lookback_days": account.lookback_days,
        "generated_at": account.generated_at,
        "collection_health": [
            _clean(asdict(item)) for item in account.collection_health
        ],
        "currency": account.currency,
        "cost_explorer": {"services": [asdict(s) for s in account.services]},
        "glue_jobs": [_clean(asdict(j)) for j in account.glue_jobs],
        "interactive_sessions": [_clean(asdict(s)) for s in account.interactive_sessions],
        "glue_crawlers": [_clean(asdict(c)) for c in account.glue_crawlers],
        "glue_triggers": [_clean(asdict(t)) for t in account.glue_triggers],
        "databrew_jobs": [_clean(asdict(j)) for j in account.databrew_jobs],
        "process_costs": [_clean(asdict(p)) for p in account.process_costs],
        "athena_queries": [_clean(asdict(q)) for q in account.athena_queries],
        "athena_coverage": _clean(asdict(account.athena_coverage))
        if account.athena_coverage else None,
        "athena_actor_usage": [_clean(asdict(a)) for a in account.athena_actor_usage],
        "glue_cost_coverage": _clean(asdict(account.glue_cost_coverage))
        if account.glue_cost_coverage else None,
        "state_machines": [_clean(asdict(s)) for s in account.state_machines],
        "sagemaker_apps": [_clean(asdict(a)) for a in account.sagemaker_apps],
        "sagemaker_endpoints": [_clean(asdict(e)) for e in account.sagemaker_endpoints],
        "tables": [_clean(asdict(t)) for t in account.tables],
        "schedules": [_clean(asdict(s)) for s in account.schedules],
        "actor_events": [_clean(asdict(e)) for e in account.actor_events],
        "governance": {
            "producer_candidates": [asdict(p) for p in account.producer_candidates],
            "previous_results": [asdict(r) for r in account.previous_results],
        },
    }
