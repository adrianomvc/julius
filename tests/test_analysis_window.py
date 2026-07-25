"""Contrato de janela: um período só, em UTC, para os dois serviços."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from julius.collection.collectors import cost_explorer
from julius.collection.collectors.athena import cost as athena_cost
from julius.collection.collectors.athena.telemetry import AthenaTelemetry
from julius.collection.collectors.glue import cost as glue_cost
from julius.collection.collectors.glue import jobs as glue_collector
from julius.collection.models import Account, AthenaCoverage, AthenaQuery
from julius.collection.normalizers.loader import UnsupportedDatasetVersionError, load_account
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import (
    DATASET_SCHEMA_VERSION,
    DEFAULT_CONFIG,
    GLUE_USAGE_TYPE_MARKERS,
    MIN_DAYS_FOR_FORECAST,
)
from julius.knowledge.rules.athena import queries as athena_detector

# 22h de Brasília no último dia de julho já é 1º de agosto em UTC. Era esse o
# horário em que a coleta se partia: o custo vinha do mês local e o consumo do
# mês UTC, e o rateio caía sem que a causa aparecesse.
MONTH_EDGE = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)


class _RecordingCostExplorer:
    """Registra o TimePeriod de cada chamada, sem devolver cobrança."""

    def __init__(self) -> None:
        self.periods: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.periods.append(kwargs["TimePeriod"])
        return {"ResultsByTime": []}


def test_window_covers_only_complete_utc_days():
    window = AnalysisWindow.trailing(days=30, now=datetime(2026, 7, 25, 14, 30, tzinfo=timezone.utc))

    assert window.end == datetime(2026, 7, 25, tzinfo=timezone.utc)
    assert window.start == datetime(2026, 6, 25, tzinfo=timezone.utc)
    assert window.data_through.isoformat() == "2026-07-24"
    assert len(window.day_keys) == 30
    # O dia corrente está parcial e nunca entra.
    assert not window.contains(datetime(2026, 7, 25, 10, tzinfo=timezone.utc))
    assert window.contains(datetime(2026, 7, 24, 23, 59, tzinfo=timezone.utc))


def test_window_is_the_same_regardless_of_local_time_of_day():
    """Dois scans no mesmo dia UTC enxergam exatamente a mesma janela."""
    morning = AnalysisWindow.trailing(now=datetime(2026, 7, 25, 3, tzinfo=timezone.utc))
    evening = AnalysisWindow.trailing(now=datetime(2026, 7, 25, 23, tzinfo=timezone.utc))

    assert morning == evening


def test_glue_and_athena_costs_cover_the_same_days():
    """A cobrança dos dois serviços é pedida para o mesmo intervalo."""
    window = AnalysisWindow.trailing(now=MONTH_EDGE)
    glue_client = _RecordingCostExplorer()
    athena_client = _RecordingCostExplorer()

    glue_cost.collect_glue_costs(
        glue_client, window=window, markers=GLUE_USAGE_TYPE_MARKERS
    )
    athena_cost.costs(
        athena_client,
        window.start,
        window.end,
        AthenaTelemetry(AthenaCoverage()),
    )

    assert glue_client.periods == athena_client.periods
    assert glue_client.periods[0] == {
        "Start": window.start_date.isoformat(),
        "End": window.end_date.isoformat(),
    }


def test_month_edge_scan_does_not_reset_measured_consumption():
    """Rodar 22h de Brasília no fim do mês não zera o consumo coletado.

    Antes, o corte de DPU virava o mês UTC seguinte enquanto o Cost Explorer
    ainda devolvia o mês anterior inteiro: nenhum job recebia rateio e a
    qualidade caía para `partial` com um gap genérico.
    """
    window = AnalysisWindow.trailing(now=MONTH_EDGE)
    ran_at = MONTH_EDGE - timedelta(days=2)

    class _Glue:
        def get_paginator(self, name):
            pages = {
                "get_jobs": [
                    {
                        "Jobs": [
                            {
                                "Name": "processa",
                                "GlueVersion": "4.0",
                                "WorkerType": "G.1X",
                                "NumberOfWorkers": 2,
                                "Command": {"Name": "glueetl"},
                                "DefaultArguments": {},
                            }
                        ]
                    }
                ],
                "get_job_runs": [
                    {
                        "JobRuns": [
                            {
                                "Id": "jr-1",
                                "JobRunState": "SUCCEEDED",
                                "ExecutionTime": 1800,
                                "DPUSeconds": 3600.0,
                                "StartedOn": ran_at,
                            }
                        ]
                    }
                ],
            }[name]
            return type("P", (), {"paginate": lambda self, **kw: pages})()

    job = glue_collector.collect_jobs(_Glue(), window=window)[0]

    assert job.runs_in_window == 1
    assert job.actual_dpu_hours_window == 1.0
    assert job.window_end == window.data_through.isoformat()


def test_consumption_is_reported_as_measured_not_projected():
    window = AnalysisWindow.trailing(days=30, now=MONTH_EDGE)
    account = load_account("data/sample/consumer-avi.json")

    for job in account.glue_jobs:
        # A janela nunca é multiplicada por quanto falta para o mês fechar.
        assert job.window_dpu_hours == pytest.approx(
            job.total_dpu_hours_window
        ) or job.total_dpu_hours_window == 0
    assert account.window_days == window.days


def test_monthly_conversion_is_explicit_and_not_thirty_days():
    """30 dias não são um mês: a conversão é nomeada e vale 30,44/30."""
    account = load_account("data/sample/consumer-avi.json")
    job = next(job for job in account.glue_jobs if job.runs_in_window)

    assert job.monthly_factor == pytest.approx(365.25 / 12 / 30)
    assert job.runs_per_month == pytest.approx(
        round(job.runs_in_window * job.monthly_factor, 1)
    )


def test_forecast_is_withheld_until_enough_days_are_closed():
    """Com poucos dias fechados a projeção do mês não é exibida."""

    class _CostExplorer:
        def __init__(self) -> None:
            self.forecast_calls = 0

        def get_cost_and_usage(self, **kwargs):
            return {
                "ResultsByTime": [
                    {
                        "Estimated": True,
                        "Groups": [
                            {
                                "Keys": ["AWS Glue"],
                                "Metrics": {
                                    "UnblendedCost": {"Amount": "10.0", "Unit": "USD"}
                                },
                            }
                        ],
                    }
                ]
            }

        def get_cost_forecast(self, **kwargs):
            self.forecast_calls += 1
            return {"Total": {"Amount": "300.0", "Unit": "USD"}}

    early = _CostExplorer()
    services = cost_explorer.collect_services(
        early,
        billing=BillingMonth.current(now=datetime(2026, 7, 3, tzinfo=timezone.utc)),
        include_forecast=True,
    )
    assert early.forecast_calls == 0
    assert services[0].forecast_cost_eom is None

    late = _CostExplorer()
    cost_explorer.collect_services(
        late,
        billing=BillingMonth.current(now=datetime(2026, 7, 20, tzinfo=timezone.utc)),
        include_forecast=True,
    )
    assert late.forecast_calls == 1
    assert BillingMonth.current(
        now=datetime(2026, 7, 3, tzinfo=timezone.utc)
    ).forecast_factor(minimum_days=MIN_DAYS_FOR_FORECAST) is None


def test_dataset_from_the_previous_schema_is_refused_not_reinterpreted(tmp_path):
    """Um dataset v1 mede mês-corrente e não vira janela por renomeação."""
    dataset = tmp_path / "account.json"
    dataset.write_text(
        json.dumps({"account": "123456789012", "glue_jobs": []}),
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedDatasetVersionError) as excinfo:
        load_account(dataset)

    assert excinfo.value.found == 1
    assert "julius collect" in str(excinfo.value)


def test_current_schema_round_trips_with_its_window(tmp_path):
    from julius.collection.normalizers.dump import account_to_dataset

    account = Account(
        account_id="123456789012",
        window_start="2026-06-25",
        window_end="2026-07-24",
        window_days=30,
    )
    dataset = tmp_path / "account.json"
    dataset.write_text(json.dumps(account_to_dataset(account)), encoding="utf-8")

    loaded = load_account(dataset)

    assert loaded.window_start == "2026-06-25"
    assert loaded.window_end == "2026-07-24"
    assert loaded.window_days == 30
    assert json.loads(dataset.read_text(encoding="utf-8"))[
        "dataset_schema_version"
    ] == DATASET_SCHEMA_VERSION


def test_provisioned_capacity_queries_are_reported_instead_of_dropped():
    """Query fora de on-demand não é analisada — e isso precisa aparecer."""
    account = Account(
        account_id="123456789012",
        athena_coverage=AthenaCoverage(),
        athena_queries=[
            AthenaQuery(
                query_id="reservada",
                modality="provisioned",
                billed_bytes=10 * 1024**4,
                observed_runs=40,
                coverage_days=30,
                full_scan_confirmed=True,
            )
        ],
    )

    found = athena_detector.detect(account, DEFAULT_CONFIG, "scan")

    assert found == []
    assert any(
        "provisioned" in gap and "não analisados" in gap
        for gap in account.athena_coverage.gaps
    ), account.athena_coverage.gaps
