"""Histórico analítico do MVP 1B em DuckDB, com exportação Parquet.

O backlog JSON continua responsável por reconciliar o ciclo de vida operacional.
Este módulo guarda snapshots imutáveis de cada execução e as revisões humanas,
permitindo medir Precision@10 e falsos positivos ao longo do tempo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from julius.collection.models import Account
from julius.opportunities.base import Opportunity
from julius.opportunities.lifecycle import LifecycleEvent
from julius.state.diff import DiffEvent
from julius.state.validation import ValidationResult


@dataclass(frozen=True)
class ReviewSummary:
    reviewed: int = 0
    true_positives: int = 0
    false_positives: int = 0
    precision: float | None = None
    false_positive_rate: float | None = None


@dataclass(frozen=True)
class CalibrationFactor:
    rule_id: str
    sample_count: int
    predicted_total: float
    realized_total: float
    factor: float
    mean_precision: float


@dataclass(frozen=True)
class BenefitSummary:
    validations: int = 0
    predicted_monthly: float = 0.0
    realized_monthly: float = 0.0
    realization_rate: float | None = None


@dataclass(frozen=True)
class LifecycleLeadTimes:
    detected_to_accepted_days: float | None = None
    accepted_to_implemented_days: float | None = None
    implemented_to_validated_days: float | None = None


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

            CREATE TABLE IF NOT EXISTS lifecycle_events (
                fingerprint VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                opportunity_id VARCHAR NOT NULL,
                occurred_at TIMESTAMP NOT NULL,
                from_status VARCHAR NOT NULL,
                to_status VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                automatic BOOLEAN NOT NULL,
                PRIMARY KEY (fingerprint, occurred_at, to_status)
            );

            CREATE TABLE IF NOT EXISTS diff_events (
                scan_id VARCHAR NOT NULL,
                event_type VARCHAR NOT NULL,
                fingerprint VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                asset_name VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                previous_value DOUBLE,
                current_value DOUBLE,
                details_json VARCHAR NOT NULL
            );

            CREATE TABLE IF NOT EXISTS validations (
                fingerprint VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                opportunity_id VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                validated_at TIMESTAMP NOT NULL,
                predicted_monthly DOUBLE NOT NULL,
                realized_monthly DOUBLE NOT NULL,
                absolute_saving DOUBLE NOT NULL,
                baseline_cost DOUBLE NOT NULL,
                after_cost DOUBLE NOT NULL,
                baseline_volume DOUBLE,
                after_volume DOUBLE,
                baseline_cost_per_unit DOUBLE,
                after_cost_per_unit DOUBLE,
                normalized_saving DOUBLE,
                estimation_precision DOUBLE NOT NULL,
                realization_rate DOUBLE,
                performance_change_pct DOUBLE,
                failure_rate_change_pct DOUBLE,
                actor VARCHAR NOT NULL,
                notes VARCHAR NOT NULL,
                PRIMARY KEY (fingerprint, validated_at)
            );

            CREATE TABLE IF NOT EXISTS athena_recommendation_baselines (
                scan_id VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                workgroup VARCHAR NOT NULL,
                fingerprint VARCHAR NOT NULL,
                actor VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                period VARCHAR NOT NULL,
                currency VARCHAR NOT NULL,
                allocated_cost DOUBLE,
                billed_bytes BIGINT NOT NULL,
                executions INTEGER NOT NULL,
                cost_per_execution DOUBLE,
                reuse_count INTEGER NOT NULL,
                successor_fingerprint VARCHAR,
                PRIMARY KEY (scan_id, account, fingerprint, actor, rule_id)
            );
            """
        )
        self._db.execute(
            "ALTER TABLE opportunity_snapshots "
            "ADD COLUMN IF NOT EXISTS evidence_hash VARCHAR DEFAULT ''"
        )
        self._db.execute(
            "ALTER TABLE opportunity_snapshots "
            "ADD COLUMN IF NOT EXISTS urgency DOUBLE DEFAULT 1.0"
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
                o.evidence_signature(),
                o.urgency,
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
                    INSERT INTO opportunity_snapshots (
                        scan_id, fingerprint, opportunity_id, account, rule_id,
                        asset_type, asset_name, bucket, execution_priority,
                        monthly_expected, confidence, actionable, status,
                        first_seen, last_seen, evidence_hash, urgency
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            self._record_athena_baselines(account, opportunities, scan_id)
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def _record_athena_baselines(
        self, account: Account, opportunities: list[Opportunity], scan_id: str
    ) -> None:
        """Persiste somente agregados aprovados; nunca execuções ou SQL."""
        eligible = {
            (opportunity.asset_name, opportunity.rule_id)
            for opportunity in opportunities
            if opportunity.asset_type == "athena_query"
            and opportunity.status in {"accepted", "planned", "implemented"}
        }
        self._db.execute(
            "DELETE FROM athena_recommendation_baselines WHERE scan_id = ? AND account = ?",
            [scan_id, account.account_id],
        )
        rows = []
        for query in account.athena_queries:
            rules = [rule for asset, rule in eligible if asset == query.query_id]
            for rule in rules:
                for actor in query.actors or ["desconhecido"]:
                    rows.append(
                        [
                            scan_id,
                            account.account_id,
                            query.workgroup,
                            query.structural_fingerprint or query.query_id,
                            actor,
                            rule,
                            account.athena_coverage.window_start
                            + "/"
                            + account.athena_coverage.window_end
                            if account.athena_coverage else account.period,
                            query.currency or account.currency,
                            query.allocated_cost,
                            query.billed_bytes,
                            query.observed_runs,
                            query.allocated_cost / query.observed_runs
                            if query.allocated_cost is not None and query.observed_runs else None,
                            query.reused_runs,
                            None,
                        ]
                    )
        if rows:
            self._db.executemany(
                """
                INSERT INTO athena_recommendation_baselines VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                rows,
            )

    def link_athena_successor(
        self, account: str, fingerprint: str, successor_fingerprint: str
    ) -> int:
        """Associação manual de uma query reescrita ao baseline anterior."""
        result = self._db.execute(
            """
            UPDATE athena_recommendation_baselines
            SET successor_fingerprint = ?
            WHERE account = ? AND fingerprint = ?
            """,
            [successor_fingerprint, account, fingerprint],
        )
        return int(result.rowcount if result.rowcount >= 0 else 0)

    def compare_athena_baseline(
        self, account: str, fingerprint: str, current
    ) -> dict | None:
        """Compara agregados mensais sem inferir economia quando a query sumiu."""
        row = self._db.execute(
            """
            SELECT b.allocated_cost, b.billed_bytes, b.executions, b.reuse_count,
                   b.currency, b.successor_fingerprint
            FROM athena_recommendation_baselines b
            JOIN runs r USING (scan_id, account)
            WHERE b.account = ? AND b.fingerprint = ?
            ORDER BY r.scanned_on DESC, b.scan_id DESC
            LIMIT 1
            """,
            [account, fingerprint],
        ).fetchone()
        if row is None:
            return None
        baseline_cost, baseline_bytes, baseline_executions, baseline_reuse, currency, successor = row
        if current is None:
            return {
                "status": "not_observed",
                "currency": currency,
                "successor_fingerprint": successor,
                "estimated_saving": None,
                "note": "desaparecimento não equivale automaticamente a 100% de economia",
            }
        current_cost_per_execution = (
            current.allocated_cost / current.observed_runs
            if current.allocated_cost is not None and current.observed_runs else None
        )
        baseline_cost_per_execution = (
            baseline_cost / baseline_executions
            if baseline_cost is not None and baseline_executions else None
        )
        return {
            "status": "compared",
            "currency": currency,
            "cost_total_change": _ratio_change(baseline_cost, current.allocated_cost),
            "cost_per_execution_change": _ratio_change(
                baseline_cost_per_execution, current_cost_per_execution
            ),
            "bytes_per_execution_change": _ratio_change(
                baseline_bytes / baseline_executions if baseline_executions else None,
                current.billed_bytes / current.observed_runs if current.observed_runs else None,
            ),
            "frequency_change": _ratio_change(baseline_executions, current.observed_runs),
            "reuse_change": current.reused_runs - baseline_reuse,
            "successor_fingerprint": successor,
        }

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

    def latest_snapshots(self, account: str) -> list[dict]:
        """Último snapshot completo anterior à próxima execução da conta."""
        row = self._db.execute(
            """
            SELECT scan_id FROM runs
            WHERE account = ?
            ORDER BY scanned_on DESC, scan_id DESC
            LIMIT 1
            """,
            [account],
        ).fetchone()
        if row is None:
            return []
        cursor = self._db.execute(
            """
            SELECT fingerprint, opportunity_id, account, rule_id, asset_type,
                   asset_name, bucket, execution_priority, monthly_expected,
                   confidence, actionable, status, first_seen, last_seen,
                   evidence_hash, urgency
            FROM opportunity_snapshots
            WHERE account = ? AND scan_id = ?
            """,
            [account, row[0]],
        )
        columns = [item[0] for item in cursor.description]
        return [
            dict(zip(columns, values, strict=True)) for values in cursor.fetchall()
        ]

    def record_diff_events(self, scan_id: str, events: list[DiffEvent]) -> None:
        if not events:
            return
        self._db.executemany(
            """
            INSERT INTO diff_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                [
                    scan_id,
                    event.event_type,
                    event.fingerprint,
                    event.account,
                    event.asset_name,
                    event.rule_id,
                    event.previous_value,
                    event.current_value,
                    json.dumps(event.details, ensure_ascii=False, sort_keys=True),
                ]
                for event in events
            ],
        )

    def record_lifecycle_event(self, event: LifecycleEvent) -> None:
        self._db.execute(
            """
            INSERT INTO lifecycle_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event.fingerprint,
                event.account,
                event.opportunity_id,
                event.occurred_at.replace(tzinfo=None),
                event.from_status,
                event.to_status,
                event.actor,
                event.reason,
                event.automatic,
            ],
        )

    def record_validation(self, result: ValidationResult) -> None:
        self._db.execute(
            """
            INSERT INTO validations VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                result.fingerprint,
                result.account,
                result.opportunity_id,
                result.rule_id,
                result.validated_at.replace(tzinfo=None),
                result.predicted_monthly,
                result.realized_monthly,
                result.absolute_saving,
                result.baseline_cost,
                result.after_cost,
                result.baseline_volume,
                result.after_volume,
                result.baseline_cost_per_unit,
                result.after_cost_per_unit,
                result.normalized_saving,
                result.estimation_precision,
                result.realization_rate,
                result.performance_change_pct,
                result.failure_rate_change_pct,
                result.actor,
                result.notes,
            ],
        )

    def calibration_for(
        self, rule_id: str, *, minimum_samples: int = 3
    ) -> CalibrationFactor | None:
        row = self._db.execute(
            """
            SELECT count(*) AS samples,
                   sum(predicted_monthly) AS predicted,
                   sum(realized_monthly) AS realized,
                   avg(estimation_precision) AS mean_precision
            FROM validations
            WHERE rule_id = ?
            """,
            [rule_id],
        ).fetchone()
        if row is None or int(row[0]) < minimum_samples or float(row[1] or 0) <= 0:
            return None
        raw_factor = float(row[2]) / float(row[1])
        return CalibrationFactor(
            rule_id=rule_id,
            sample_count=int(row[0]),
            predicted_total=round(float(row[1]), 2),
            realized_total=round(float(row[2]), 2),
            factor=round(max(0.25, min(2.0, raw_factor)), 4),
            mean_precision=round(float(row[3] or 0), 4),
        )

    def benefit_summary(self, account: str) -> BenefitSummary:
        row = self._db.execute(
            """
            WITH latest AS (
                SELECT *, row_number() OVER (
                    PARTITION BY fingerprint ORDER BY validated_at DESC
                ) AS position
                FROM validations
                WHERE account = ?
            )
            SELECT count(*), sum(predicted_monthly), sum(realized_monthly)
            FROM latest WHERE position = 1
            """,
            [account],
        ).fetchone()
        if row is None:
            return BenefitSummary()
        count = int(row[0] or 0)
        predicted = float(row[1] or 0.0)
        realized = float(row[2] or 0.0)
        return BenefitSummary(
            validations=count,
            predicted_monthly=round(predicted, 2),
            realized_monthly=round(realized, 2),
            realization_rate=round(realized / predicted, 4)
            if predicted > 0
            else None,
        )

    def latest_validations(self, account: str, *, limit: int = 10) -> list[dict]:
        cursor = self._db.execute(
            """
            SELECT opportunity_id, rule_id, validated_at, predicted_monthly,
                   realized_monthly, estimation_precision, realization_rate,
                   normalized_saving, actor, notes
            FROM validations
            WHERE account = ?
            ORDER BY validated_at DESC
            LIMIT ?
            """,
            [account, limit],
        )
        columns = [item[0] for item in cursor.description]
        return [
            dict(zip(columns, values, strict=True)) for values in cursor.fetchall()
        ]

    def lifecycle_lead_times(self, account: str) -> LifecycleLeadTimes:
        row = self._db.execute(
            """
            WITH first_seen AS (
                SELECT fingerprint,
                       min(try_cast(nullif(first_seen, '') AS DATE)) AS detected_at
                FROM opportunity_snapshots
                WHERE account = ?
                GROUP BY fingerprint
            ), milestones AS (
                SELECT fingerprint,
                       min(CASE WHEN to_status = 'accepted' THEN occurred_at END) accepted_at,
                       min(CASE WHEN to_status = 'implemented' THEN occurred_at END) implemented_at,
                       min(CASE WHEN to_status = 'validated' THEN occurred_at END) validated_at
                FROM lifecycle_events
                WHERE account = ?
                GROUP BY fingerprint
            )
            SELECT
                avg(date_diff('day', detected_at, accepted_at)),
                avg(date_diff('day', accepted_at, implemented_at)),
                avg(date_diff('day', implemented_at, validated_at))
            FROM first_seen JOIN milestones USING (fingerprint)
            """,
            [account, account],
        ).fetchone()
        if row is None:
            return LifecycleLeadTimes()
        return LifecycleLeadTimes(
            detected_to_accepted_days=_round_optional(row[0]),
            accepted_to_implemented_days=_round_optional(row[1]),
            implemented_to_validated_days=_round_optional(row[2]),
        )

    def run_count(self) -> int:
        """Quantidade de snapshots de execução persistidos."""
        row = self._db.execute("SELECT count(*) FROM runs").fetchone()
        return int(row[0]) if row else 0

    def export_parquet(self, directory: str | Path) -> list[Path]:
        """Exporta tabelas analíticas para Parquet comprimido e reproduzível."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for table in (
            "runs",
            "opportunity_snapshots",
            "reviews",
            "lifecycle_events",
            "diff_events",
            "validations",
        ):
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


def _round_optional(value) -> float | None:
    return round(float(value), 2) if value is not None else None


def _ratio_change(previous, current) -> float | None:
    if previous is None or current is None or float(previous) == 0:
        return None
    return round((float(current) - float(previous)) / float(previous), 4)
