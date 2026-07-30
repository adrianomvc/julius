"""Testes do MVP 1B: persistência, portfolio multi-conta e KPIs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from julius.pipeline import analyze
from julius.portfolio import analyze_portfolio, discover_inputs
from julius.reporting import compute_kpis
from julius.state import BacklogStore, HistoryStore

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "sample"
AVI = SAMPLE_DIR / "consumer-avi.json"


def test_store_preserves_first_seen(tmp_path):
    store = BacklogStore(tmp_path / "backlog.json")
    a1 = analyze(AVI, store=store, today=date(2026, 5, 1))
    assert all(o.first_seen == "2026-05-01" for o in a1.opportunities)

    a2 = analyze(AVI, store=store, today=date(2026, 7, 1))
    # first_seen preservado; last_seen avança.
    for o in a2.opportunities:
        assert o.first_seen == "2026-05-01"
        assert o.last_seen == "2026-07-01"
    assert (tmp_path / "backlog.json").exists()


def test_portfolio_aggregates(tmp_path):
    inputs = discover_inputs(SAMPLE_DIR)
    assert len(inputs) >= 3
    p = analyze_portfolio(inputs, store=BacklogStore(tmp_path / "backlog.json"))
    assert len(p.rollups) == len(inputs)
    assert p.total_identified_monthly > 0
    # Ordenado por economia identificada (maior primeiro).
    ids = [r.identified_monthly for r in p.rollups]
    assert ids == sorted(ids, reverse=True)


def test_kpis(tmp_path):
    a = analyze(AVI)
    kpis = compute_kpis(a.account, a.opportunities)
    assert 0.0 < kpis.actionability_rate <= 1.0
    assert 0.0 <= kpis.coverage_overall <= 1.0
    assert kpis.precision_at_10 is None  # sem rótulos

    # Com rótulos: Precision@10 calculado sobre as top oportunidades.
    labels = {o.opportunity_id: True for o in a.opportunities[:3]}
    labels.update({o.opportunity_id: False for o in a.opportunities[3:5]})
    kpis2 = compute_kpis(a.account, a.opportunities, labels)
    assert kpis2.precision_at_10 is not None
    assert 0.0 <= kpis2.precision_at_10 <= 1.0
    assert kpis2.reviewed_at_10 == 5
    assert kpis2.false_positives_at_10 == 2
    assert kpis2.false_positive_rate_at_10 == 0.4


def test_opportunity_ids_are_stable():
    first = analyze(AVI)
    second = analyze(AVI)
    first_ids = {(o.rule_id, o.asset_name): o.opportunity_id for o in first.opportunities}
    second_ids = {(o.rule_id, o.asset_name): o.opportunity_id for o in second.opportunities}
    assert first_ids == second_ids


def test_duckdb_history_reviews_and_parquet(tmp_path):
    db_path = tmp_path / "julius.duckdb"
    parquet_dir = tmp_path / "parquet"
    backlog = BacklogStore(tmp_path / "backlog.json")

    with HistoryStore(db_path) as history:
        analysis = analyze(
            AVI,
            store=backlog,
            history=history,
            today=date(2026, 7, 23),
        )
        history.record_review(
            analysis.opportunities[0],
            is_true_positive=True,
            reviewer="especialista-a",
        )
        history.record_review(
            analysis.opportunities[1],
            is_true_positive=False,
            reviewer="especialista-a",
            notes="uso sazonal conhecido",
        )

        labels = history.labels_for(analysis.opportunities)
        reviewed_kpis = compute_kpis(analysis.account, analysis.opportunities, labels)
        assert reviewed_kpis.precision_at_10 == 0.5
        assert reviewed_kpis.false_positive_rate_at_10 == 0.5

        summary = history.review_summary(analysis.opportunities)
        assert summary.reviewed == 2
        assert summary.false_positives == 1
        written = history.export_parquet(parquet_dir)

    assert db_path.exists()
    assert {path.name for path in written} == {
        "diff_events.parquet",
        "lifecycle_events.parquet",
        "runs.parquet",
        "opportunity_snapshots.parquet",
        "process_efficiency_snapshots.parquet",
        "reviews.parquet",
        "validations.parquet",
    }
    assert all(path.exists() and path.stat().st_size > 0 for path in written)


def test_portfolio_records_three_accounts_in_history(tmp_path):
    inputs = discover_inputs(SAMPLE_DIR)
    with HistoryStore(tmp_path / "portfolio.duckdb") as history:
        portfolio = analyze_portfolio(
            inputs,
            store=BacklogStore(tmp_path / "backlog.json"),
            history=history,
        )
        run_count = history.run_count()

    assert len(portfolio.analyses) >= 3
    assert run_count == len(inputs)
