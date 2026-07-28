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


class _FakeCloudWatch:
    """Responde `get_metric_data` a partir de um mapa métrica → valores.

    O Stubber valida a forma da chamada, e há um teste dedicado a isso logo
    abaixo. Aqui o que importa é o mapeamento: qual consulta alimenta qual campo
    de qual job, o que só se enxerga controlando a resposta por `Id`.
    """

    def __init__(self, values_by_metric: dict[str, list[float]], pages: int = 1):
        self.values_by_metric = values_by_metric
        self.pages = pages
        self.calls: list[dict] = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        page = len(self.calls)
        results = []
        for query in kwargs["MetricDataQueries"]:
            metric = query["MetricStat"]["Metric"]["MetricName"]
            values = self.values_by_metric.get(metric, [])
            # Numa paginação, cada página traz um pedaço dos valores.
            chunk = values[page - 1 :: self.pages] if self.pages > 1 else values
            results.append({"Id": query["Id"], "Values": list(chunk)})
        response = {"MetricDataResults": results}
        if page < self.pages:
            response["NextToken"] = f"token-{page}"
        return response


class _BrokenCloudWatch:
    def get_metric_data(self, **_kwargs):
        raise AwsError("throttled")


class AwsError(Exception):
    pass


def test_cloudwatch_asks_for_cpu_with_the_shape_the_api_expects():
    """Stubber: um parâmetro fora do lugar aqui é erro de chamada real.

    A CPU é a única métrica sem `JobRunId` — ela é publicada por job, não por
    execução — e é isso que este teste prende.
    """
    cw = _client("cloudwatch")
    stub = Stubber(cw)
    now = datetime.now(timezone.utc)
    window = AnalysisWindow.trailing(now=now)
    stub.add_response(
        "get_metric_data",
        {"MetricDataResults": [{"Id": "m0", "Values": [0.20, 0.24]}]},
        {
            "MetricDataQueries": [
                {
                    "Id": "m0",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "Glue",
                            "MetricName": "glue.ALL.system.cpuSystemLoad",
                            "Dimensions": [
                                {"Name": "JobName", "Value": "processa"},
                                {"Name": "Type", "Value": "gauge"},
                            ],
                        },
                        "Period": 86400,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
            ],
            "StartTime": window.start,
            "EndTime": window.end,
        },
    )
    jobs = [GlueJob(name="processa", worker_type="G.1X", number_of_workers=20)]
    with stub:
        cloudwatch_collector.enrich_glue_cpu(cw, jobs, window=window)

    # média das duas leituras → destrava as regras de capacidade.
    assert jobs[0].avg_cpu_load == pytest.approx(0.22, abs=0.001)


def test_every_metric_of_every_job_travels_in_a_single_call():
    """Era uma ida à AWS por métrica por job; são dez métricas e três jobs."""
    cw = _FakeCloudWatch(
        {
            "glue.driver.workerUtilization": [0.4, 0.6],
            "glue.ALL.memory.total.used.percentage": [10.0, 90.0],
            "glue.driver.aggregate.bytesRead": [100.0, 200.0],
        }
    )
    jobs = [GlueJob(name=f"job-{index}") for index in range(3)]

    cloudwatch_collector.enrich_glue_observability(
        cw, jobs, window=AnalysisWindow.trailing()
    )

    assert len(cw.calls) == 1
    assert len(cw.calls[0]["MetricDataQueries"]) == 30  # 3 jobs × 10 métricas
    for job in jobs:
        assert job.avg_worker_utilization == pytest.approx(0.5)
        assert job.bytes_read_window == pytest.approx(300.0)


def test_the_peak_is_preserved_and_the_average_is_averaged():
    """Suavizar pressão de memória, disco ou skew esconderia o pior momento."""
    cw = _FakeCloudWatch(
        {
            "glue.ALL.memory.total.used.percentage": [10.0, 90.0],
            "glue.driver.workerUtilization": [10.0, 90.0],
        }
    )
    jobs = [GlueJob(name="processa")]

    cloudwatch_collector.enrich_glue_observability(
        cw, jobs, window=AnalysisWindow.trailing()
    )

    assert jobs[0].max_memory_used_pct == pytest.approx(90.0)
    assert jobs[0].avg_worker_utilization == pytest.approx(50.0)


def test_queries_are_split_when_they_pass_the_api_ceiling():
    """500 consultas por chamada é limite da API, não escolha nossa."""
    cw = _FakeCloudWatch({"glue.driver.workerUtilization": [1.0]})
    jobs = [GlueJob(name=f"job-{index}") for index in range(60)]  # 60 × 10 = 600

    cloudwatch_collector.enrich_glue_observability(
        cw, jobs, window=AnalysisWindow.trailing()
    )

    assert [len(call["MetricDataQueries"]) for call in cw.calls] == [500, 100]
    assert all(job.avg_worker_utilization == pytest.approx(1.0) for job in jobs)


def test_a_paginated_answer_is_accumulated_before_being_reduced():
    """Reduzir página a página daria a média das médias, não a da janela."""
    cw = _FakeCloudWatch({"glue.ALL.memory.total.used.percentage": [10.0, 90.0]}, pages=2)
    jobs = [GlueJob(name="processa")]

    cloudwatch_collector.enrich_glue_observability(
        cw, jobs, window=AnalysisWindow.trailing()
    )

    assert len(cw.calls) == 2
    assert jobs[0].max_memory_used_pct == pytest.approx(90.0)


def test_a_failed_block_leaves_the_fields_absent_not_zero():
    """Zero significaria "medido e vazio"; a métrica ausente não é isso."""
    jobs = [GlueJob(name="processa")]

    cloudwatch_collector.enrich_glue_observability(
        _BrokenCloudWatch(), jobs, window=AnalysisWindow.trailing()
    )

    assert jobs[0].max_memory_used_pct is None
    assert jobs[0].avg_worker_utilization is None
    assert jobs[0].bytes_read_window is None


def test_a_metric_with_no_datapoints_stays_absent():
    cw = _FakeCloudWatch({})
    jobs = [GlueJob(name="processa")]

    cloudwatch_collector.enrich_glue_observability(
        cw, jobs, window=AnalysisWindow.trailing()
    )

    assert jobs[0].max_task_skew is None


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


class _RedshiftCostExplorer:
    """Cobrança agregada por usage type, como o Cost Explorer entrega."""

    def __init__(self, groups=None):
        self.groups = groups if groups is not None else [
            ("SAE1-Node:ra3.xlplus", "1670.20"),
            ("SAE1-RMS-Storage-ByteHrs", "168.40"),
            ("SAE1-Backup-ByteHrs", "35.80"),
        ]

    def get_cost_and_usage(self, **_):
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": [usage_type],
                            "Metrics": {
                                "NetUnblendedCost": {"Amount": amount, "Unit": "USD"}
                            },
                        }
                        for usage_type, amount in self.groups
                    ]
                }
            ]
        }


def test_redshift_cost_separates_compute_from_what_survives_a_pause():
    """Armazenamento continua sendo cobrado com o cluster parado."""
    from julius.collection.collectors import redshift_cost
    from julius.knowledge.redshift_cost import (
        REDSHIFT_COMPUTE_BUCKETS,
        REDSHIFT_USAGE_TYPE_MARKERS,
    )

    coverage = redshift_cost.collect_redshift_costs(
        _RedshiftCostExplorer(),
        window=AnalysisWindow.trailing(),
        markers=REDSHIFT_USAGE_TYPE_MARKERS,
    )

    assert coverage.buckets["node_hours"] == 1670.20
    assert coverage.buckets["managed_storage"] == 168.40
    assert coverage.buckets["backup"] == 35.80
    assert coverage.compute_cost(REDSHIFT_COMPUTE_BUCKETS) == 1670.20
    assert coverage.net_cost == 1874.40


def test_redshift_compute_is_split_by_declared_capacity():
    from julius.collection.collectors import redshift_cost
    from julius.collection.models import Account, RedshiftCluster
    from julius.knowledge.redshift_cost import (
        REDSHIFT_COMPUTE_BUCKETS,
        REDSHIFT_USAGE_TYPE_MARKERS,
    )

    account = Account(
        account_id="123456789012",
        redshift_clusters=[
            RedshiftCluster(name="a", node_count=4, observed_days=30),
            RedshiftCluster(name="b", node_count=12, observed_days=30),
        ],
    )
    coverage = redshift_cost.collect_redshift_costs(
        _RedshiftCostExplorer(),
        window=AnalysisWindow.trailing(),
        markers=REDSHIFT_USAGE_TYPE_MARKERS,
    )
    redshift_cost.allocate_costs(account, coverage, REDSHIFT_COMPUTE_BUCKETS)

    a, b = account.redshift_clusters
    assert a.allocated_compute_cost == round(1670.20 * 4 / 16, 2)
    assert b.allocated_compute_cost == round(1670.20 * 12 / 16, 2)
    # Só o compute é rateado: armazenamento não some ao pausar.
    assert a.allocated_compute_cost + b.allocated_compute_cost < coverage.net_cost


def test_an_unknown_usage_type_is_never_dropped_in_silence():
    from julius.collection.collectors import redshift_cost
    from julius.knowledge.redshift_cost import REDSHIFT_USAGE_TYPE_MARKERS

    coverage = redshift_cost.collect_redshift_costs(
        _RedshiftCostExplorer([("SAE1-CoisaNova-Hrs", "12.00")]),
        window=AnalysisWindow.trailing(),
        markers=REDSHIFT_USAGE_TYPE_MARKERS,
    )

    assert coverage.unknown_usage_types == ["SAE1-CoisaNova-Hrs"]
    assert coverage.buckets["other"] == 12.00


def test_capacity_unknown_leaves_the_cost_unallocated_instead_of_guessing():
    from julius.collection.collectors import redshift_cost
    from julius.collection.models import Account, RedshiftCluster
    from julius.knowledge.redshift_cost import (
        REDSHIFT_COMPUTE_BUCKETS,
        REDSHIFT_USAGE_TYPE_MARKERS,
    )

    account = Account(
        account_id="123456789012",
        redshift_clusters=[RedshiftCluster(name="sem-capacidade", node_count=0)],
    )
    coverage = redshift_cost.collect_redshift_costs(
        _RedshiftCostExplorer(),
        window=AnalysisWindow.trailing(),
        markers=REDSHIFT_USAGE_TYPE_MARKERS,
    )
    redshift_cost.allocate_costs(account, coverage, REDSHIFT_COMPUTE_BUCKETS)

    assert account.redshift_clusters[0].allocated_compute_cost is None
    assert any("não rateado" in gap for gap in coverage.gaps)
