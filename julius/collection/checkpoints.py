"""Checkpoints imutáveis por domínio durante a coleta.

O callback roda na thread coordenadora depois do ``SourceResult`` ser aplicado.
Nenhum worker boto3 escreve em DuckDB ou serializa o ``Account`` compartilhado.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from julius.collection.models import Account, CollectionHealth
from julius.collection.normalizers.dump import account_fields_to_dataset
from julius.collection.normalizers.loader import account_from_dataset
from julius.collection.settings import DATASET_SCHEMA_VERSION
from julius.collection.sources import Source

CHECKPOINT_SCHEMA_VERSION = "2.0"

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
        collection_fingerprint: str = "",
    ) -> None:
        if not scan_id:
            raise ValueError("scan_id é obrigatório para checkpoints")
        self.store = store
        self.root = Path(root)
        self.account = account
        self.scan_id = scan_id
        self.enqueue_ai = enqueue_ai
        self.collection_fingerprint = collection_fingerprint
        self._statuses: dict[str, str] = {}
        self._health: dict[str, list[CollectionHealth]] = {}
        self._closed: set[str] = set()
        # Escrita/hash/ledger local não disputa o pool boto3. Um único escritor
        # mantém ordem determinística e evita concorrência desnecessária no DB.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="julius-artifacts"
        )
        self._futures: list[Future[None]] = []
        self._finished = False

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
        # Congela antes de devolver o controle ao scheduler: fontes posteriores
        # podem enriquecer a Account, mas o pacote representa este fechamento.
        payload = account_fields_to_dataset(self.account, DOMAIN_FIELDS[domain])
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
            "collection_fingerprint": self.collection_fingerprint,
            "status": status,
            "window": {
                "start": self.account.window_start,
                "end": self.account.window_end,
                "days": self.account.window_days,
            },
            "sources": sources,
            "collection_health": health,
            "payload": payload,
        }
        self._closed.add(domain)
        self._futures.append(
            self._executor.submit(
                self._persist, domain, status, sources, checkpoint
            )
        )

    def _persist(
        self,
        domain: str,
        status: str,
        sources: dict[str, str],
        checkpoint: dict,
    ) -> None:
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

    def wait(self, *, raise_errors: bool = True) -> None:
        """Drena artefatos locais; nunca espera provider de IA."""
        if self._finished:
            return
        first_error: BaseException | None = None
        for future in self._futures:
            try:
                future.result()
            except BaseException as exc:  # pragma: no branch - preserva a primeira
                first_error = first_error or exc
        self._executor.shutdown(wait=True)
        self._finished = True
        if first_error is not None and raise_errors:
            raise first_error


def resume_ready_domains(
    store: CheckpointStore,
    account: Account,
    scan_id: str,
    *,
    collection_fingerprint: str,
) -> tuple[set[str], list[CollectionHealth]]:
    """Reidrata somente domínios completos e exatamente compatíveis."""
    checkpoints = getattr(store, "checkpoints", None)
    verified = getattr(store, "verified_checkpoint", None)
    if not callable(checkpoints) or not callable(verified):
        return set(), []
    resumed: set[str] = set()
    health: list[CollectionHealth] = []
    for record in checkpoints(account.account_id, scan_id):
        domain = str(getattr(record, "domain", ""))
        if domain not in DOMAIN_FIELDS or getattr(record, "status", "") != "ready":
            continue
        try:
            current = verified(account.account_id, scan_id, domain)
            raw = json.loads(Path(current.payload_path).read_text(encoding="utf-8"))
            if not _compatible_checkpoint(
                raw,
                account,
                scan_id,
                domain,
                collection_fingerprint,
                current.sources,
            ):
                continue
            loaded = account_from_dataset(
                {
                    "dataset_schema_version": DATASET_SCHEMA_VERSION,
                    "account": account.account_id,
                    "scope": {
                        "profile": account.scope_profile,
                        "s3_mode": account.s3_mode,
                    },
                    "region": account.region,
                    "window": raw.get("window", {}),
                    "collection_health": raw.get("collection_health", []),
                    **dict(raw.get("payload") or {}),
                }
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        _apply_domain(account, loaded, domain)
        for entry in loaded.collection_health:
            entry.result_origin = "resumed"
            entry.duration_ms = 0
            health.append(entry)
        resumed.update(DOMAIN_SOURCES[domain])
    return resumed, health


def _compatible_checkpoint(
    raw: dict,
    account: Account,
    scan_id: str,
    domain: str,
    fingerprint: str,
    sources: dict[str, str],
) -> bool:
    account_raw = raw.get("account") or {}
    return (
        raw.get("checkpoint_schema_version") == CHECKPOINT_SCHEMA_VERSION
        and raw.get("scan_id") == scan_id
        and raw.get("domain") == domain
        and raw.get("collection_fingerprint") == fingerprint
        and account_raw.get("id") == account.account_id
        and account_raw.get("region") == account.region
        and raw.get("window", {}).get("start") == account.window_start
        and raw.get("window", {}).get("end") == account.window_end
        and set(sources) == set(DOMAIN_SOURCES[domain])
        and all(status == "ok" for status in sources.values())
    )


def _apply_domain(target: Account, loaded: Account, domain: str) -> None:
    for field in DOMAIN_FIELDS[domain]:
        if field == "stepfunctions_operational":
            for suffix in (
                "map_backlog",
                "open_executions",
                "service_integration_failures",
                "service_integration_timeouts",
            ):
                name = f"stepfunctions_{suffix}"
                setattr(target, name, getattr(loaded, name))
            continue
        setattr(target, field, getattr(loaded, field))


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
