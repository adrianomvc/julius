"""Run manifest — torna cada execução reproduzível e auditável."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from julius.collection.models import Account
from julius.config import JULIUS_VERSION, KNOWLEDGE_VERSION, Config


def new_scan_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    # Microssegundos evitam colisões em portfólios e execuções automatizadas.
    return "scan-" + now.strftime("%Y%m%d-%H%M%S-%f")


def build_manifest(
    account: Account,
    config: Config,
    scan_id: str,
    source: str,
    managed_processes: Sequence[str] = (),
) -> list[dict]:
    """O manifesto da execução.

    `managed_processes` chega pronto de cima: quem sabe o que é processo da
    plataforma é a camada de conhecimento, e `state` não importa dela — a seta
    aponta para baixo, e `tests/test_dependency_direction.py` cobra isso.
    """
    now = datetime.now().astimezone()
    gerenciados = sorted(managed_processes)
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
                f"{config.pricing.provenance} · {config.pricing.currency} · "
                f"Glue STANDARD {config.pricing.glue_dpu_hour:.4f}/DPU-h · FLEX "
                f"{config.pricing.glue_flex_dpu_hour:.4f}/DPU-h"
            ),
        },
        *(
            [
                {
                    "k": "processos da plataforma",
                    "v": (
                        f"{len(gerenciados)} fora das recomendações (a conta não "
                        f"altera a infra deles), no inventário para o rateio: "
                        + ", ".join(gerenciados)
                    ),
                }
            ]
            if gerenciados
            else []
        ),
        {"k": "gerado", "v": now.strftime("%Y-%m-%d %H:%M %z")},
    ]
