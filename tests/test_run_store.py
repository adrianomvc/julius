"""Ledger retomável entre coleta determinística e análise contextual."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from julius.state import RunStore, file_sha256


def test_run_store_tracks_checkpoint_and_idempotent_ai_job(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(
            "123456789012",
            "scan-a",
            status="deterministic_published",
            deterministic_path="context.json",
        )
        store.checkpoint(
            "123456789012",
            "scan-a",
            "cross_service",
            status="ready",
            payload_path="context.json",
            payload_hash="abc",
            sources={"EC2": "ok", "S3": "ok"},
        )

        first = store.enqueue_ai(
            "123456789012",
            "scan-a",
            "cross_service",
            context_hash="abc",
            payload_path="context.json",
        )
        second = store.enqueue_ai(
            "123456789012",
            "scan-a",
            "cross_service",
            context_hash="abc",
            payload_path="context.json",
        )

        assert first == second
        assert store.run_status("123456789012", "scan-a") == "ai_pending"
        assert [task.task_id for task in store.tasks()] == [first]


def test_run_store_enforces_transitions_and_completes_run(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run("123456789012", "scan-a", status="deterministic_published")
        task_id = store.enqueue_ai(
            "123456789012",
            "scan-a",
            "cross_service",
            context_hash="abc",
            payload_path="context.json",
        )

        with pytest.raises(ValueError, match="transição de job inválida"):
            store.complete_ai(task_id)

        store.transition_task(task_id, "running")
        store.complete_ai(task_id)

        completed = store.tasks(status="completed")
        assert completed[0].attempts == 1
        assert store.run_status("123456789012", "scan-a") == "enriched"
        with pytest.raises(ValueError, match="transição de run inválida"):
            store.transition("123456789012", "scan-a", "ai_pending")


def test_failed_job_can_be_retried_without_mixing_accounts(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        for account in ("111111111111", "222222222222"):
            store.create_run(account, "same-scan", status="deterministic_published")
            store.enqueue_ai(
                account,
                "same-scan",
                "s3",
                context_hash="same-hash",
                payload_path=f"{account}.json",
            )

        by_account = {task.account_id: task for task in store.tasks()}
        first = by_account["111111111111"]
        second = by_account["222222222222"]
        assert first.task_id != second.task_id
        store.transition_task(first.task_id, "running")
        store.transition_task(first.task_id, "failed", error_category="throttling")
        store.transition_task(first.task_id, "pending")
        store.transition_task(first.task_id, "running")

        running = store.tasks(status="running")
        assert running[0].account_id == "111111111111"
        assert running[0].attempts == 2
        assert store.tasks()[0].account_id == "222222222222"


def test_file_sha256_reads_large_files_in_chunks(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"a" * (1024 * 1024 + 1))

    assert file_sha256(payload) == hashlib.sha256(payload.read_bytes()).hexdigest()


def test_worker_claim_is_ordered_and_running_jobs_can_be_recovered(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run("123456789012", "scan-a", status="deterministic_published")
        first_id = store.enqueue_ai(
            "123456789012",
            "scan-a",
            "athena",
            context_hash="first",
            payload_path="first.json",
        )
        store.enqueue_ai(
            "123456789012",
            "scan-a",
            "s3",
            context_hash="second",
            payload_path="second.json",
        )

        claimed = store.claim_next()

        assert claimed is not None
        assert claimed.task_id == first_id
        assert claimed.status == "running"
        assert claimed.attempts == 1
        assert store.requeue_running() == 1
        pending = {task.task_id: task for task in store.tasks()}
        assert pending[first_id].error_category == "worker_restarted"


def test_new_scan_supersedes_only_older_contextual_work(tmp_path: Path) -> None:
    account = "123456789012"
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(account, "scan-a", status="deterministic_published")
        old_task = store.enqueue_ai(
            account,
            "scan-a",
            "s3",
            context_hash="old",
            payload_path="old.json",
        )
        store.create_run(account, "scan-m", status="collecting")
        store.create_run(account, "scan-z", status="created")

        runs, jobs = store.supersede_older_context(account, "scan-z")

        assert (runs, jobs) == (1, 1)
        assert store.run_status(account, "scan-a") == "superseded"
        assert store.run_status(account, "scan-m") == "collecting"
        assert store.run_status(account, "scan-z") == "created"
        superseded = store.tasks(status="superseded")
        assert superseded[0].task_id == old_task
        assert superseded[0].error_category == "newer_scan"


def test_ai_queue_limit_is_audited_without_blocking_checkpoint(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb", max_pending_ai_jobs=1) as store:
        store.create_run("111111111111", "scan-a", status="deterministic_published")
        store.create_run("222222222222", "scan-b", status="deterministic_published")
        accepted = store.enqueue_ai(
            "111111111111",
            "scan-a",
            "s3",
            context_hash="accepted",
            payload_path="accepted.json",
        )
        rejected = store.enqueue_ai(
            "222222222222",
            "scan-b",
            "athena",
            context_hash="rejected",
            payload_path="rejected.json",
        )

        stats = store.queue_stats()

        assert accepted
        assert rejected == ""
        assert stats.pending == 1
        assert stats.rejected == 1
        assert stats.oldest_pending_ms >= 0
