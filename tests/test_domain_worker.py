from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from julius.analysis.domain_worker import (
    DOMAIN_RESULT_SCHEMA_VERSION,
    DomainResultError,
    InboxDomainProvider,
    merge_results,
    process_next,
)
from julius.cli import app
from julius.state import RunStore

ACCOUNT = "123456789012"
SCAN = "scan-20260803-180000-000001"


class FakeProvider:
    name = "fake"

    def __init__(self, mutate=None, error: Exception | None = None) -> None:
        self.mutate = mutate
        self.error = error
        self.calls = 0

    def analyze(self, checkpoint, *, task):
        del task
        self.calls += 1
        if self.error is not None:
            raise self.error
        payload = {
            "schema_version": DOMAIN_RESULT_SCHEMA_VERSION,
            "account_id": checkpoint["account"]["id"],
            "scan_id": checkpoint["scan_id"],
            "domain": checkpoint["domain"],
            "context_hash": checkpoint["context_hash_for_test"],
            "provider": self.name,
            "status": "enriched",
            "summary": "Contexto adicional sem alterar o motor.",
            "observations": [
                {
                    "subject_id": "job-a",
                    "diagnosis": "Evidência contextual.",
                    "implementation_steps": ["Revisar com o time dono."],
                    "risks": ["Validar dependências."],
                    "evidence_refs": ["payload.glue_jobs[0]"],
                }
            ],
            "suspected_injections": [],
        }
        if self.mutate is not None:
            self.mutate(payload)
        return payload


def _checkpoint(store: RunStore, root: Path, *, domain: str = "glue") -> str:
    path = root / f"{domain}.json"
    payload = {
        "checkpoint_schema_version": "1.0",
        "account": {"id": ACCOUNT, "region": "sa-east-1"},
        "scan_id": SCAN,
        "domain": domain,
        "payload": {"glue_jobs": []},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    payload["context_hash_for_test"] = digest
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    # O provider precisa devolver o hash do arquivo final, então estabilizamos o
    # campo de teste fora do hash e o substituímos após registrar o checkpoint.
    path.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    payload["context_hash_for_test"] = digest
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # O worker não exige esse campo no checkpoint; o fake o usa como canal para
    # montar a resposta. Atualizar o valor mudaria o hash indefinidamente, então
    # o teste injeta o hash correto no provider ao preparar a chamada.
    store.checkpoint(
        ACCOUNT,
        SCAN,
        domain,
        status="ready",
        payload_path=str(path),
        payload_hash=digest,
        sources={"Glue Jobs": "ok"},
    )
    store.enqueue_ai(
        ACCOUNT,
        SCAN,
        domain,
        context_hash=digest,
        payload_path=str(path),
    )
    return digest


def _provider_for_hash(digest: str, **kwargs) -> FakeProvider:
    original_mutate = kwargs.pop("mutate", None)

    def mutate(payload):
        payload["context_hash"] = digest
        if original_mutate is not None:
            original_mutate(payload)

    return FakeProvider(mutate=mutate, **kwargs)


def test_worker_valida_persiste_e_completa_sem_boto3(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_published")
        digest = _checkpoint(store, tmp_path)
        provider = _provider_for_hash(digest)

        outcome = process_next(store, provider, tmp_path / "results")

        assert outcome is not None and outcome.status == "completed"
        assert provider.calls == 1
        assert store.tasks(status="completed")[0].context_hash == digest
        assert store.run_status(ACCOUNT, SCAN) == "ai_partial"
        results = store.contextual_results(ACCOUNT, SCAN)
        assert len(results) == 1
        assert Path(results[0].result_path).is_file()


def test_worker_recusa_campo_deterministico_injetado(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_published")
        digest = _checkpoint(store, tmp_path)
        provider = _provider_for_hash(
            digest, mutate=lambda payload: payload.__setitem__("monthly_savings", 999)
        )

        outcome = process_next(store, provider, tmp_path / "results")

        assert outcome is not None and outcome.error_category == "invalid_result"
        assert store.contextual_results(ACCOUNT, SCAN) == []


def test_worker_isola_falha_do_provider(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_published")
        _checkpoint(store, tmp_path)

        outcome = process_next(
            store,
            FakeProvider(error=RuntimeError("provider offline")),
            tmp_path / "results",
        )

        assert outcome is not None and outcome.error_category == "provider_error"
        assert store.tasks(status="failed")[0].attempts == 1


def test_inbox_ausente_recoloca_job_sem_bloquear(tmp_path: Path) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_published")
        _checkpoint(store, tmp_path)

        outcome = process_next(
            store, InboxDomainProvider(tmp_path / "inbox"), tmp_path / "results"
        )

        assert outcome is not None and outcome.status == "pending"
        assert outcome.error_category == "result_not_ready"
        assert len(store.tasks(status="pending")) == 1


def test_job_de_checkpoint_anterior_e_superseded_sem_chamar_provider(
    tmp_path: Path,
) -> None:
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_published")
        _checkpoint(store, tmp_path)
        replacement = tmp_path / "replacement.json"
        replacement.write_text('{"new":true}')
        replacement_hash = hashlib.sha256(replacement.read_bytes()).hexdigest()
        store.checkpoint(
            ACCOUNT,
            SCAN,
            "glue",
            status="ready",
            payload_path=str(replacement),
            payload_hash=replacement_hash,
            sources={"Glue Jobs": "ok"},
        )
        provider = FakeProvider()

        outcome = process_next(store, provider, tmp_path / "results")

        assert outcome is not None and outcome.status == "superseded"
        assert provider.calls == 0


def test_merge_revalida_hash_e_nao_muta_dataset(tmp_path: Path) -> None:
    deterministic = {"account": ACCOUNT, "opportunities": [{"priority": 1}]}
    with RunStore(tmp_path / "runs.duckdb") as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_published")
        digest = _checkpoint(store, tmp_path)
        process_next(store, _provider_for_hash(digest), tmp_path / "results")

        output = merge_results(store, ACCOUNT, SCAN, tmp_path / "merged.json")
        merged = json.loads(output.read_text())

        assert merged["status"] == "enriched"
        assert merged["results"][0]["domain"] == "glue"
        assert deterministic == {
            "account": ACCOUNT,
            "opportunities": [{"priority": 1}],
        }

        result_path = Path(store.contextual_results(ACCOUNT, SCAN)[0].result_path)
        result_path.write_text("corrompido")
        with pytest.raises(DomainResultError, match="corrompido"):
            merge_results(store, ACCOUNT, SCAN, tmp_path / "other.json")


def test_cli_processa_inbox_e_mescla_resultados(tmp_path: Path) -> None:
    store_path = tmp_path / "runs.duckdb"
    inbox = tmp_path / "inbox"
    results = tmp_path / "results"
    with RunStore(store_path) as store:
        store.create_run(ACCOUNT, SCAN, status="deterministic_published")
        digest = _checkpoint(store, tmp_path)
        task = store.tasks()[0]
        checkpoint = json.loads(Path(task.payload_path).read_text())
        payload = _provider_for_hash(digest).analyze(checkpoint, task=task)
        payload["provider"] = "file"
        response_path = (
            inbox / ACCOUNT / SCAN / f"glue-{digest[:16]}.json"
        )
        response_path.parent.mkdir(parents=True)
        response_path.write_text(json.dumps(payload), encoding="utf-8")

    worked = CliRunner().invoke(
        app,
        [
            "agent",
            "work-domains",
            "--run-store",
            str(store_path),
            "--inbox",
            str(inbox),
            "--output",
            str(results),
        ],
    )
    assert worked.exit_code == 0, worked.output
    assert "Nenhum cliente boto3 foi criado" in worked.output

    merged_path = tmp_path / "merged.json"
    merged = CliRunner().invoke(
        app,
        [
            "agent",
            "merge-domains",
            "--run-store",
            str(store_path),
            "--account-id",
            ACCOUNT,
            "--scan-id",
            SCAN,
            "--output",
            str(merged_path),
        ],
    )
    assert merged.exit_code == 0, merged.output
    assert merged_path.is_file()
