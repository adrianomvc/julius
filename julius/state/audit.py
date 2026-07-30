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
    opportunity_count: int = 0,
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
        {"k": "perfil de escopo", "v": account.scope_profile},
        {"k": "modo S3", "v": account.s3_mode},
        {
            "k": "custo estimado por conta",
            "v": f"US$ {account.run_telemetry.estimated_cost_usd:.6f}",
        },
        {
            "k": "custo por oportunidade válida",
            "v": (
                f"US$ {account.run_telemetry.estimated_cost_usd / opportunity_count:.6f}"
                if opportunity_count
                else "indisponível (nenhuma oportunidade válida)"
            ),
        },
        {
            "k": "chamadas API",
            "v": str(
                sum(item.calls for item in account.run_telemetry.api_calls.values())
            ),
        },
        {
            "k": "páginas API",
            "v": str(
                sum(item.pages for item in account.run_telemetry.api_calls.values())
            ),
        },
        *[
            {
                "k": f"API {service}",
                "v": (
                    f"{sum(item.calls for item in stats)} chamadas · "
                    f"{sum(item.pages for item in stats)} páginas · "
                    f"{sum(item.retries for item in stats)} retries · "
                    f"{sum(item.throttles for item in stats)} throttles · "
                    f"{sum(item.cache_hits for item in stats)} cache hits · "
                    f"{sum(item.duration_ms for item in stats)} ms"
                ),
            }
            for service in sorted(
                {item.service for item in account.run_telemetry.api_calls.values()}
            )
            for stats in (
                [
                    item
                    for item in account.run_telemetry.api_calls.values()
                    if item.service == service
                ],
            )
        ],
        {
            "k": "operações sem tarifa",
            "v": (
                ", ".join(account.run_telemetry.unpriced_operations)
                if account.run_telemetry.unpriced_operations
                else "nenhuma"
            ),
        },
        {"k": "período", "v": account.period or f"{account.lookback_days}d"},
        {"k": "região", "v": account.region},
        {"k": "acesso", "v": source},
        {"k": "saúde da coleta", "v": account.collection_status},
        {
            "k": "detectores",
            "v": (
                "regras versionadas filtradas pelo perfil: Glue, Glue Code, "
                "Sessions, Crawlers, DataBrew, Athena, Data, Step Functions, "
                "SageMaker, S3 e Redshift"
            ),
        },
        {"k": "pesos", "v": f"{config.weights.profile}"},
        {
            "k": "regras",
            "v": (
                "GLUE/GLUE-CODE/SESSION/CRAWLER/DATABREW/ATHENA/DATA/"
                "SFN/SM/S3/REDSHIFT (quando habilitadas pelo escopo)"
            ),
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
