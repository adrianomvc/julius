"""Estado retomável do pipeline determinístico e contextual."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_RUN_TRANSITIONS = {
    "created": {"collecting", "deterministic_published", "cancelled"},
    "collecting": {"collection_partial", "deterministic_ready", "cancelled"},
    "collection_partial": {"deterministic_ready", "cancelled"},
    "deterministic_ready": {"deterministic_published", "cancelled"},
    "deterministic_published": {"ai_pending", "enriched", "ai_failed"},
    "ai_pending": {"ai_partial", "enriched", "ai_failed", "superseded"},
    "ai_partial": {"enriched", "ai_failed", "superseded"},
    "ai_failed": {"ai_pending", "superseded"},
    "enriched": set(),
    "cancelled": set(),
    "superseded": set(),
}
_TASK_TRANSITIONS = {
    "pending": {"running", "superseded"},
    "running": {"completed", "failed", "pending", "superseded"},
    "failed": {"pending", "superseded"},
    "completed": set(),
    "superseded": set(),
}


@dataclass(frozen=True)
class RunTask:
    task_id: str
    account_id: str
    scan_id: str
    domain: str
    status: str
    context_hash: str
    payload_path: str
    attempts: int = 0
    error_category: str = ""


@dataclass(frozen=True)
class DomainCheckpoint:
    account_id: str
    scan_id: str
    domain: str
    status: str
    payload_path: str
    payload_hash: str
    sources: dict[str, str]


class RunStore:
    """Ledger DuckDB local; nenhum worker de IA toca a coleta boto3."""

    def __init__(self, path: str | Path) -> None:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("DuckDB não está instalado") from exc
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = duckdb.connect(str(self.path))
        self._lock = Lock()
        self._create_schema()

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _create_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                account_id VARCHAR NOT NULL,
                scan_id VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                deterministic_path VARCHAR DEFAULT '',
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (account_id, scan_id)
            );
            CREATE TABLE IF NOT EXISTS domain_checkpoints (
                account_id VARCHAR NOT NULL,
                scan_id VARCHAR NOT NULL,
                domain VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                payload_path VARCHAR NOT NULL,
                payload_hash VARCHAR NOT NULL,
                sources_json VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL,
                PRIMARY KEY (account_id, scan_id, domain)
            );
            CREATE TABLE IF NOT EXISTS ai_jobs (
                task_id VARCHAR PRIMARY KEY,
                account_id VARCHAR NOT NULL,
                scan_id VARCHAR NOT NULL,
                domain VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                context_hash VARCHAR NOT NULL,
                payload_path VARCHAR NOT NULL,
                attempts INTEGER NOT NULL,
                error_category VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
            """
        )

    def create_run(
        self,
        account_id: str,
        scan_id: str,
        *,
        status: str = "created",
        deterministic_path: str = "",
    ) -> None:
        _require_run_status(status)
        now = _now()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO pipeline_runs VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (account_id, scan_id) DO NOTHING
                """,
                [account_id, scan_id, status, deterministic_path, now, now],
            )

    def transition(self, account_id: str, scan_id: str, status: str) -> None:
        _require_run_status(status)
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM pipeline_runs WHERE account_id=? AND scan_id=?",
                [account_id, scan_id],
            ).fetchone()
            if row is None:
                raise KeyError(f"run desconhecido: {account_id}/{scan_id}")
            current = str(row[0])
            if status == current:
                return
            if status not in _RUN_TRANSITIONS[current]:
                raise ValueError(f"transição de run inválida: {current} -> {status}")
            self._db.execute(
                """UPDATE pipeline_runs SET status=?, updated_at=?
                   WHERE account_id=? AND scan_id=?""",
                [status, _now(), account_id, scan_id],
            )

    def run_status(self, account_id: str, scan_id: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM pipeline_runs WHERE account_id=? AND scan_id=?",
                [account_id, scan_id],
            ).fetchone()
        if row is None:
            raise KeyError(f"run desconhecido: {account_id}/{scan_id}")
        return str(row[0])

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
    ) -> None:
        if status not in {"ready", "partial", "unavailable"}:
            raise ValueError(f"estado de checkpoint inválido: {status}")
        with self._lock:
            self._db.execute(
                """
                INSERT INTO domain_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (account_id, scan_id, domain) DO UPDATE SET
                    status=excluded.status,
                    payload_path=excluded.payload_path,
                    payload_hash=excluded.payload_hash,
                    sources_json=excluded.sources_json,
                    created_at=excluded.created_at
                """,
                [
                    account_id,
                    scan_id,
                    domain,
                    status,
                    payload_path,
                    payload_hash,
                    json.dumps(sources, sort_keys=True),
                    _now(),
                ],
            )

    def checkpoints(
        self, account_id: str, scan_id: str
    ) -> list[DomainCheckpoint]:
        with self._lock:
            rows = self._db.execute(
                """SELECT account_id, scan_id, domain, status, payload_path,
                          payload_hash, sources_json
                   FROM domain_checkpoints
                   WHERE account_id=? AND scan_id=? ORDER BY domain""",
                [account_id, scan_id],
            ).fetchall()
        return [
            DomainCheckpoint(
                account_id=str(row[0]),
                scan_id=str(row[1]),
                domain=str(row[2]),
                status=str(row[3]),
                payload_path=str(row[4]),
                payload_hash=str(row[5]),
                sources=json.loads(str(row[6])),
            )
            for row in rows
        ]

    def verified_checkpoint(
        self, account_id: str, scan_id: str, domain: str
    ) -> DomainCheckpoint:
        matches = [
            item
            for item in self.checkpoints(account_id, scan_id)
            if item.domain == domain
        ]
        if not matches:
            raise KeyError(f"checkpoint desconhecido: {account_id}/{scan_id}/{domain}")
        checkpoint = matches[0]
        path = Path(checkpoint.payload_path)
        if not path.is_file() or file_sha256(path) != checkpoint.payload_hash:
            raise ValueError(
                f"checkpoint inválido ou corrompido: {account_id}/{scan_id}/{domain}"
            )
        return checkpoint

    def publish_deterministic(
        self,
        account_id: str,
        scan_id: str,
        deterministic_path: str,
    ) -> None:
        """Publica o dataset final sem esperar jobs contextuais de domínio."""
        with self._lock:
            self._db.execute(
                """UPDATE pipeline_runs SET deterministic_path=?, updated_at=?
                   WHERE account_id=? AND scan_id=?""",
                [deterministic_path, _now(), account_id, scan_id],
            )
        self.transition(account_id, scan_id, "deterministic_published")
        with self._lock:
            row = self._db.execute(
                """SELECT count(*) FROM ai_jobs
                   WHERE account_id=? AND scan_id=?
                     AND status NOT IN ('completed', 'superseded')""",
                [account_id, scan_id],
            ).fetchone()
        if row is not None and int(row[0]):
            self.transition(account_id, scan_id, "ai_pending")

    def enqueue_ai(
        self,
        account_id: str,
        scan_id: str,
        domain: str,
        *,
        context_hash: str,
        payload_path: str,
    ) -> str:
        task_id = hashlib.sha256(
            f"{account_id}|{scan_id}|{domain}|{context_hash}".encode()
        ).hexdigest()[:24]
        now = _now()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO ai_jobs VALUES (?, ?, ?, ?, 'pending', ?, ?, 0, '', ?, ?)
                ON CONFLICT (task_id) DO NOTHING
                """,
                [
                    task_id,
                    account_id,
                    scan_id,
                    domain,
                    context_hash,
                    payload_path,
                    now,
                    now,
                ],
            )
        if self.run_status(account_id, scan_id) == "deterministic_published":
            self.transition(account_id, scan_id, "ai_pending")
        return task_id

    def tasks(self, *, status: str = "pending") -> list[RunTask]:
        with self._lock:
            rows = self._db.execute(
                """SELECT task_id, account_id, scan_id, domain, status,
                          context_hash, payload_path, attempts, error_category
                   FROM ai_jobs WHERE status=? ORDER BY created_at, task_id""",
                [status],
            ).fetchall()
        return [RunTask(*row) for row in rows]

    def claim_next(self) -> RunTask | None:
        """Reserva atomicamente o próximo job; worker nenhum toca boto3."""
        with self._lock:
            row = self._db.execute(
                """UPDATE ai_jobs SET status='running', attempts=attempts + 1,
                          error_category='', updated_at=?
                   WHERE task_id=(
                       SELECT task_id FROM ai_jobs WHERE status='pending'
                       ORDER BY created_at, task_id LIMIT 1
                   ) AND status='pending'
                   RETURNING task_id, account_id, scan_id, domain, status,
                             context_hash, payload_path, attempts, error_category""",
                [_now()],
            ).fetchone()
        return RunTask(*row) if row is not None else None

    def requeue_running(self, *, error_category: str = "worker_restarted") -> int:
        """Recupera jobs abandonados depois de queda do executor de IA."""
        with self._lock:
            row = self._db.execute(
                "SELECT count(*) FROM ai_jobs WHERE status='running'"
            ).fetchone()
            count = int(row[0]) if row is not None else 0
            self._db.execute(
                """UPDATE ai_jobs SET status='pending', error_category=?, updated_at=?
                   WHERE status='running'""",
                [error_category, _now()],
            )
        return count

    def task_for_context(
        self,
        account_id: str,
        scan_id: str,
        domain: str,
        context_hash: str,
    ) -> RunTask:
        """Localiza o job exato; hash impede validar um contexto obsoleto."""
        with self._lock:
            row = self._db.execute(
                """SELECT task_id, account_id, scan_id, domain, status,
                          context_hash, payload_path, attempts, error_category
                   FROM ai_jobs
                   WHERE account_id=? AND scan_id=? AND domain=? AND context_hash=?""",
                [account_id, scan_id, domain, context_hash],
            ).fetchone()
        if row is None:
            raise KeyError(
                f"job desconhecido: {account_id}/{scan_id}/{domain}/{context_hash}"
            )
        return RunTask(*row)

    def transition_task(
        self, task_id: str, status: str, *, error_category: str = ""
    ) -> None:
        if status not in _TASK_TRANSITIONS:
            raise ValueError(f"estado de job inválido: {status}")
        with self._lock:
            row = self._db.execute(
                "SELECT status, attempts FROM ai_jobs WHERE task_id=?", [task_id]
            ).fetchone()
            if row is None:
                raise KeyError(f"job desconhecido: {task_id}")
            current, attempts = str(row[0]), int(row[1])
            if status == current:
                return
            if status not in _TASK_TRANSITIONS[current]:
                raise ValueError(f"transição de job inválida: {current} -> {status}")
            self._db.execute(
                """UPDATE ai_jobs SET status=?, attempts=?, error_category=?, updated_at=?
                   WHERE task_id=?""",
                [
                    status,
                    attempts + int(status == "running"),
                    error_category,
                    _now(),
                    task_id,
                ],
            )

    def complete_ai(self, task_id: str) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT account_id, scan_id FROM ai_jobs WHERE task_id=?", [task_id]
            ).fetchone()
        if row is None:
            raise KeyError(f"job desconhecido: {task_id}")
        self.transition_task(task_id, "completed")
        account_id, scan_id = str(row[0]), str(row[1])
        with self._lock:
            count_row = self._db.execute(
                """SELECT sum(CASE WHEN status NOT IN ('completed', 'superseded')
                                    THEN 1 ELSE 0 END),
                          max(CASE WHEN domain='cross_service' AND status='completed'
                                   THEN 1 ELSE 0 END)
                   FROM ai_jobs
                   WHERE account_id=? AND scan_id=?""",
                [account_id, scan_id],
            ).fetchone()
            if count_row is None:  # pragma: no cover - count sempre retorna uma linha
                raise RuntimeError("DuckDB não retornou a contagem de jobs")
            open_jobs = int(count_row[0])
            cross_service_completed = bool(count_row[1])
        if not open_jobs and self.run_status(account_id, scan_id) in {
            "ai_pending",
            "ai_partial",
        }:
            self.transition(
                account_id,
                scan_id,
                "enriched" if cross_service_completed else "ai_partial",
            )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_run_status(status: str) -> None:
    if status not in _RUN_TRANSITIONS:
        raise ValueError(f"estado de run inválido: {status}")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
