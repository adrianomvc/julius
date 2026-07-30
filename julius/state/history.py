"""Histórico analítico do MVP 1B em DuckDB, com exportação Parquet.

O backlog JSON continua responsável por reconciliar o ciclo de vida operacional.
Este módulo guarda snapshots imutáveis de cada execução e as revisões humanas,
permitindo medir Precision@10 e falsos positivos ao longo do tempo.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from julius.collection.models import Account
from julius.findings.lifecycle import LifecycleEvent
from julius.findings.opportunity import Opportunity
from julius.state.diff import DiffEvent
from julius.state.signal_ledger import SignalDecision
from julius.state.validation import ValidationResult

_LEGACY_RULE_IDS = {"GLUE-IS-IDLE": "GLUE-IS-IDLE-TIMEOUT"}


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
    factor_low: float = 1.0
    factor_high: float = 1.0
    median_error: float = 0.0
    confidence: str = "low"
    segment: str = "rule"
    fallback_level: str = "rule"


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
                cadence VARCHAR DEFAULT 'weekly',
                financial_period VARCHAR DEFAULT '',
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
                technical_predicted_monthly DOUBLE,
                calibrated_predicted_monthly DOUBLE,
                eligible_for_calibration BOOLEAN DEFAULT FALSE,
                service VARCHAR DEFAULT '',
                workload_type VARCHAR DEFAULT '',
                modality VARCHAR DEFAULT '',
                cost_band VARCHAR DEFAULT '',
                evidence_quality VARCHAR DEFAULT '',
                PRIMARY KEY (fingerprint, validated_at)
            );

            CREATE TABLE IF NOT EXISTS signal_verdicts (
                fingerprint VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                rule_id VARCHAR NOT NULL,
                asset_type VARCHAR NOT NULL,
                asset_name VARCHAR NOT NULL,
                verdict VARCHAR NOT NULL,
                rationale VARCHAR NOT NULL,
                evidence_hash VARCHAR NOT NULL,
                scan_id VARCHAR NOT NULL,
                prompt_version VARCHAR NOT NULL,
                recorded_at TIMESTAMP NOT NULL,
                PRIMARY KEY (fingerprint, recorded_at)
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

            CREATE TABLE IF NOT EXISTS process_efficiency_snapshots (
                scan_id VARCHAR NOT NULL,
                account VARCHAR NOT NULL,
                service VARCHAR NOT NULL,
                process_name VARCHAR NOT NULL,
                metric VARCHAR NOT NULL,
                value DOUBLE NOT NULL,
                unit VARCHAR NOT NULL,
                recorded_on DATE NOT NULL,
                PRIMARY KEY (scan_id, account, service, process_name, metric)
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
        for definition in (
            "technical_predicted_monthly DOUBLE",
            "calibrated_predicted_monthly DOUBLE",
            "eligible_for_calibration BOOLEAN DEFAULT FALSE",
            "service VARCHAR DEFAULT ''",
            "workload_type VARCHAR DEFAULT ''",
            "modality VARCHAR DEFAULT ''",
            "cost_band VARCHAR DEFAULT ''",
            "evidence_quality VARCHAR DEFAULT ''",
        ):
            self._db.execute(
                f"ALTER TABLE validations ADD COLUMN IF NOT EXISTS {definition}"
            )
        self._db.execute(
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS cadence VARCHAR DEFAULT 'weekly'"
        )
        self._db.execute(
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS financial_period VARCHAR DEFAULT ''"
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
            o.portfolio_gain.monthly_expected
            for o in opportunities
            if not o.estimated_gain.is_strategic
        )
        realizable = sum(o.portfolio_gain.realizable_year for o in opportunities)

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
                o.portfolio_gain.monthly_expected,
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
            if account.cadence == "monthly" and account.financial_period:
                prior = [
                    row[0]
                    for row in self._db.execute(
                        """
                        SELECT scan_id FROM runs
                        WHERE account = ? AND cadence = 'monthly'
                          AND financial_period = ?
                        """,
                        [account.account_id, account.financial_period],
                    ).fetchall()
                ]
                for prior_scan in prior:
                    self._db.execute(
                        "DELETE FROM opportunity_snapshots WHERE scan_id = ? AND account = ?",
                        [prior_scan, account.account_id],
                    )
                    self._db.execute(
                        "DELETE FROM process_efficiency_snapshots WHERE scan_id = ? AND account = ?",
                        [prior_scan, account.account_id],
                    )
                    self._db.execute(
                        "DELETE FROM athena_recommendation_baselines WHERE scan_id = ? AND account = ?",
                        [prior_scan, account.account_id],
                    )
                self._db.execute(
                    """
                    DELETE FROM runs WHERE account = ? AND cadence = 'monthly'
                      AND financial_period = ?
                    """,
                    [account.account_id, account.financial_period],
                )
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
                INSERT INTO runs (
                    scan_id, account, scanned_on, source, opportunity_count,
                    identified_monthly, realizable_year, cadence, financial_period
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    scan_id,
                    account.account_id,
                    scanned_on,
                    source,
                    len(opportunities),
                    round(identified, 2),
                    round(realizable, 2),
                    account.cadence,
                    account.financial_period,
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
            self._record_efficiency(account, scan_id, scanned_on)
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def efficiency_regressions(
        self, account: Account, *, threshold: float = 0.20
    ) -> list[dict]:
        """Compara custo/consumo unitário atual com o último snapshot disponível."""
        current = {
            (service, process, metric): (value, unit)
            for service, process, metric, value, unit in _efficiency_rows(account)
        }
        rows = self._db.execute(
            """
            SELECT service, process_name, metric, value, unit
            FROM process_efficiency_snapshots
            WHERE account = ? AND scan_id = (
                SELECT scan_id FROM process_efficiency_snapshots
                WHERE account = ?
                ORDER BY recorded_on DESC, scan_id DESC LIMIT 1
            )
            """,
            [account.account_id, account.account_id],
        ).fetchall()
        out = []
        for service, process, metric, previous, unit in rows:
            item = current.get((service, process, metric))
            if item is None or float(previous) <= 0:
                continue
            value = item[0]
            change = (value - float(previous)) / float(previous)
            if change > threshold:
                out.append(
                    {
                        "service": service,
                        "process": process,
                        "metric": metric,
                        "previous": round(float(previous), 4),
                        "current": round(value, 4),
                        "change": round(change, 4),
                        "unit": unit,
                    }
                )
        return out

    def _record_efficiency(
        self, account: Account, scan_id: str, recorded_on: date
    ) -> None:
        rows = [
            [
                scan_id,
                account.account_id,
                service,
                process,
                metric,
                value,
                unit,
                recorded_on,
            ]
            for service, process, metric, value, unit in _efficiency_rows(account)
        ]
        self._db.execute(
            "DELETE FROM process_efficiency_snapshots WHERE scan_id = ? AND account = ?",
            [scan_id, account.account_id],
        )
        if rows:
            self._db.executemany(
                "INSERT INTO process_efficiency_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

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
        aliases = {
            alias: o
            for o in opportunities
            for alias in _fingerprint_aliases(o)
        }
        fingerprints = list(aliases)
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
            aliases[fingerprint].opportunity_id: bool(label)
            for fingerprint, label in by_fingerprint.items()
            if fingerprint in aliases
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
            _canonical_snapshot(dict(zip(columns, values, strict=True)))
            for values in cursor.fetchall()
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

    def record_signal_verdicts(self, decisions: list[SignalDecision]) -> None:
        """Guarda o julgamento da IA com a versão do prompt que o produziu.

        Sem a versão o número não significa nada: comparar precisão entre dois
        briefings diferentes seria comparar duas perguntas diferentes.
        """
        if not decisions:
            return
        self._db.executemany(
            """
            INSERT OR REPLACE INTO signal_verdicts
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                [
                    item.fingerprint,
                    item.account,
                    item.rule_id,
                    item.asset_type,
                    item.asset_name,
                    item.verdict,
                    item.rationale,
                    item.evidence_hash,
                    item.scan_id,
                    item.prompt_version,
                    _as_timestamp(item.decided_at),
                ]
                for item in decisions
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
            INSERT INTO validations (
                fingerprint, account, opportunity_id, rule_id, validated_at,
                predicted_monthly, realized_monthly, absolute_saving,
                baseline_cost, after_cost, baseline_volume, after_volume,
                baseline_cost_per_unit, after_cost_per_unit, normalized_saving,
                estimation_precision, realization_rate, performance_change_pct,
                failure_rate_change_pct, actor, notes,
                technical_predicted_monthly, calibrated_predicted_monthly,
                eligible_for_calibration, service, workload_type, modality,
                cost_band, evidence_quality
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
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
                result.technical_predicted_monthly,
                result.calibrated_predicted_monthly,
                result.eligible_for_calibration,
                result.service,
                result.workload_type,
                result.modality,
                result.cost_band,
                result.evidence_quality,
            ],
        )

    def calibration_for(
        self,
        rule_id: str,
        *,
        minimum_samples: int = 3,
        opportunity: Opportunity | None = None,
    ) -> CalibrationFactor | None:
        rows = self._db.execute(
            """
            SELECT technical_predicted_monthly, realized_monthly,
                   estimation_precision, service, workload_type, modality,
                   cost_band, evidence_quality
            FROM validations
            WHERE rule_id = ? AND eligible_for_calibration
              AND technical_predicted_monthly > 0
            """,
            [rule_id],
        ).fetchall()
        if opportunity is None:
            candidates = [("rule", rows)]
        else:
            service, workload, modality = _opportunity_segment(opportunity)
            band = _cost_band(opportunity.estimated_gain.monthly_expected)
            quality = opportunity.evidence_quality
            candidates = [
                (
                    "exact",
                    [
                        row for row in rows
                        if row[3:] == (service, workload, modality, band, quality)
                    ],
                ),
                (
                    "workload",
                    [
                        row for row in rows
                        if row[3] == service
                        and row[4] == workload
                        and row[5] == modality
                    ],
                ),
                ("service", [row for row in rows if row[3] == service]),
                ("rule", rows),
            ]
        selected_level = ""
        selected: list[tuple] = []
        for level, values in candidates:
            if len(values) >= minimum_samples:
                selected_level, selected = level, values
                break
        if not selected:
            return None
        ratios = [
            max(0.0, min(2.0, float(row[1]) / float(row[0])))
            for row in selected
        ]
        factor = statistics.median(ratios)
        low = _percentile(ratios, 0.25)
        high = _percentile(ratios, 0.75)
        errors = [
            abs(float(row[1]) - float(row[0])) / float(row[0])
            for row in selected
        ]
        median_error = statistics.median(errors)
        sample_count = len(selected)
        confidence = (
            "high"
            if sample_count >= 10 and median_error <= 0.25
            else "medium"
            if sample_count >= 5 and median_error <= 0.50
            else "low"
        )
        return CalibrationFactor(
            rule_id=rule_id,
            sample_count=sample_count,
            predicted_total=round(sum(float(row[0]) for row in selected), 2),
            realized_total=round(sum(float(row[1]) for row in selected), 2),
            factor=round(factor, 4),
            mean_precision=round(
                sum(float(row[2] or 0) for row in selected) / sample_count, 4
            ),
            factor_low=round(low, 4),
            factor_high=round(high, 4),
            median_error=round(median_error, 4),
            confidence=confidence,
            segment=(
                f"{service}/{workload}/{modality}/{band}/{quality}"
                if opportunity is not None else rule_id
            ),
            fallback_level=selected_level,
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

    def validation_window_status(
        self,
        fingerprint: str,
        financial_period: str,
        *,
        stabilization_days: int = 7,
    ) -> tuple[bool, str]:
        """Confirma que o mês inteiro começou após a estabilização da mudança."""
        try:
            period_start = date.fromisoformat(f"{financial_period}-01")
        except ValueError:
            return False, "período financeiro mensal ausente ou inválido"
        row = self._db.execute(
            """
            SELECT min(occurred_at) FROM lifecycle_events
            WHERE fingerprint = ? AND to_status = 'implemented'
            """,
            [fingerprint],
        ).fetchone()
        if row is None or row[0] is None:
            return False, "evento implemented não encontrado no histórico"
        implemented = row[0].date()
        stable_after = implemented + timedelta(days=stabilization_days)
        if period_start < stable_after:
            return (
                False,
                f"mês começou antes de {stable_after.isoformat()} "
                f"({stabilization_days} dias de estabilização)",
            )
        return True, "mês completo estável"

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
            "process_efficiency_snapshots",
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


def _percentile(values: list[float], position: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * position
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _opportunity_segment(opportunity: Opportunity) -> tuple[str, str, str]:
    asset = opportunity.asset_type
    service = (
        "glue" if asset.startswith("glue_")
        else "sagemaker" if asset.startswith("sagemaker_")
        else "stepfunctions" if asset == "state_machine"
        else "athena" if asset == "athena_query"
        else "s3" if asset.startswith("s3_")
        else asset
    )
    modality = {
        "glue_session": "interactive",
        "sagemaker_training_job": "training",
        "state_machine": "workflow",
        "athena_query": "query",
    }.get(asset, "default")
    return service, asset, modality


def _cost_band(value: float) -> str:
    if value < 100:
        return "lt_100"
    if value < 500:
        return "100_500"
    if value < 2000:
        return "500_2000"
    return "gte_2000"


def _as_timestamp(value: str) -> datetime:
    """ISO gravado pelo livro de vereditos, em datetime naive para o DuckDB."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return parsed.replace(tzinfo=None)


def _fingerprint_aliases(opportunity: Opportunity) -> tuple[str, ...]:
    current = opportunity.fingerprint()
    legacy = next(
        (
            old
            for old, canonical in _LEGACY_RULE_IDS.items()
            if canonical == opportunity.rule_id
        ),
        None,
    )
    if legacy is None:
        return (current,)
    return (
        current,
        _opportunity_fingerprint(
            opportunity.account,
            opportunity.asset_type,
            opportunity.asset_name,
            legacy,
        ),
    )


def _canonical_snapshot(row: dict) -> dict:
    canonical = _LEGACY_RULE_IDS.get(str(row.get("rule_id") or ""))
    if canonical is None:
        return row
    row["rule_id"] = canonical
    row["fingerprint"] = _opportunity_fingerprint(
        str(row.get("account") or ""),
        str(row.get("asset_type") or ""),
        str(row.get("asset_name") or ""),
        canonical,
    )
    return row


def _opportunity_fingerprint(
    account: str, asset_type: str, asset_name: str, rule_id: str
) -> str:
    raw = f"{account}|{asset_type}:{asset_name}|{rule_id}|default"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{raw}#{digest}"


def _efficiency_rows(account: Account) -> list[tuple[str, str, str, float, str]]:
    rows = []
    for job in account.glue_jobs:
        successes = max(
            0,
            int(getattr(job, "runs_in_window", 0) or 0)
            - int(getattr(job, "failed_runs_in_window", 0) or 0),
        )
        cost = getattr(job, "allocated_cost", None)
        if cost is not None and successes:
            rows.append(("glue", job.name, "cost_per_success", cost / successes, "USD"))
        total_cost = float(cost or 0)
        failed_cost = getattr(job, "failed_cost_window", None)
        if total_cost > 0 and failed_cost is not None:
            rows.append(
                (
                    "glue",
                    job.name,
                    "failure_cost_ratio",
                    failed_cost / total_cost,
                    "ratio",
                )
            )
    for query in account.athena_queries:
        runs = int(query.observed_runs or 0)
        if runs:
            rows.append(
                (
                    "athena",
                    query.structural_fingerprint or query.query_id,
                    "bytes_per_execution",
                    query.billed_bytes / runs,
                    "bytes",
                )
            )
            if query.allocated_cost is not None:
                rows.append(
                    (
                        "athena",
                        query.structural_fingerprint or query.query_id,
                        "cost_per_execution",
                        query.allocated_cost / runs,
                        "USD",
                    )
                )
    rows.append(
        ("account", account.account_id, "monthly_cost", account.billing_cost_mtd, "USD")
    )
    return rows
