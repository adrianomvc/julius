from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import Stubber

from julius.collection.collectors import cloudwatch as cloudwatch_collector
from julius.collection.collectors.glue import crawlers as crawlers_collector
from julius.collection.collectors.glue import databrew as databrew_collector
from julius.collection.collectors.glue import jobs as glue_collector
from julius.collection.collectors.glue import sessions as sessions_collector
from julius.collection.collectors.glue import spark_logs as spark_event_logs_collector
from julius.collection.collectors.glue import triggers as glue_triggers_collector
from julius.collection.models import (
    Account,
    ActorEvent,
    GlueJob,
    ProcessCost,
    Schedule,
    StateMachine,
)
from julius.collection.schedule_frequency import expected_runs_per_month
from julius.collection.window import AnalysisWindow
from julius.config import DEFAULT_CONFIG
from julius.findings.opportunity import Estimation, Opportunity
from julius.graph.ownership import resolve_owner
from julius.knowledge.rules.glue import jobs as glue_detector
from julius.scoring import priority as prioritizer
from julius.scoring.process_cost import (
    apply_conservative_caps,
    build_process_costs,
)


def _glue_client():
    return boto3.client(
        "glue",
        region_name="sa-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_job_cost_separates_reported_and_estimated_dpu_hours():
    glue = _glue_client()
    stub = Stubber(glue)
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    # `now` e o corte da janela; execucoes precisam cair em dia fechado.
    ran_at = now - timedelta(days=1)
    stub.add_response(
        "get_jobs",
        {
            "Jobs": [
                {
                    "Name": "processa",
                    "GlueVersion": "5.1",
                    "JobMode": "VISUAL",
                    "WorkerType": "G.1X",
                    "NumberOfWorkers": 2,
                    "DefaultArguments": {},
                    "Command": {"Name": "glueetl", "ScriptLocation": "s3://scripts/a.py"},
                }
            ]
        },
    )
    stub.add_response(
        "get_job_runs",
        {
            "JobRuns": [
                {
                    "Id": "jr-reported",
                    "JobRunState": "SUCCEEDED",
                    "ExecutionTime": 1800,
                    "DPUSeconds": 3600.0,
                    "StartedOn": ran_at,
                },
                {
                    "Id": "jr-estimated",
                    "JobRunState": "SUCCEEDED",
                    "ExecutionTime": 1800,
                    "WorkerType": "G.1X",
                    "NumberOfWorkers": 2,
                    "StartedOn": ran_at,
                },
            ]
        },
    )
    with stub:
        job = glue_collector.collect_jobs(glue, window=AnalysisWindow.trailing(now=now))[0]

    assert job.job_mode == "VISUAL"
    assert job.actual_dpu_hours_window == 1.0
    assert job.estimated_dpu_hours_window == 1.0
    assert job.run_ids_in_window == ["jr-estimated", "jr-reported"]


def test_streaming_run_started_before_window_is_still_measured():
    glue = _glue_client()
    stub = Stubber(glue)
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    stub.add_response(
        "get_jobs",
        {
            "Jobs": [
                {
                    "Name": "stream",
                    "GlueVersion": "5.1",
                    "WorkerType": "G.1X",
                    "NumberOfWorkers": 2,
                    "DefaultArguments": {},
                    "Command": {
                        "Name": "gluestreaming",
                        "ScriptLocation": "s3://scripts/stream.py",
                    },
                }
            ]
        },
    )
    stub.add_response(
        "get_job_runs",
        {
            "JobRuns": [
                {
                    "Id": "jr-stream",
                    "JobRunState": "RUNNING",
                    "ExecutionTime": 40 * 86400,
                    "StartedOn": now - timedelta(days=40),
                    "WorkerType": "G.1X",
                    "NumberOfWorkers": 2,
                }
            ]
        },
    )

    with stub:
        job = glue_collector.collect_jobs(
            glue, window=AnalysisWindow.trailing(now=now)
        )[0]

    assert job.runs_in_window == 1
    assert job.active_seconds_window == pytest.approx(job.window_days * 86400)
    assert job.estimated_dpu_hours_window == pytest.approx(
        job.window_days * 24 * 2
    )


def test_collects_overlap_concurrency_and_retry_signals():
    glue = _glue_client()
    stub = Stubber(glue)
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    start = now - timedelta(days=1)
    stub.add_response(
        "get_jobs",
        {
            "Jobs": [
                {
                    "Name": "overlap",
                    "GlueVersion": "5.1",
                    "WorkerType": "G.1X",
                    "NumberOfWorkers": 2,
                    "ExecutionProperty": {"MaxConcurrentRuns": 2},
                    "JobRunQueuingEnabled": True,
                    "DefaultArguments": {},
                    "Command": {
                        "Name": "glueetl",
                        "ScriptLocation": "s3://scripts/overlap.py",
                    },
                }
            ]
        },
    )
    stub.add_response(
        "get_job_runs",
        {
            "JobRuns": [
                {
                    "Id": "jr-b",
                    "PreviousRunId": "jr-a",
                    "JobRunState": "SUCCEEDED",
                    "ExecutionTime": 3600,
                    "DPUSeconds": 7200.0,
                    "StartedOn": start + timedelta(minutes=30),
                    "CompletedOn": start + timedelta(minutes=90),
                },
                {
                    "Id": "jr-a",
                    "JobRunState": "SUCCEEDED",
                    "ExecutionTime": 3600,
                    "DPUSeconds": 7200.0,
                    "StartedOn": start,
                    "CompletedOn": start + timedelta(minutes=60),
                },
            ]
        },
    )

    with stub:
        job = glue_collector.collect_jobs(
            glue, window=AnalysisWindow.trailing(now=now)
        )[0]

    assert job.max_concurrent_runs == 2
    assert job.job_run_queuing_enabled is True
    assert job.overlapping_runs_in_window == 2
    assert job.overlap_seconds_window == 1800
    assert job.retry_runs_in_window == 1


def test_window_consumption_is_measured_and_never_extrapolated():
    """A janela é reportada como medida; o mês vem de um fator explícito.

    Antes, 10 DPU-hora observadas até o dia 10 viravam 31 pela projeção para o
    fim do mês — o resultado dependia do dia em que o scan rodasse.
    """
    job = GlueJob(
        name="processa",
        dpu_seconds_window=36000,
        window_end="2026-07-10",
        window_days=30,
    )

    assert DEFAULT_CONFIG.pricing.currency == "USD"
    assert job.total_dpu_hours_window == 10.0
    assert job.window_dpu_hours == 10.0
    assert job.monthly_dpu_hours == pytest.approx(10.0 * 365.25 / 12 / 30)


def test_detects_overlapping_runs_as_blocked_investigation():
    job = GlueJob(
        name="overlap",
        glue_version="5.1",
        command_type="glueetl",
        runs_in_window=2,
        observed_runs=2,
        coverage_days=30,
        overlapping_runs_in_window=2,
        overlap_seconds_window=1800,
        max_concurrent_runs=2,
    )

    found = glue_detector.detect(
        Account(account_id="123", glue_jobs=[job]),
        DEFAULT_CONFIG,
        "scan-overlap",
    )
    opportunity = next(
        item for item in found if item.rule_id == "GLUE-OVERLAPPING-RUNS"
    )

    assert opportunity.blocked is True
    assert opportunity.estimated_gain.monthly_expected == 0
    assert any("30.0 min" in item for item in opportunity.evidence)


def test_detects_streaming_cost_without_input_but_does_not_assume_saving():
    job = GlueJob(
        name="stream",
        glue_version="5.1",
        command_type="gluestreaming",
        worker_type="G.1X",
        number_of_workers=2,
        runs_in_window=1,
        observed_runs=1,
        coverage_days=30,
        active_seconds_window=7200,
        estimated_dpu_hours_window=4,
        streaming_records_window=0,
    )

    found = glue_detector.detect(
        Account(account_id="123", glue_jobs=[job]),
        DEFAULT_CONFIG,
        "scan-stream",
    )
    opportunity = next(
        item for item in found if item.rule_id == "GLUE-STREAMING-NO-INPUT"
    )

    assert opportunity.blocked is True
    assert opportunity.estimation is not None
    assert opportunity.estimation.baseline_cost > 0
    assert opportunity.estimation.estimated_saving == 0


def test_detects_dpu_consumption_with_explicit_zero_input_metric():
    job = GlueJob(
        name="empty-input",
        glue_version="5.1",
        command_type="glueetl",
        worker_type="G.1X",
        number_of_workers=2,
        runs_in_window=1,
        observed_runs=1,
        coverage_days=30,
        dpu_seconds_window=3600,
        bytes_read_window=0,
    )

    rules = {
        item.rule_id
        for item in glue_detector.detect(
            Account(account_id="123", glue_jobs=[job]),
            DEFAULT_CONFIG,
            "scan-empty",
        )
    }

    assert "GLUE-NO-INPUT-WASTE" in rules


def test_flex_requires_supported_spark_batch_job():
    eligible = GlueJob(
        name="batch",
        glue_version="4.0",
        command_type="glueetl",
        execution_class="STANDARD",
        time_sensitive=False,
        runs_in_window=10,
        avg_execution_sec=600,
        worker_type="G.1X",
        number_of_workers=2,
        owner_tag="dados",
    )
    legacy = GlueJob(
        name="legacy",
        glue_version="1.0",
        command_type="glueetl",
        execution_class="STANDARD",
        time_sensitive=False,
        runs_in_window=10,
        avg_execution_sec=600,
        worker_type="G.1X",
        number_of_workers=2,
        owner_tag="dados",
    )
    rules = {
        (item.asset_name, item.rule_id)
        for item in glue_detector.detect(
            Account(account_id="123", glue_jobs=[eligible, legacy]),
            DEFAULT_CONFIG,
            "scan",
        )
    }

    assert ("batch", "GLUE-FLEX-CANDIDATE") in rules
    assert ("legacy", "GLUE-FLEX-CANDIDATE") not in rules


def test_latest_human_update_becomes_inferred_owner():
    account = Account(
        account_id="123",
        glue_jobs=[GlueJob(name="processa")],
        actor_events=[
            ActorEvent(
                resource_type="glue_job",
                resource_name="processa",
                event_name="CreateJob",
                event_time="2026-01-01T10:00:00Z",
                source_identity="criador",
                is_human=True,
            ),
            ActorEvent(
                resource_type="glue_job",
                resource_name="processa",
                event_name="UpdateJob",
                event_time="2026-07-20T10:00:00Z",
                source_identity="maria",
                is_human=True,
            ),
            ActorEvent(
                resource_type="glue_job",
                resource_name="processa",
                event_name="StartJobRun",
                event_time="2026-07-21T10:00:00Z",
                source_identity="operador",
                is_human=True,
            ),
        ],
    )

    owner = resolve_owner(account, "glue_job", "processa")
    assert owner.owner == "maria"
    assert "última alteração humana" in owner.source


def test_process_cost_allocation_does_not_duplicate_shared_job():
    job = GlueJob(
        name="compartilhado",
        worker_type="G.1X",
        number_of_workers=2,
        dpu_seconds_window=7200,
        window_end="2026-07-24",
    )
    account = Account(
        account_id="123",
        generated_at="2026-07-24",
        glue_jobs=[job],
        schedules=[
            Schedule(name="p1", target_name="m1"),
            Schedule(name="p2", target_name="m2"),
        ],
        state_machines=[
            StateMachine(name="m1", glue_jobs=["compartilhado"]),
            StateMachine(name="m2", glue_jobs=["compartilhado"]),
        ],
    )
    rows = build_process_costs(
        account, DEFAULT_CONFIG, today=date(2026, 7, 24)
    )

    assert len(rows) == 2
    assert sum(row.actual_dpu_hours for row in rows) == pytest.approx(2.0)
    assert all(row.allocation_method == "equal_share_across_processes" for row in rows)


def test_saving_is_conservative_and_capped_by_process_cost():
    account = Account(
        account_id="123",
        process_costs=[
            ProcessCost(
                process_id="glue_job:processa",
                process_name="processa",
                root_type="glue_job",
                component_names=["processa"],
                actual_cost_window=100.0,
            )
        ],
    )
    opportunity = Opportunity(
        opportunity_id="x",
        account="123",
        asset_type="glue_job",
        asset_name="processa",
        category="cost_optimization",
        rule_id="TEST",
        finding="teste",
        recommended_action="teste",
        difficulty_score=1,
        estimation=Estimation(
            method="test",
            baseline_cost=1000,
            projected_cost=100,
            estimated_saving=900,
        ),
    )
    apply_conservative_caps(
        account, [opportunity], DEFAULT_CONFIG, today=date(2026, 7, 24)
    )

    # O teto é o custo do processo por mês — a janela medida convertida, não
    # uma projeção para o fim do mês-calendário.
    cap = account.process_costs[0].monthly_cost
    assert opportunity.estimated_gain.monthly_expected == pytest.approx(cap, abs=0.01)
    assert opportunity.estimated_gain.monthly_high <= round(cap, 2)
    assert opportunity.estimation is not None
    assert opportunity.estimation.estimated_saving <= round(cap, 2)


def test_process_cap_is_shared_across_related_assets():
    account = Account(
        account_id="123",
        process_costs=[
            ProcessCost(
                process_id="glue_job:processa",
                process_name="processa",
                root_type="glue_job",
                component_names=["processa"],
                actual_cost_window=100.0,
            )
        ],
    )

    def opportunity(asset_name: str, source_process: str | None = None):
        item = Opportunity(
            opportunity_id=asset_name,
            account="123",
            asset_type="table" if source_process else "glue_job",
            asset_name=asset_name,
            category="cost_optimization",
            rule_id="TEST",
            finding="teste",
            recommended_action="teste",
            difficulty_score=1,
            source_process=source_process,
            estimation=Estimation(
                method="test",
                baseline_cost=100.0,
                projected_cost=10.0,
                estimated_saving=90.0,
            ),
        )
        prioritizer.assign(item)
        return item

    opportunities = [
        opportunity("processa"),
        opportunity("saida", source_process="processa"),
    ]
    apply_conservative_caps(
        account, opportunities, DEFAULT_CONFIG, today=date(2026, 7, 24)
    )

    assert sum(
        item.estimation.estimated_saving for item in opportunities
    ) == pytest.approx(account.process_costs[0].monthly_cost, abs=0.01)


def test_blocked_state_survives_reprioritization():
    item = Opportunity(
        opportunity_id="x",
        account="123",
        asset_type="glue_job",
        asset_name="processa",
        category="cost_optimization",
        rule_id="TEST",
        finding="teste",
        recommended_action="teste",
        how_to_validate="teste",
        evidence=["evidência"],
        owner="dados",
        gain_score=100,
        confidence=1.0,
        blocked=True,
    )

    prioritizer.assign(item)

    assert item.blocked is True
    assert item.bucket != "fazer_agora"


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **_):
        yield from self.pages


class _GlueInventory:
    def __init__(self, now):
        self.now = now

    def get_paginator(self, name):
        if name == "get_crawlers":
            return _Pages(
                [
                    {
                        "Crawlers": [
                            {
                                "Name": "catalogo",
                                "State": "READY",
                                "DatabaseName": "lake",
                                "Schedule": {
                                    "ScheduleExpression": "cron(0 1 * * ? *)",
                                    "State": "SCHEDULED",
                                },
                                "LastCrawl": {
                                    "Status": "SUCCEEDED",
                                    "StartTime": self.now,
                                },
                            }
                        ]
                    }
                ]
            )
        if name == "get_crawler_metrics":
            return _Pages(
                [
                    {
                        "CrawlerMetricsList": [
                            {
                                "CrawlerName": "catalogo",
                                "LastRuntimeSeconds": 120.0,
                                "MedianRuntimeSeconds": 100.0,
                                "TablesUpdated": 2,
                            }
                        ]
                    }
                ]
            )
        if name == "list_crawls":
            return _Pages(
                [
                    {
                        "Crawls": [
                            {
                                "CrawlId": "crawl-1",
                                "State": "COMPLETED",
                                "StartTime": self.now,
                                "DPUHour": 0.25,
                            }
                        ]
                    }
                ]
            )
        if name == "get_triggers":
            return _Pages(
                [
                    {
                        "Triggers": [
                            {
                                "Name": "diario",
                                "Type": "SCHEDULED",
                                "Schedule": "cron(0 1 * * ? *)",
                                "Actions": [
                                    {"JobName": "processa"},
                                    {"CrawlerName": "catalogo"},
                                ],
                            }
                        ]
                    }
                ]
            )
        raise AssertionError(name)


def test_collects_crawler_dpu_and_glue_trigger_lineage():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    # `now` e o corte da janela; execucoes precisam cair em dia fechado.
    ran_at = now - timedelta(days=1)
    client = _GlueInventory(ran_at)

    crawler = crawlers_collector.collect_crawlers(client, window=AnalysisWindow.trailing(now=now))[0]
    trigger = glue_triggers_collector.collect_triggers(client)[0]

    assert crawler.dpu_hours_window == 0.25
    assert crawler.tables_updated == 2
    assert trigger.job_names == ["processa"]
    assert trigger.crawler_names == ["catalogo"]


class _DataBrew:
    def __init__(self, now):
        self.now = now

    def get_paginator(self, name):
        if name == "list_jobs":
            return _Pages(
                [
                    {
                        "Jobs": [
                            {
                                "Name": "limpeza",
                                "Type": "RECIPE",
                                "MaxCapacity": 10,
                                "Timeout": 60,
                            }
                        ]
                    }
                ]
            )
        if name == "list_schedules":
            return _Pages(
                [
                    {
                        "Schedules": [
                            {
                                "Name": "diario",
                                "JobNames": ["limpeza"],
                                "CronExpression": "cron(0 1 * * ? *)",
                            }
                        ]
                    }
                ]
            )
        if name == "list_job_runs":
            return _Pages(
                [
                    {
                        "JobRuns": [
                            {
                                "State": "SUCCEEDED",
                                "CreatedOn": self.now,
                                "ExecutionTime": 3600,
                            }
                        ]
                    }
                ]
            )
        raise AssertionError(name)


def test_databrew_cost_keeps_node_hours_separate_from_dpu_hours():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    # `now` e o corte da janela; execucoes precisam cair em dia fechado.
    ran_at = now - timedelta(days=1)
    job = databrew_collector.collect_jobs(_DataBrew(ran_at), window=AnalysisWindow.trailing(now=now))[0]

    assert job.execution_hours_window == 1.0
    assert job.estimated_node_hours_window == 10.0
    assert job.schedule_names == ["diario"]
    assert job.expected_runs_monthly == 30.0


_OBSERVABILITY_VALUES = {
    "glue.driver.workerUtilization": 0.2,
    "glue.ALL.memory.total.used.percentage": 0.4,
    "glue.ALL.disk.used.percentage": 0.3,
    "glue.driver.skewness.job": 1.5,
    "glue.driver.ExecutorAllocationManager.executors.numberAllExecutors": 10,
    "glue.driver.ExecutorAllocationManager.executors.numberMaxNeededExecutors": 4,
    "glue.driver.aggregate.bytesRead": 8 * 1024**3,
    "glue.driver.throughput.bytesWritten": 4 * 1024**3,
    "glue.driver.throughput.filesWritten": 200,
    "glue.driver.streaming.numRecords": 1000,
}


class _CloudWatch:
    def get_metric_data(self, **kwargs):
        results = []
        for query in kwargs["MetricDataQueries"]:
            metric = query["MetricStat"]["Metric"]
            # Métrica de observabilidade é agregada em JobRunId=ALL; sem essa
            # dimensão a resposta viria por execução e a soma da janela mudaria.
            assert {"Name": "JobRunId", "Value": "ALL"} in metric["Dimensions"]
            results.append(
                {
                    "Id": query["Id"],
                    "Values": [_OBSERVABILITY_VALUES[metric["MetricName"]]],
                }
            )
        return {"MetricDataResults": results}


def test_collects_observability_metrics_needed_for_capacity_decisions():
    job = GlueJob(name="processa")
    cloudwatch_collector.enrich_glue_observability(
        _CloudWatch(),
        [job],
        window=AnalysisWindow.trailing(
            now=datetime(2026, 7, 24, tzinfo=timezone.utc)
        ),
    )

    assert job.avg_worker_utilization == 0.2
    assert job.max_memory_used_pct == 0.4
    assert job.max_disk_used_pct == 0.3
    assert job.max_task_skew == 1.5
    assert job.avg_all_executors == 10
    assert job.avg_max_needed_executors == 4
    assert job.bytes_read_window == 8 * 1024**3
    assert job.bytes_written_window == 4 * 1024**3
    assert job.files_written_window == 200
    assert job.streaming_records_window == 1000
    assert job.average_output_file_bytes == pytest.approx(4 * 1024**3 / 200)


class _CloudWatchPeaks:
    def get_metric_data(self, **kwargs):
        return {
            "MetricDataResults": [
                {
                    "Id": query["Id"],
                    "Values": (
                        [0.2, 0.8]
                        if query["MetricStat"]["Stat"] == "Maximum"
                        else [0.2, 0.4]
                    ),
                }
                for query in kwargs["MetricDataQueries"]
            ]
        }


def test_observability_preserves_peaks_instead_of_averaging_them():
    job = GlueJob(name="processa")

    cloudwatch_collector.enrich_glue_observability(
        _CloudWatchPeaks(),
        [job],
        window=AnalysisWindow.trailing(
            now=datetime(2026, 7, 24, tzinfo=timezone.utc)
        ),
    )

    assert job.max_memory_used_pct == 0.8
    assert job.max_disk_used_pct == 0.8
    assert job.avg_worker_utilization == pytest.approx(0.3)


class _Sessions:
    def __init__(self, now):
        self.now = now

    def get_paginator(self, name):
        assert name == "list_sessions"
        return _Pages(
            [
                {
                    "Sessions": [
                        {
                            "Id": "session-1",
                            "Status": "READY",
                            "CreatedOn": self.now.replace(day=23),
                            "IdleTimeout": 2880,
                            "MaxCapacity": 5.0,
                            "DPUSeconds": 18000.0,
                        }
                    ]
                }
            ]
        )

    def list_statements(self, **_):
        start = self.now.replace(day=23, hour=1)
        return {
            "Statements": [
                {
                    "Id": 1,
                    "StartedOn": start,
                    "CompletedOn": start.replace(hour=2),
                }
            ]
        }


class _SessionTags(_Sessions):
    class _Meta:
        region_name = "sa-east-1"
        partition = "aws"

    meta = _Meta()

    def get_tags(self, **kwargs):
        assert kwargs == {
            "ResourceArn": (
                "arn:aws:glue:sa-east-1:123456789012:session/session-1"
            )
        }
        return {
            "Tags": {
                "owner": (
                    "ARO47GCAHI5VXYBL4CCT:"
                    "adriano.vilela-costa@itau-unibanco.com.br"
                )
            }
        }


class _SessionTagsDenied(_SessionTags):
    def get_tags(self, **_kwargs):
        raise RuntimeError("sem acesso")


def test_session_idle_is_derived_from_statement_activity():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    # `now` e o corte da janela; execucoes precisam cair em dia fechado.
    ran_at = now - timedelta(days=1)
    session = sessions_collector.collect_sessions(_Sessions(ran_at), window=AnalysisWindow.trailing(now=now))[0]

    assert session.activity_evidence is True
    assert session.observed_runs == 1
    assert session.idle_hours_per_day == pytest.approx(23.0)
    assert session.dpu_hours == 5.0


def test_session_owner_is_loaded_with_get_tags_and_saved_as_email():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    session = sessions_collector.collect_sessions(
        _SessionTags(now - timedelta(days=1)),
        window=AnalysisWindow.trailing(now=now),
        account_id="123456789012",
    )[0]

    assert session.owner_tag == "adriano.vilela-costa@itau-unibanco.com.br"


def test_session_without_get_tags_permission_is_still_collected():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    gaps: list[str] = []
    sessions = sessions_collector.collect_sessions(
        _SessionTagsDenied(now - timedelta(days=1)),
        window=AnalysisWindow.trailing(now=now),
        account_id="123456789012",
        gaps=gaps,
    )

    assert len(sessions) == 1
    assert sessions[0].owner_tag is None
    assert gaps and gaps[0].startswith("get_tags[session-1]:")


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (
            {
                "owner": (
                    "ARO47GCAHI5VXYBL4CCT:"
                    "adriano.vilela-costa@itau-unibanco.com.br"
                )
            },
            "adriano.vilela-costa@itau-unibanco.com.br",
        ),
        ({"Owner": "pessoa@empresa.com.br"}, "pessoa@empresa.com.br"),
        ({"OWNER": "squad-dados"}, "squad-dados"),
        ({}, None),
    ],
)
def test_interactive_session_owner_tag_uses_the_email_after_the_principal_id(
    tags, expected
):
    assert sessions_collector._owner_from_tags(tags) == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("rate(1 hour)", 720.0),
        ("cron(0 1 * * ? *)", 30.0),
        ("cron(0 1 ? * MON *)", 4.33),
    ],
)
def test_normalizes_common_schedule_frequencies(expression, expected):
    assert expected_runs_per_month(expression) == expected


class _SparkLogS3:
    def __init__(self, content: bytes, *, size: int | None = None):
        self.content = content
        self.size = len(content) if size is None else size

    def list_objects_v2(self, **kwargs):
        assert kwargs == {"Bucket": "logs", "Prefix": "prefix/", "MaxKeys": 1000}
        return {
            "Contents": [
                {
                    "Key": "prefix/run-1",
                    "Size": self.size,
                    "LastModified": datetime.now(timezone.utc),
                }
            ]
        }

    def get_object(self, **kwargs):
        assert kwargs == {"Bucket": "logs", "Key": "prefix/run-1"}
        return {"Body": io.BytesIO(self.content)}


def test_spark_event_logs_collect_complete_spill_evidence():
    event = {
        "Event": "SparkListenerTaskEnd",
        "Task Metrics": {
            "Disk Bytes Spilled": 100,
            "Shuffle Read Metrics": {
                "Remote Bytes Read": 20,
                "Local Bytes Read": 5,
            },
            "Shuffle Write Metrics": {"Shuffle Bytes Written": 30},
        },
    }
    job = GlueJob(name="etl", spark_event_logs_path="s3://logs/prefix")

    spark_event_logs_collector.enrich_glue_shuffle(
        _SparkLogS3((json.dumps(event) + "\n").encode()),
        [job],
        window=AnalysisWindow.trailing(),
    )

    assert job.shuffle_spill_bytes == 100
    assert job.shuffle_read_bytes == 25
    assert job.shuffle_write_bytes == 30
    assert job.has_spill_evidence is True
    assert job.spark_event_log_evidence_complete is True


def test_spark_event_logs_do_not_claim_zero_when_log_is_too_large():
    job = GlueJob(name="etl", spark_event_logs_path="s3://logs/prefix")

    spark_event_logs_collector.enrich_glue_shuffle(
        _SparkLogS3(b"", size=10 * 1024 * 1024 + 1),
        [job],
        window=AnalysisWindow.trailing(),
    )

    assert job.shuffle_spill_bytes is None
    assert job.has_spill_evidence is False


def test_the_suggested_timeout_clears_the_longest_run_that_actually_happened():
    """Dobrar o p95 pode cortar um pico legítimo — que é o risco da própria regra.

    `max_execution_sec` já era coletado e ninguém o lia. Sem ele, um job com p95
    de 10 min e uma execução real de 45 min recebia a sugestão de 20 min, e
    aplicá-la mataria uma execução que a janela registrou funcionando.
    """
    from julius.knowledge.rules.glue.jobs import _timeout_sugerido

    job = GlueJob(
        name="picos",
        p95_execution_sec=600,
        avg_execution_sec=480,
        max_execution_sec=2700,
    )
    sem_pico = GlueJob(
        name="regular",
        p95_execution_sec=600,
        avg_execution_sec=480,
        max_execution_sec=660,
    )

    sugerido, max_min = _timeout_sugerido(job, job.p95_execution_sec / 60)
    assert max_min == 45
    # 1,25 × 45 min supera 2 × 10 min, então o piso é a execução observada.
    assert sugerido == 56

    # Sem pico relevante, o piso de 30 min que já existia continua decidindo.
    regular, _ = _timeout_sugerido(sem_pico, sem_pico.p95_execution_sec / 60)
    assert regular == 30
