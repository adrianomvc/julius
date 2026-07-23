"""Run manifest — torna cada execução reproduzível e auditável."""

from __future__ import annotations

from datetime import datetime, timezone

from julius.config import JULIUS_VERSION, KNOWLEDGE_VERSION, Config
from julius.inventory.model import Account


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
        {
            "k": "detectores",
            "v": "18 regras: Glue, Sessions, Athena, Data, Step Functions e SageMaker",
        },
        {"k": "pesos", "v": f"{config.weights.profile}"},
        {
            "k": "regras",
            "v": "GLUE/SESSION/ATHENA/DATA/SFN/SM 1.x (versionadas por oportunidade)",
        },
        {"k": "knowledge", "v": KNOWLEDGE_VERSION},
        {"k": "preços", "v": f"{config.pricing.region} v{config.pricing.version}"},
        {"k": "gerado", "v": now.strftime("%Y-%m-%d %H:%M %z")},
    ]
