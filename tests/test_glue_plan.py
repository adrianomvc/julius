from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone

import boto3
import pytest
from botocore.stub import Stubber

from julius.aws import (
    cloudwatch_collector,
    crawlers_collector,
    databrew_collector,
    glue_collector,
    glue_triggers_collector,
    sessions_collector,
    spark_event_logs_collector,
)
from julius.aws.schedule_frequency import expected_runs_per_month
from julius.config import DEFAULT_CONFIG
from julius.estimation.process_cost import (
    apply_conservative_caps,
    build_process_costs,
)
from julius.graph.ownership import resolve_owner
from julius.inventory.model import (
    Account,
    ActorEvent,
    GlueJob,
    ProcessCost,
    Schedule,
    StateMachine,
)
from julius.opportunities.base import Estimation, Opportunity
from julius.opportunities import prioritizer
from julius.opportunities.detectors import glue as glue_detector


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
                    "StartedOn": now,
                },
                {
                    "Id": "jr-estimated",
                    "JobRunState": "SUCCEEDED",
                    "ExecutionTime": 1800,
                    "WorkerType": "G.1X",
                    "NumberOfWorkers": 2,
                    "StartedOn": now,
                },
            ]
        },
    )
    with stub:
        job = glue_collector.collect_jobs(glue, now=now)[0]

    assert job.job_mode == "VISUAL"
    assert job.actual_dpu_hours_window == 1.0
    assert job.estimated_dpu_hours_window == 1.0
    assert job.run_ids_in_window == ["jr-estimated", "jr-reported"]


def test_usd_is_canonical_and_mtd_is_forecast_before_monthly_savings():
    job = GlueJob(
        name="processa",
        dpu_seconds_window=36000,
        window_end="2026-07-10",
    )

    assert DEFAULT_CONFIG.pricing.currency == "USD"
    assert job.total_dpu_hours_window == 10.0
    assert job.forecast_dpu_hours_eom == pytest.approx(31.0)
    assert job.window_dpu_hours == pytest.approx(31.0)


def test_flex_requires_supported_spark_batch_job():
    eligible = GlueJob(
        name="batch",
        glue_version="4.0",
        command_type="glueetl",
        execution_class="STANDARD",
        time_sensitive=False,
        runs_per_month=10,
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
        runs_per_month=10,
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
                forecast_cost_eom=100.0,
                component_names=["processa"],
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

    assert opportunity.estimated_gain.monthly_expected == 100.0
    assert opportunity.estimated_gain.monthly_high <= 100.0
    assert opportunity.estimation is not None
    assert opportunity.estimation.estimated_saving <= 100.0


def test_process_cap_is_shared_across_related_assets():
    account = Account(
        account_id="123",
        process_costs=[
            ProcessCost(
                process_id="glue_job:processa",
                process_name="processa",
                root_type="glue_job",
                forecast_cost_eom=100.0,
                component_names=["processa"],
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
    ) == pytest.approx(100.0)


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
    client = _GlueInventory(now)

    crawler = crawlers_collector.collect_crawlers(client, now=now)[0]
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
    job = databrew_collector.collect_jobs(_DataBrew(now), now=now)[0]

    assert job.execution_hours_window == 1.0
    assert job.estimated_node_hours_window == 10.0
    assert job.schedule_names == ["diario"]
    assert job.expected_runs_monthly == 30.0


class _CloudWatch:
    def get_metric_statistics(self, **kwargs):
        assert {"Name": "JobRunId", "Value": "ALL"} in kwargs["Dimensions"]
        statistic = kwargs["Statistics"][0]
        value = {
            "glue.driver.workerUtilization": 0.2,
            "glue.ALL.memory.total.used.percentage": 0.4,
            "glue.ALL.disk.used.percentage": 0.3,
            "glue.driver.skewness.job": 1.5,
            "glue.driver.ExecutorAllocationManager.executors.numberAllExecutors": 10,
            "glue.driver.ExecutorAllocationManager.executors.numberMaxNeededExecutors": 4,
        }[kwargs["MetricName"]]
        return {"Datapoints": [{statistic: value}]}


def test_collects_observability_metrics_needed_for_capacity_decisions():
    job = GlueJob(name="processa")
    cloudwatch_collector.enrich_glue_observability(
        _CloudWatch(),
        [job],
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )

    assert job.avg_worker_utilization == 0.2
    assert job.max_memory_used_pct == 0.4
    assert job.max_disk_used_pct == 0.3
    assert job.max_task_skew == 1.5
    assert job.avg_all_executors == 10
    assert job.avg_max_needed_executors == 4


class _CloudWatchPeaks:
    def get_metric_statistics(self, **kwargs):
        statistic = kwargs["Statistics"][0]
        values = [0.2, 0.8] if statistic == "Maximum" else [0.2, 0.4]
        return {"Datapoints": [{statistic: value} for value in values]}


def test_observability_preserves_peaks_instead_of_averaging_them():
    job = GlueJob(name="processa")

    cloudwatch_collector.enrich_glue_observability(
        _CloudWatchPeaks(),
        [job],
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
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


def test_session_idle_is_derived_from_statement_activity():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    session = sessions_collector.collect_sessions(_Sessions(now), now=now)[0]

    assert session.activity_evidence is True
    assert session.observed_runs == 1
    assert session.idle_hours_per_day == pytest.approx(23.0)
    assert session.dpu_hours == 5.0


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
    )

    assert job.shuffle_spill_bytes is None
    assert job.has_spill_evidence is False
