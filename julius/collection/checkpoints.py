"""Checkpoints imutáveis por domínio durante a coleta.

O callback roda na thread coordenadora depois do ``SourceResult`` ser aplicado.
Nenhum worker boto3 escreve em DuckDB ou serializa o ``Account`` compartilhado.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from julius.collection.models import Account, CollectionHealth
from julius.collection.normalizers.dump import account_to_dataset
from julius.collection.sources import Source

CHECKPOINT_SCHEMA_VERSION = "1.0"

DOMAIN_SOURCES: dict[str, frozenset[str]] = {
    "glue": frozenset(
        {
            "Glue Jobs",
            "Glue Scripts",
            "Spark Event Logs",
            "Glue Catalog",
            "Glue Crawlers",
            "Glue Triggers",
            "Glue DataBrew",
            "CloudWatch Glue CPU",
            "CloudWatch Glue Observability",
            "Glue Interactive Sessions",
            "Glue Cost Explorer",
        }
    ),
    "athena": frozenset({"Athena Queries", "Athena Provisioned Capacity"}),
    "s3": frozenset(
        {
            "Amazon S3",
            "S3 Config",
            "S3 Prefixes",
            "S3 Access Evidence",
            "S3 Multipart Uploads",
            "S3 Cost Explorer",
        }
    ),
    "sagemaker": frozenset(
        {
            "SageMaker Studio",
            "SageMaker Spaces",
            "SageMaker Domains",
            "SageMaker Endpoints",
            "SageMaker Notebooks",
            "SageMaker Jobs",
            "SageMaker Feature Store",
            "SageMaker Pipelines",
            "SageMaker Model Monitor",
            "SageMaker Inference Recommender",
            "SageMaker Cost Explorer",
            "SageMaker Savings Plans",
        }
    ),
    "redshift": frozenset({"Amazon Redshift", "Redshift Cost Explorer"}),
    "orchestration": frozenset({"Step Functions", "EventBridge Schedules"}),
}

DOMAIN_FIELDS: dict[str, tuple[str, ...]] = {
    "glue": (
        "glue_jobs",
        "interactive_sessions",
        "glue_crawlers",
        "glue_triggers",
        "databrew_jobs",
        "tables",
        "glue_cost_coverage",
    ),
    "athena": (
        "athena_queries",
        "athena_capacity_reservations",
        "athena_coverage",
        "athena_actor_usage",
    ),
    "s3": (
        "s3_buckets",
        "s3_prefixes",
        "s3_multipart",
        "s3_bucket_configs",
        "s3_cost_coverage",
    ),
    "sagemaker": (
        "sagemaker_apps",
        "sagemaker_spaces",
        "sagemaker_domains",
        "sagemaker_endpoints",
        "sagemaker_notebooks",
        "sagemaker_jobs",
        "sagemaker_feature_groups",
        "sagemaker_pipelines",
        "sagemaker_monitoring_schedules",
        "sagemaker_inference_recommendations",
        "sagemaker_cost_coverage",
        "sagemaker_savings_plans",
    ),
    "redshift": ("redshift_clusters", "redshift_cost_coverage"),
    "orchestration": (
        "state_machines",
        "stepfunctions_operational",
        "schedules",
    ),
}


class CheckpointStore(Protocol):
    """Porta mínima implementada pelo ledger da camada de composição."""

    def create_run(
        self,
        account_id: str,
        scan_id: str,
        *,
        status: str = "created",
        deterministic_path: str = "",
    ) -> None: ...

    def run_status(self, account_id: str, scan_id: str) -> str: ...

    def transition(self, account_id: str, scan_id: str, status: str) -> None: ...

    def checkpoint(
        self,
        account_id: str,
        scan_id: str,
        domain: str,
        *,
        status: str,
        payload_path: str,
        payload_hash: str,
        sources: dict[str, str],
    ) -> None: ...

    def enqueue_ai(
        self,
        account_id: str,
        scan_id: str,
        domain: str,
        *,
        context_hash: str,
        payload_path: str,
    ) -> str: ...


class DomainCheckpointWriter:
    """Fecha cada domínio uma vez e enfileira seu pacote pelo hash."""

    def __init__(
        self,
        store: CheckpointStore,
        root: str | Path,
        account: Account,
        scan_id: str,
        *,
        enqueue_ai: bool = True,
    ) -> None:
        if not scan_id:
            raise ValueError("scan_id é obrigatório para checkpoints")
        self.store = store
        self.root = Path(root)
        self.account = account
        self.scan_id = scan_id
        self.enqueue_ai = enqueue_ai
        self._statuses: dict[str, str] = {}
        self._health: dict[str, list[CollectionHealth]] = {}
        self._closed: set[str] = set()

    def source_completed(
        self, source: Source, entries: list[CollectionHealth]
    ) -> None:
        own = next((entry for entry in entries if entry.source == source.name), None)
        self._statuses[source.name] = own.status if own is not None else "unavailable"
        self._health[source.name] = list(entries)
        for domain, expected in DOMAIN_SOURCES.items():
            if domain in self._closed or source.name not in expected:
                continue
            if expected <= self._statuses.keys():
                self._close(domain, expected)

    def _close(self, domain: str, expected: frozenset[str]) -> None:
        sources = {name: self._statuses[name] for name in sorted(expected)}
        status = _checkpoint_status(sources.values())
        dataset = account_to_dataset(self.account)
        health = [
            asdict(entry)
            for source in sorted(expected)
            for entry in self._health.get(source, [])
        ]
        checkpoint = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "account": {
                "id": self.account.account_id,
                "region": self.account.region,
            },
            "scan_id": self.scan_id,
            "domain": domain,
            "status": status,
            "window": {
                "start": self.account.window_start,
                "end": self.account.window_end,
                "days": self.account.window_days,
            },
            "sources": sources,
            "collection_health": health,
            "payload": {
                field: dataset.get(field) for field in DOMAIN_FIELDS[domain]
            },
        }
        encoded = json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        path = (
            self.root
            / self.account.account_id
            / self.scan_id
            / f"{domain}-{digest[:16]}.json"
        )
        _write_immutable(path, encoded)
        self.store.checkpoint(
            self.account.account_id,
            self.scan_id,
            domain,
            status=status,
            payload_path=str(path),
            payload_hash=digest,
            sources=sources,
        )
        if self.enqueue_ai:
            self.store.enqueue_ai(
                self.account.account_id,
                self.scan_id,
                domain,
                context_hash=digest,
                payload_path=str(path),
            )
        self._closed.add(domain)


def _checkpoint_status(statuses) -> str:
    values = list(statuses)
    if values and all(status == "ok" for status in values):
        return "ready"
    if any(status in {"ok", "partial"} for status in values):
        return "partial"
    return "unavailable"


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"colisão de hash no checkpoint: {path}")
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
