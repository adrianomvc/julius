"""Histórico analítico do MVP 1B em DuckDB, com exportação Parquet.

O backlog JSON continua responsável por reconciliar o ciclo de vida operacional.
Este módulo guarda snapshots imutáveis de cada execução e as revisões humanas,
permitindo medir Precision@10 e falsos positivos ao longo do tempo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from julius.inventory.model import Account
from julius.opportunities.base import Opportunity


@dataclass(frozen=True)
class ReviewSummary:
    reviewed: int = 0
    true_positives: int = 0
    false_positives: int = 0
    precision: float | None = None
    false_positive_rate: float | None = None


class HistoryStore:
    """Persiste execuções, oportunidades e revisões em um arquivo DuckDB."""

    def __init__(self, path: str | Path) -> None:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - mensagem para instalação quebrada
            raise RuntimeError(
                "DuckDB não está instalado. Rode `pip install -e .` para habilitar o histórico."
            ) from exc

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = duckdb.connect(str(self.path))
        self._create_schema()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> HistoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                scan_id VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                scanned_on DATE NOT NULL,
                source VARCHAR NOT NULL,
                opportunity_count INTEGER NOT NULL,
                identified_monthly DOUBLE NOT NULL,
                realizable_year DOUBLE NOT NULL,
                PRIMARY KEY (scan_id, account)
            );

            CREATE TABLE IF NOT EXISTS opportunity_snapshots (
                scan_id VARCHAR NOT NULL,
                fingerprint VARCHAR NOT NULL,
                opportunity_id VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                asset_type VARCHAR NOT NULL,
                asset_name VARCHAR NOT NULL,
                bucket VARCHAR NOT NULL,
                execution_priority INTEGER NOT NULL,
                monthly_expected DOUBLE NOT NULL,
                confidence DOUBLE NOT NULL,
                actionable BOOLEAN NOT NULL,
                status VARCHAR NOT NULL,
                first_seen VARCHAR NOT NULL,
                last_seen VARCHAR NOT NULL,
                PRIMARY KEY (scan_id, account, fingerprint)
            );

            CREATE TABLE IF NOT EXISTS reviews (
                fingerprint VARCHAR NOT NULL,
                opportunity_id VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                reviewed_at TIMESTAMP NOT NULL,
                is_true_positive BOOLEAN NOT NULL,
                reviewer VARCHAR NOT NULL,
                notes VARCHAR NOT NULL,
                PRIMARY KEY (fingerprint, reviewed_at)
            );
            """
        )

    def record_run(
        self,
        account: Account,
        opportunities: list[Opportunity],
        scan_id: str,
        *,
        source: str,
        scanned_on: date | None = None,
    ) -> None:
        """Grava um snapshot idempotente da análise."""
        scanned_on = scanned_on or date.today()
        identified = sum(
            o.estimated_gain.monthly_expected
            for o in opportunities
            if not o.estimated_gain.is_strategic
        )
        realizable = sum(o.estimated_gain.realizable_year for o in opportunities)

        rows = [
            [
                scan_id,
                o.fingerprint(),
                o.opportunity_id,
                o.account,
                o.rule_id,
                o.asset_type,
                o.asset_name,
                o.bucket,
                o.execution_priority,
                o.estimated_gain.monthly_expected,
                o.confidence,
                o.actionable,
                o.status,
                o.first_seen,
                o.last_seen,
            ]
            for o in opportunities
        ]
        self._db.execute("BEGIN TRANSACTION")
        try:
            self._db.execute(
                "DELETE FROM opportunity_snapshots WHERE scan_id = ? AND account = ?",
                [scan_id, account.account_id],
            )
            self._db.execute(
                "DELETE FROM runs WHERE scan_id = ? AND account = ?",
                [scan_id, account.account_id],
            )
            self._db.execute(
                """
                INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    scan_id,
                    account.account_id,
                    scanned_on,
                    source,
                    len(opportunities),
                    round(identified, 2),
                    round(realizable, 2),
                ],
            )
            if rows:
                self._db.executemany(
                    """
                    INSERT INTO opportunity_snapshots
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def record_review(
        self,
        opportunity: Opportunity,
        *,
        is_true_positive: bool,
        reviewer: str,
        notes: str = "",
        reviewed_at: datetime | None = None,
    ) -> None:
        """Registra uma decisão humana sem sobrescrever o histórico anterior."""
        reviewed_at = reviewed_at or datetime.now(timezone.utc)
        self._db.execute(
            """
            INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                opportunity.fingerprint(),
                opportunity.opportunity_id,
                opportunity.account,
                reviewed_at.replace(tzinfo=None),
                is_true_positive,
                reviewer,
                notes,
            ],
        )

    def labels_for(self, opportunities: list[Opportunity]) -> dict[str, bool]:
        """Retorna o rótulo humano mais recente de cada oportunidade atual."""
        if not opportunities:
            return {}
        fingerprints = [o.fingerprint() for o in opportunities]
        placeholders = ", ".join("?" for _ in fingerprints)
        rows = self._db.execute(
            f"""
            SELECT fingerprint, is_true_positive
            FROM (
                SELECT fingerprint, is_true_positive,
                       row_number() OVER (
                           PARTITION BY fingerprint ORDER BY reviewed_at DESC
                       ) AS position
                FROM reviews
                WHERE fingerprint IN ({placeholders})
            )
            WHERE position = 1
            """,
            fingerprints,
        ).fetchall()
        by_fingerprint = dict(rows)
        return {
            o.opportunity_id: bool(by_fingerprint[o.fingerprint()])
            for o in opportunities
            if o.fingerprint() in by_fingerprint
        }

    def review_summary(self, opportunities: list[Opportunity]) -> ReviewSummary:
        labels = self.labels_for(opportunities)
        true_positives = sum(1 for value in labels.values() if value)
        false_positives = sum(1 for value in labels.values() if not value)
        reviewed = true_positives + false_positives
        if not reviewed:
            return ReviewSummary()
        return ReviewSummary(
            reviewed=reviewed,
            true_positives=true_positives,
            false_positives=false_positives,
            precision=round(true_positives / reviewed, 3),
            false_positive_rate=round(false_positives / reviewed, 3),
        )

    def run_count(self) -> int:
        """Quantidade de snapshots de execução persistidos."""
        return int(self._db.execute("SELECT count(*) FROM runs").fetchone()[0])

    def export_parquet(self, directory: str | Path) -> list[Path]:
        """Exporta tabelas analíticas para Parquet comprimido e reproduzível."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for table in ("runs", "opportunity_snapshots", "reviews"):
            target = directory / f"{table}.parquet"
            escaped = str(target.resolve()).replace("'", "''")
            self._db.execute(
                f"""
                COPY (SELECT * FROM {table})
                TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
            written.append(target)
        return written
