"""Testes dos coletores boto3 com botocore Stubber (sem AWS real)."""

from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import Stubber

from julius.collection.collectors import cloudwatch as cloudwatch_collector
from julius.collection.collectors import cost_explorer as cost_explorer
from julius.collection.collectors import datawarm as datawarm_collector
from julius.collection.collectors import schedules as schedules_collector
from julius.collection.collectors import stepfunctions as stepfunctions_collector
from julius.collection.collectors import touches as touches_collector
from julius.collection.collectors.glue import jobs as glue_collector
from julius.collection.currency import UnsupportedCurrencyError
from julius.collection.models import Account, GlueJob, Table
from julius.collection.window import AnalysisWindow, BillingMonth


def _client(service: str):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_cost_explorer_maps_services():
    ce = _client("ce")
    stub = Stubber(ce)
    stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "Estimated": True,
                    "Groups": [
                        {"Keys": ["AWS Glue"], "Metrics": {"UnblendedCost": {"Amount": "21400", "Unit": "USD"}}},
                        {"Keys": ["Amazon Athena"], "Metrics": {"UnblendedCost": {"Amount": "6800", "Unit": "USD"}}},
                        {"Keys": ["Amazon Elastic Compute Cloud"], "Metrics": {"UnblendedCost": {"Amount": "500", "Unit": "USD"}}},
                    ]
                }
            ]
        },
    )
    with stub:
        services = cost_explorer.collect_services(ce, billing=BillingMonth.current())

    by_name = {s.name: s.monthly_cost for s in services}
    assert by_name["AWS Glue"] == 21400
    assert by_name["Amazon Athena"] == 6800
    assert by_name["Outros"] == 500  # serviço fora do escopo → agregado
    assert all(service.currency == "USD" for service in services)
    assert all(service.period_kind == "month_to_date" for service in services)
    assert all(service.estimated is True for service in services)


def test_cost_explorer_first_day_uses_a_valid_exclusive_end():
    ce = _client("ce")
    stub = Stubber(ce)
    stub.add_response(
        "get_cost_and_usage",
        {"ResultsByTime": []},
        {
            "TimePeriod": {"Start": "2026-07-01", "End": "2026-07-02"},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        },
    )
    with stub:
        assert cost_explorer.collect_services(ce, billing=BillingMonth.current(now=datetime(2026, 7, 1, tzinfo=timezone.utc))) == []


def test_cost_explorer_rejects_a_response_outside_usd():
    """A AWS reporta custo em USD mesmo com fatura em outra moeda."""
    ce = _client("ce")
    stub = Stubber(ce)
    stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "Estimated": True,
                    "Groups": [
                        {
                            "Keys": ["AWS Glue"],
                            "Metrics": {"UnblendedCost": {"Amount": "543", "Unit": "BRL"}},
                        }
                    ],
                }
            ]
        },
    )
    with stub, pytest.raises(UnsupportedCurrencyError):
        cost_explorer.collect_services(ce, billing=BillingMonth.current())


def test_cost_explorer_accepts_a_zero_amount_with_an_odd_unit():
    """Zero não tem moeda; unidade estranha em grupo zerado não bloqueia."""
    ce = _client("ce")
    stub = Stubber(ce)
    stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "Estimated": True,
                    "Groups": [
                        {
                            "Keys": ["AWS Glue"],
                            "Metrics": {"UnblendedCost": {"Amount": "0", "Unit": "N/A"}},
                        }
                    ],
                }
            ]
        },
    )
    with stub:
        services = cost_explorer.collect_services(ce, billing=BillingMonth.current())

    assert services[0].monthly_cost == 0
    assert services[0].currency == "USD"


def test_glue_collector_jobs_and_failure_rate():
    glue = _client("glue")
    stub = Stubber(glue)
    stub.add_response(
        "get_jobs",
        {
            "Jobs": [
                {
                    "Name": "processa",
                    "GlueVersion": "1.0",
                    "WorkerType": "G.1X",
                    "NumberOfWorkers": 20,
                    "ExecutionClass": "STANDARD",
                    "Timeout": 2880,
                    "MaxRetries": 3,
                    "DefaultArguments": {
                        "--enable-auto-scaling": "false",
                        "--job-bookmark-option": "job-bookmark-disable",
                    },
                    "Command": {"ScriptLocation": "s3://scripts/processa.py"},
                }
            ]
        },
    )
    now = datetime.now(timezone.utc)
    stub.add_response(
        "get_job_runs",
        {
            "JobRuns": [
                {"JobRunState": "SUCCEEDED", "ExecutionTime": 3600, "StartedOn": now},
                {"JobRunState": "SUCCEEDED", "ExecutionTime": 3600, "StartedOn": now},
                {"JobRunState": "FAILED", "ExecutionTime": 1200, "StartedOn": now},
            ]
        },
    )
    with stub:
        jobs = glue_collector.collect_jobs(glue, window=AnalysisWindow.trailing(days=90, now=now))

    assert len(jobs) == 1
    job = jobs[0]
    assert job.glue_version == "1.0"
    assert job.auto_scaling is False
    assert job.job_bookmark is False
    assert job.avg_execution_sec == 3600
    assert job.failure_rate == pytest.approx(1 / 3, abs=0.01)
    assert job.observed_runs == 3


def test_cloudwatch_enriches_cpu():
    cw = _client("cloudwatch")
    stub = Stubber(cw)
    now = datetime.now(timezone.utc)
    stub.add_response(
        "get_metric_statistics",
        {
            "Label": "glue.ALL.system.cpuSystemLoad",
            "Datapoints": [
                {"Timestamp": now, "Average": 0.20, "Unit": "None"},
                {"Timestamp": now, "Average": 0.24, "Unit": "None"},
            ],
        },
    )
    jobs = [GlueJob(name="processa", worker_type="G.1X", number_of_workers=20)]
    with stub:
        cloudwatch_collector.enrich_glue_cpu(cw, jobs, window=AnalysisWindow.trailing(now=now))

    # média das duas leituras → destrava as regras de capacidade.
    assert jobs[0].avg_cpu_load == pytest.approx(0.22, abs=0.001)


def test_touches_collector_parses_rows():
    athena = _client("athena")
    stub = Stubber(athena)
    stub.add_response("start_query_execution", {"QueryExecutionId": "q1"})
    stub.add_response(
        "get_query_execution",
        {"QueryExecution": {"QueryExecutionId": "q1", "Status": {"State": "SUCCEEDED"}}},
    )
    stub.add_response(
        "get_query_results",
        {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "tabela"}, {"VarCharValue": "toques"}, {"VarCharValue": "contas"}, {"VarCharValue": "comunidades"}]},
                    {"Data": [{"VarCharValue": "base_legado"}, {"VarCharValue": "0"}, {"VarCharValue": "0"}, {"VarCharValue": "0"}]},
                    {"Data": [{"VarCharValue": "resumo_regional"}, {"VarCharValue": "11"}, {"VarCharValue": "1"}, {"VarCharValue": "1"}]},
                ]
            }
        },
    )
    with stub:
        stats = touches_collector.collect_touches(
            athena,
            touches_table="governanca.toques",
            workgroup="julius",
            window=AnalysisWindow.trailing(),
        )

    assert stats["base_legado"].touches == 0
    assert stats["resumo_regional"].touches == 11
    assert stats["resumo_regional"].communities == 1


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **_):
        yield from self.pages


class _StepFunctions:
    def get_paginator(self, name):
        if name == "list_state_machines":
            return _Pages(
                [[
                    {
                        "stateMachines": [
                            {
                                "name": "orquestra",
                                "stateMachineArn": "arn:aws:states:x:1:stateMachine:orquestra",
                            }
                        ]
                    }
                ][0]]
            )
        if name == "list_executions":
            return _Pages([{"executions": []}])
        raise AssertionError(name)

    def describe_state_machine(self, **_):
        return {
            "type": "STANDARD",
            "definition": (
                '{"StartAt":"Glue","States":{"Glue":{"Type":"Task",'
                '"Resource":"arn:aws:states:::glue:startJobRun.sync",'
                '"Parameters":{"JobName":"transforma"},"End":true}}}'
            ),
        }


def test_stepfunctions_collector_extracts_glue_lineage():
    machines = stepfunctions_collector.collect_state_machines(_StepFunctions(), window=AnalysisWindow.trailing())
    assert len(machines) == 1
    assert machines[0].name == "orquestra"
    assert machines[0].glue_jobs == ["transforma"]


class _Events:
    def get_paginator(self, name):
        assert name == "list_rules"
        return _Pages(
            [
                {
                    "Rules": [
                        {
                            "Name": "cron-diario",
                            "ScheduleExpression": "cron(0 1 * * ? *)",
                        }
                    ]
                }
            ]
        )

    def list_targets_by_rule(self, **_):
        return {
            "Targets": [
                {
                    "Id": "sfn",
                    "Arn": "arn:aws:states:sa-east-1:1:stateMachine:orquestra",
                }
            ]
        }


def test_schedule_collector_links_state_machine():
    schedules = schedules_collector.collect_schedules(_Events())
    assert schedules[0].name == "cron-diario"
    assert schedules[0].target_name == "orquestra"


def test_datawarm_marks_published_tables():
    account = Account(
        account_id="consumer",
        glue_jobs=[GlueJob(name="publicador-datawarm", owner_tag="Squad Plataforma")],
        tables=[Table(name="produto", written_by="publicador-datawarm")],
    )
    assert datawarm_collector.mark_publications(account, "datawarm") == 1
    assert account.tables[0].datawarm_published is True
    assert account.tables[0].datawarm_owner == "Squad Plataforma"


_POLLING_ASL = (
    '{"StartAt":"Start","States":{'
    '"Start":{"Type":"Task","Resource":"arn:aws:states:::glue:startJobRun",'
    '"Parameters":{"JobName":"transforma"},"Next":"Wait",'
    '"Retry":[{"ErrorEquals":["States.ALL"],"MaxAttempts":5}]},'
    '"Wait":{"Type":"Wait","Seconds":30,"Next":"Check"},'
    '"Check":{"Type":"Task","Resource":"arn:aws:states:::aws-sdk:glue:getJobRun",'
    '"Next":"Choice"},'
    '"Choice":{"Type":"Choice","Next":"Wait"}}}'
)


class _PollingStepFunctions:
    """Uma máquina com loop de espera e histórico amostrável."""

    def __init__(self, executions=2, events_per_execution=None):
        self.executions = executions
        self.events_per_execution = events_per_execution
        self.history_calls = 0

    def get_paginator(self, name):
        if name == "list_state_machines":
            return _Pages(
                [
                    {
                        "stateMachines": [
                            {
                                "name": "orquestra",
                                "stateMachineArn": "arn:aws:states:x:1:stateMachine:orquestra",
                            }
                        ]
                    }
                ]
            )
        if name == "list_executions":
            now = datetime.now(timezone.utc)
            return _Pages(
                [
                    {
                        "executions": [
                            {
                                "executionArn": f"arn:exec:{index}",
                                "startDate": now,
                                "stopDate": now,
                            }
                            for index in range(self.executions)
                        ]
                    }
                ]
            )
        raise AssertionError(name)

    def describe_state_machine(self, **_):
        return {"type": "STANDARD", "definition": _POLLING_ASL}

    def get_execution_history(self, **_):
        self.history_calls += 1
        if self.events_per_execution is None:
            return {"events": []}
        return {
            "events": [
                {"type": "TaskStateEntered", "stateEnteredEventDetails": {"name": name}}
                for name in self.events_per_execution
            ]
        }


def test_stepfunctions_counts_transitions_from_the_execution_history():
    """Standard é cobrado por transição; sem contá-las não há baseline."""
    client = _PollingStepFunctions(
        events_per_execution=[
            "Start",
            "Wait", "Check", "Choice",
            "Wait", "Check", "Choice",
        ]
    )
    machines = stepfunctions_collector.collect_state_machines(
        client, window=AnalysisWindow.trailing()
    )

    machine = machines[0]
    assert machine.avg_state_transitions == 7
    assert machine.has_polling_loop is True
    # Seis entradas nos estados do loop, menos a passagem única de cada um.
    assert machine.poll_extra_transitions == 3
    assert machine.sampled_executions == 2
    assert machine.max_retry_attempts == 5


def test_stepfunctions_without_history_leaves_transitions_absent_not_zero():
    """Zero transições afirmaria que a máquina não custa nada."""
    machines = stepfunctions_collector.collect_state_machines(
        _PollingStepFunctions(events_per_execution=None),
        window=AnalysisWindow.trailing(),
    )

    machine = machines[0]
    assert machine.avg_state_transitions is None
    assert machine.poll_extra_transitions is None
    assert machine.has_polling_loop is True


def test_stepfunctions_history_sampling_has_an_explicit_ceiling():
    """Uma máquina de alto volume não vira varredura do histórico."""
    client = _PollingStepFunctions(executions=500, events_per_execution=["Start"])
    stepfunctions_collector.collect_state_machines(
        client, window=AnalysisWindow.trailing()
    )

    assert client.history_calls == stepfunctions_collector._MAX_SAMPLED_EXECUTIONS
