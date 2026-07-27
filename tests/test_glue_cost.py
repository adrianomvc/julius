"""Custo Glue real por usage type: classificação, rateio, gates e moeda."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from julius.collection.collectors.glue import cost as glue_cost
from julius.collection.currency import usd_amount
from julius.collection.models import (
    Account,
    DataBrewJob,
    GlueCrawler,
    GlueJob,
    InteractiveSession,
    ServiceCost,
)
from julius.collection.normalizers.loader import load_account
from julius.collection.window import AnalysisWindow
from julius.config import (
    ALLOCATED_GLUE_BUCKETS,
    DATASET_SCHEMA_VERSION,
    DEFAULT_CONFIG,
    GLUE_USAGE_TYPE_MARKERS,
)
from julius.knowledge.rules.glue import estimation as glue_est
from julius.pipeline import analyze_account
from julius.reporting import renderer
from julius.scoring.process_cost import build_process_costs

TODAY = date(2026, 7, 25)
WINDOW = AnalysisWindow.trailing(
    now=datetime(2026, 7, 26, tzinfo=timezone.utc)
)


class FakeCE:
    """Cost Explorer mínimo: responde a métrica pedida ou levanta erro."""

    def __init__(self, groups, *, metric="NetUnblendedCost", fail_metrics=()):
        self.groups = groups
        self.metric = metric
        self.fail_metrics = set(fail_metrics)
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        requested = kwargs["Metrics"][0]
        if requested in self.fail_metrics:
            raise RuntimeError("indisponível")
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": [usage],
                            "Metrics": {requested: {"Amount": str(amount), "Unit": unit}},
                        }
                        for usage, amount, unit in self.groups
                    ]
                }
            ]
        }


def _hours(dpu_hours: float) -> float:
    return dpu_hours * 3600.0


def _account(**kwargs) -> Account:
    account = Account(account_id="123456789012", generated_at=TODAY.isoformat())
    account.glue_jobs = kwargs.get("jobs", [])
    account.glue_crawlers = kwargs.get("crawlers", [])
    account.interactive_sessions = kwargs.get("sessions", [])
    account.databrew_jobs = kwargs.get("databrew", [])
    return account


def _job(name: str, dpu_hours: float, **kwargs) -> GlueJob:
    return GlueJob(
        name=name,
        dpu_seconds_window=_hours(dpu_hours),
        window_end=TODAY.isoformat(),
        worker_type="G.1X",
        number_of_workers=10,
        **kwargs,
    )


@pytest.mark.parametrize(
    "usage_type,bucket",
    [
        ("SAE1-Glue-ETL-DPU-Hour", "etl_job"),
        ("SAE1-Glue-Flex-DPU-Hour", "flex"),
        ("SAE1-Crawler-DPU-Hour", "crawler"),
        ("SAE1-GlueInteractiveSession-DPU-Hour", "interactive_session"),
        ("SAE1-DataBrew-Node-Hour", "databrew"),
        ("SAE1-Request", "catalog"),
        ("SAE1-DataQuality-DPU-Hour", "data_quality"),
        ("SAE1-Algo-Que-Nao-Conhecemos", "other"),
    ],
)
def test_usage_types_are_classified_by_versioned_markers(usage_type, bucket):
    assert glue_cost.classify_usage_type(usage_type, GLUE_USAGE_TYPE_MARKERS) == bucket


def test_collection_keeps_unknown_usage_types_visible():
    ce = FakeCE(
        [
            ("SAE1-Glue-ETL-DPU-Hour", 440, "USD"),
            ("SAE1-Glue-TabelaNova", 7, "USD"),
        ]
    )
    coverage = glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS)

    assert coverage.buckets == {"etl_job": 440.0, "other": 7.0}
    # Usage type desconhecido nunca é descartado em silêncio.
    assert coverage.unknown_usage_types == ["SAE1-Glue-TabelaNova"]
    assert coverage.net_cost == 447.0
    assert coverage.cost_metric == "NetUnblendedCost"
    assert ce.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
    assert ce.calls[0]["Filter"] == {
        "Dimensions": {"Key": "SERVICE", "Values": ["AWS Glue"]}
    }


def test_collection_falls_back_to_unblended_and_records_the_metric():
    ce = FakeCE(
        [("SAE1-Glue-ETL-DPU-Hour", 100, "USD")],
        fail_metrics={"NetUnblendedCost"},
    )
    coverage = glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS)

    assert coverage.cost_metric == "UnblendedCost"
    assert any("NetUnblendedCost" in gap for gap in coverage.gaps)


def test_allocation_splits_each_bucket_by_measured_consumption():
    account = _account(
        jobs=[
            _job("pesado", 750),
            _job("leve", 250),
            _job("flexivel", 100, execution_class="FLEX"),
        ],
        crawlers=[
            GlueCrawler(name="c1", dpu_hours_window=3.0),
            GlueCrawler(name="c2", dpu_hours_window=1.0),
        ],
        sessions=[InteractiveSession(session_id="s1", dpu_seconds_window=_hours(20))],
        databrew=[DataBrewJob(name="d1", estimated_node_hours_window=10.0)],
    )
    ce = FakeCE(
        [
            ("SAE1-Glue-ETL-DPU-Hour", 440, "USD"),
            ("SAE1-Glue-Flex-DPU-Hour", 29, "USD"),
            ("SAE1-Crawler-DPU-Hour", 8, "USD"),
            ("SAE1-GlueInteractiveSession-DPU-Hour", 12, "USD"),
            ("SAE1-DataBrew-Node-Hour", 4.8, "USD"),
        ]
    )
    coverage = glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        allocatable_buckets=ALLOCATED_GLUE_BUCKETS,
    )

    by_name = {job.name: job.allocated_cost for job in account.glue_jobs}
    assert by_name["pesado"] == pytest.approx(330.0)
    assert by_name["leve"] == pytest.approx(110.0)
    # FLEX é rateado no próprio bucket, não junto do STANDARD.
    assert by_name["flexivel"] == pytest.approx(29.0)
    assert account.glue_crawlers[0].allocated_cost == pytest.approx(6.0)
    assert account.glue_crawlers[1].allocated_cost == pytest.approx(2.0)
    assert account.interactive_sessions[0].allocated_cost == pytest.approx(12.0)
    assert account.databrew_jobs[0].allocated_cost == pytest.approx(4.8)
    assert coverage.cost_quality == "reconciled"
    assert coverage.modeled_ratio == pytest.approx(1.0)


def test_quality_is_partial_when_runs_did_not_report_dpu_seconds():
    job = _job("estimado", 500)
    job.estimated_dpu_hours_window = 500.0  # metade da DPU-hora é duração estimada
    account = _account(jobs=[job])
    ce = FakeCE([("SAE1-Glue-ETL-DPU-Hour", 440, "USD")])

    coverage = glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        allocatable_buckets=ALLOCATED_GLUE_BUCKETS,
    )

    assert coverage.cost_quality == "partial"
    assert job.cost_quality == "partial"
    # O rateio continua acontecendo; só a qualidade é rebaixada.
    assert job.allocated_cost == pytest.approx(440.0)
    assert any("DPUSeconds" in gap for gap in coverage.gaps)


def test_quality_is_partial_when_the_job_inventory_is_incomplete():
    account = _account(jobs=[_job("unico", 1000)])
    ce = FakeCE([("SAE1-Glue-ETL-DPU-Hour", 440, "USD")])

    coverage = glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        jobs_collection_complete=False,
    )

    assert coverage.cost_quality == "partial"
    assert any("inventário de jobs incompleto" in gap for gap in coverage.gaps)


def test_quality_is_partial_when_consumption_diverges_from_the_billing():
    # 1000 DPU-h × 0,44 = 440 modelado contra 900 cobrados: fora da banda.
    account = _account(jobs=[_job("divergente", 1000)])
    ce = FakeCE([("SAE1-Glue-ETL-DPU-Hour", 900, "USD")])

    coverage = glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        allocatable_buckets=ALLOCATED_GLUE_BUCKETS,
    )

    assert coverage.cost_quality == "partial"
    assert coverage.modeled_ratio == pytest.approx(0.4889, abs=1e-4)
    assert any("divergem além da banda" in gap for gap in coverage.gaps)


def test_bucket_without_collected_consumption_is_reported_not_distributed():
    account = _account(jobs=[_job("etl", 1000)])
    ce = FakeCE(
        [
            ("SAE1-Glue-ETL-DPU-Hour", 440, "USD"),
            ("SAE1-Crawler-DPU-Hour", 15, "USD"),
        ]
    )

    coverage = glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        allocatable_buckets=ALLOCATED_GLUE_BUCKETS,
    )

    assert any("bucket crawler cobrado sem consumo" in gap for gap in coverage.gaps)


def test_catalog_and_unknown_costs_stay_unattributed():
    ce = FakeCE(
        [
            ("SAE1-Glue-ETL-DPU-Hour", 440, "USD"),
            ("SAE1-Request", 12, "USD"),
            ("SAE1-DataQuality-DPU-Hour", 8, "USD"),
            ("SAE1-Misterio", 5, "USD"),
        ]
    )
    account = _account(jobs=[_job("etl", 1000)])

    coverage = glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        allocatable_buckets=ALLOCATED_GLUE_BUCKETS,
    )

    # Catálogo, data quality e o usage type desconhecido não têm ativo a que
    # ratear; a cobrança de ETL foi absorvida pelo job.
    assert coverage.allocated_buckets == ["etl_job"]
    assert coverage.unattributed_cost == pytest.approx(25.0)


def test_nothing_is_attributed_before_allocation_runs():
    """A cobrança só deixa de ser 'não atribuída' quando alguém a atribui."""
    ce = FakeCE([("SAE1-Glue-ETL-DPU-Hour", 440, "USD")])

    coverage = glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS)

    assert coverage.allocated_buckets == []
    assert coverage.unattributed_cost == pytest.approx(440.0)


def test_billing_outside_usd_becomes_a_gap_instead_of_a_converted_number():
    """A AWS reporta custo em USD; outra moeda é anomalia, não conversão."""
    ce = FakeCE([("SAE1-Glue-ETL-DPU-Hour", 543, "BRL")])
    coverage = glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS)

    assert coverage.buckets == {}
    assert coverage.net_cost is None
    assert coverage.cost_quality == "unavailable"
    assert any("BRL" in gap and "não converte" in gap for gap in coverage.gaps)


def test_zero_cost_group_is_accepted_whatever_the_reported_unit():
    ce = FakeCE(
        [
            ("SAE1-Glue-ETL-DPU-Hour", 440, "USD"),
            ("SAE1-Glue-Sem-Cobranca", 0, "N/A"),
        ]
    )
    coverage = glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS)

    # Zero não tem moeda: uma unidade estranha em grupo zerado não bloqueia.
    assert coverage.buckets["etl_job"] == pytest.approx(440.0)
    assert coverage.cost_quality == "partial"
    assert usd_amount(0, "BRL") == 0.0
    assert usd_amount(10, "BRL") is None


def test_estimation_prefers_the_allocated_cost_over_the_table_rate():
    job = _job("caro", 1000)
    modeled = glue_est.bookmark_saving(job, DEFAULT_CONFIG)
    assert modeled.baseline_quality == "modeled"

    # Cobrança real 20% acima da tarifa de tabela para as mesmas DPU-horas.
    job.allocated_cost = 528.0
    job.cost_quality = "reconciled"
    allocated = glue_est.bookmark_saving(job, DEFAULT_CONFIG)

    assert allocated.baseline_quality == "allocated"
    assert allocated.baseline_cost == pytest.approx(modeled.baseline_cost * 1.2)
    assert any("custo alocado do Cost Explorer" in item for item in allocated.assumptions)


def test_partial_allocation_is_labeled_as_such_in_the_estimation():
    job = _job("parcial", 1000)
    job.allocated_cost = 440.0
    job.cost_quality = "partial"

    assert (
        glue_est.autoscaling_saving(job, DEFAULT_CONFIG).baseline_quality
        == "allocated_partial"
    )


def test_process_costs_sum_to_the_allocated_billing():
    account = _account(jobs=[_job("a", 750), _job("b", 250)])
    ce = FakeCE([("SAE1-Glue-ETL-DPU-Hour", 440, "USD")])
    glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        allocatable_buckets=ALLOCATED_GLUE_BUCKETS,
    )

    rows = build_process_costs(account, DEFAULT_CONFIG, today=TODAY)

    # O custo por processo herda a cobrança rateada, sem recalcular por tarifa.
    assert sum(row.total_cost_window for row in rows) == pytest.approx(440.0, abs=0.01)


def test_report_shows_the_glue_cost_quality_and_the_unattributed_buckets():
    account = _account(jobs=[_job("etl", 1000)])
    ce = FakeCE(
        [
            ("SAE1-Glue-ETL-DPU-Hour", 440, "USD"),
            ("SAE1-Request", 20, "USD"),
        ]
    )
    account.glue_cost_coverage = glue_cost.allocate_costs(
        account,
        glue_cost.collect_glue_costs(ce, window=WINDOW, markers=GLUE_USAGE_TYPE_MARKERS),
        DEFAULT_CONFIG,
        allocatable_buckets=ALLOCATED_GLUE_BUCKETS,
    )
    account.services = [
        ServiceCost(
            name="AWS Glue",
            monthly_cost=460.0,
            data_through=TODAY.isoformat(),
        )
    ]

    analysis = analyze_account(account, DEFAULT_CONFIG, today=TODAY)
    html = renderer.render_html(analysis.vm)

    # A atribuição por usage type é material de auditoria: saiu do HTML com o
    # apêndice técnico e vive completa no JSON.
    registro = json.loads(renderer.render_json(analysis.vm, analysis.opportunities))
    assert "etl_job" in str(registro["glue_cost"])
    assert "catalog" in str(registro["glue_cost"])
    assert analysis.vm.glue_cost["cost_quality"] == "reconciled"
    assert analysis.vm.glue_cost["unattributed_fmt"] == "US$ 20"
    # Nenhum valor fora da moeda canônica chega ao relatório.
    assert "R$" not in html


def test_legacy_dataset_without_currency_is_refused_not_relabeled(tmp_path):
    dataset = tmp_path / "legado.json"
    dataset.write_text(
        json.dumps(
            {
                "dataset_schema_version": DATASET_SCHEMA_VERSION,
                "account": "123456789012",
                "cost_explorer": {
                    "services": [{"name": "AWS Glue", "monthly_cost": 543.0}]
                },
                "governance": {
                    "previous_results": [
                        {
                            "title": "antigo",
                            "predicted_monthly": 108.6,
                            "realized_monthly": 54.3,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    account = load_account(dataset)

    # Sem moeda declarada o registro é anterior ao contrato USD: recusado em
    # vez de reinterpretado como dólar.
    assert account.currency == "USD"
    assert account.services == []
    assert account.previous_results == []


def test_dataset_declaring_usd_is_loaded_as_is(tmp_path):
    dataset = tmp_path / "atual.json"
    dataset.write_text(
        json.dumps(
            {
                "dataset_schema_version": DATASET_SCHEMA_VERSION,
                "account": "123456789012",
                "cost_explorer": {
                    "services": [
                        {
                            "name": "AWS Glue",
                            "monthly_cost": 3292.31,
                            "currency": "USD",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    account = load_account(dataset)

    assert account.services[0].monthly_cost == 3292.31
    assert account.services[0].currency == "USD"
