"""Fechamento incremental entre a coleta boto3 e a fila contextual."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from julius.collection.checkpoints import DOMAIN_SOURCES, DomainCheckpointWriter
from julius.collection.models import Account, CollectionHealth
from julius.collection.sources import Source
from julius.state import RunStore

ACCOUNT = "123456789012"
SCAN = "scan-20260803-120000-000001"


def _source(name: str) -> Source:
    return Source(name=name, collect=lambda _ctx: [], impact="", next_action="")


def _close_domain(
    writer: DomainCheckpointWriter,
    domain: str,
    *,
    partial_source: str = "",
) -> None:
    for name in sorted(DOMAIN_SOURCES[domain]):
        writer.source_completed(
            _source(name),
            [
                CollectionHealth(
                    source=name,
                    status="partial" if name == partial_source else "ok",
                )
            ],
        )


def test_domain_closes_once_with_immutable_hash_and_ai_job(tmp_path: Path) -> None:
    account = Account(
        account_id=ACCOUNT,
        scan_id=SCAN,
        window_start="2026-07-01",
        window_end="2026-07-31",
        window_days=30,
    )
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="collecting")
        writer = DomainCheckpointWriter(store, tmp_path / "payloads", account, SCAN)

        _close_domain(writer, "athena")

        checkpoints = store.checkpoints(ACCOUNT, SCAN)
        assert len(checkpoints) == 1
        checkpoint = store.verified_checkpoint(ACCOUNT, SCAN, "athena")
        assert checkpoint.status == "ready"
        payload = json.loads(Path(checkpoint.payload_path).read_text(encoding="utf-8"))
        assert payload["account"]["id"] == ACCOUNT
        assert payload["scan_id"] == SCAN
        assert payload["domain"] == "athena"
        assert set(payload["payload"]) == {
            "athena_queries",
            "athena_capacity_reservations",
            "athena_coverage",
            "athena_actor_usage",
        }
        assert store.tasks()[0].context_hash == checkpoint.payload_hash

        _close_domain(writer, "athena")
        assert len(store.checkpoints(ACCOUNT, SCAN)) == 1
        assert len(store.tasks()) == 1


def test_partial_source_is_explicit_and_corruption_is_rejected(tmp_path: Path) -> None:
    account = Account(account_id=ACCOUNT, scan_id=SCAN)
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="collecting")
        writer = DomainCheckpointWriter(store, tmp_path / "payloads", account, SCAN)
        partial = sorted(DOMAIN_SOURCES["redshift"])[0]

        _close_domain(writer, "redshift", partial_source=partial)

        checkpoint = store.verified_checkpoint(ACCOUNT, SCAN, "redshift")
        assert checkpoint.status == "partial"
        Path(checkpoint.payload_path).write_text("corrompido", encoding="utf-8")
        with pytest.raises(ValueError, match="corrompido"):
            store.verified_checkpoint(ACCOUNT, SCAN, "redshift")


def test_domain_ai_never_blocks_deterministic_publication(tmp_path: Path) -> None:
    output = tmp_path / "account.json"
    output.write_text("{}", encoding="utf-8")
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_ready")
        domain_task = store.enqueue_ai(
            ACCOUNT,
            SCAN,
            "s3",
            context_hash="s3-hash",
            payload_path="s3.json",
        )

        store.publish_deterministic(ACCOUNT, SCAN, str(output))

        assert store.run_status(ACCOUNT, SCAN) == "ai_pending"
        store.transition_task(domain_task, "running")
        store.complete_ai(domain_task)
        assert store.run_status(ACCOUNT, SCAN) == "ai_partial"

        cross_task = store.enqueue_ai(
            ACCOUNT,
            SCAN,
            "cross_service",
            context_hash="cross-hash",
            payload_path="context.json",
        )
        store.transition_task(cross_task, "running")
        store.complete_ai(cross_task)
        assert store.run_status(ACCOUNT, SCAN) == "enriched"
