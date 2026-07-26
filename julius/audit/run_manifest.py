"""Run manifest — torna cada execução reproduzível e auditável."""

from __future__ import annotations

from datetime import datetime, timezone

from julius.collection.models import Account
from julius.config import JULIUS_VERSION, KNOWLEDGE_VERSION, Config


def new_scan_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    # Microssegundos evitam colisões em portfólios e execuções automatizadas.
    return "scan-" + now.strftime("%Y%m%d-%H%M%S-%f")


def build_manifest(account: Account, config: Config, scan_id: str, source: str) -> list[dict]:
    now = datetime.now().astimezone()
    return [
        {"k": "julius_version", "v": JULIUS_VERSION},
        {"k": "scan_id", "v": scan_id},
        {"k": "conta", "v": account.account_id},
        {"k": "período", "v": account.period or f"{account.lookback_days}d"},
        {"k": "região", "v": account.region},
        {"k": "acesso", "v": source},
        {"k": "saúde da coleta", "v": account.collection_status},
        {
            "k": "detectores",
            "v": "regras versionadas: Glue, Glue Code, Sessions, Crawlers, DataBrew, Athena, Data, Step Functions e SageMaker",
        },
        {"k": "pesos", "v": f"{config.weights.profile}"},
        {
            "k": "regras",
            "v": "GLUE/GLUE-CODE/SESSION/CRAWLER/DATABREW/ATHENA/DATA/SFN/SM 1.x",
        },
        {"k": "knowledge", "v": KNOWLEDGE_VERSION},
        {
            "k": "preços",
            "v": (
                f"{config.pricing.region} · {config.pricing.currency} "
                f"v{config.pricing.version} · Glue STANDARD "
                f"{config.pricing.glue_dpu_hour:.4f}/DPU-h · FLEX "
                f"{config.pricing.glue_flex_dpu_hour:.4f}/DPU-h"
            ),
        },
        {"k": "gerado", "v": now.strftime("%Y-%m-%d %H:%M %z")},
    ]
