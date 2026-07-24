from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from julius.aws.athena_collector import (
    _has_partition_predicate,
    _result_reuse_eligible,
    _small_file_evidence,
    billable_bytes,
    collect_analysis,
    fingerprints,
    recurrence,
    resolve_actor,
)
from julius.ingest.dump import account_to_dataset
from julius.config import DEFAULT_CONFIG
from julius.inventory.model import Account
from julius.opportunities.detectors import athena as athena_detector
from julius.estimation import athena as athena_estimation
from julius.inventory.model import AthenaQuery
from julius.pipeline import analyze_account
from julius.report import renderer
from julius.state.history import HistoryStore

MB = 1024**2


def test_billable_bytes_rounding_minimum_and_non_billable_cases():
    assert billable_bytes(1, state="SUCCEEDED", statement_type="DML") == 10 * MB
    assert billable_bytes(10 * MB + 1, state="SUCCEEDED", statement_type="DML") == 11 * MB
    assert billable_bytes(7 * MB, state="CANCELLED", statement_type="DML") == 10 * MB
    assert billable_bytes(20 * MB, state="FAILED", statement_type="DML") == 0
    assert billable_bytes(20 * MB, state="SUCCEEDED", statement_type="DDL") == 0
    assert billable_bytes(20 * MB, state="SUCCEEDED", statement_type="DML", reused=True) == 0


def test_fingerprints_remove_literals_but_preserve_exact_identity():
    one = fingerprints("SELECT customer_id FROM db.sales WHERE dt = DATE '2026-07-01'")
    two = fingerprints("select customer_id from db.sales where dt = date '2026-07-02'")
    assert one[0] != two[0]
    assert one[1] == two[1]
    assert "2026-07" not in one[2]
    assert one[3] is True


def test_partition_ast_respects_aliases_joins_and_ctes():
    sql = """
        WITH filtered AS (
          SELECT a.id FROM db.sales a JOIN db.people p ON p.id = a.person_id
          WHERE a.dt = DATE '2026-07-01'
        )
        SELECT * FROM filtered
    """
    assert _has_partition_predicate(sql, "db.sales", "dt") is True
    assert _has_partition_predicate(sql, "db.people", "dt") is False


def test_small_files_requires_complete_s3_size_evidence():
    class S3:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return _Paginator(
                lambda **_: [{"Contents": [{"Key": f"k-{i}", "Size": MB} for i in range(100)]}]
            )

    count, average, confirmed = _small_file_evidence(S3(), "s3://bucket/prefix")
    assert (count, average, confirmed) == (100, MB, True)


def test_recurrence_burst_and_regular_are_deterministic():
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert recurrence([base, base + timedelta(minutes=10), base + timedelta(minutes=20)])[1]
    recurring, _, regular = recurrence(
        [base, base + timedelta(days=1), base + timedelta(days=2), base + timedelta(days=3)]
    )
    assert recurring is True
    assert regular is True


def test_identity_precedence_and_automation_classification():
    event = {
        "userIdentity": {
            "type": "AssumedRole",
            "sourceIdentity": "maria",
            "onBehalfOf": {"userId": "user-123"},
            "arn": "arn:aws:sts::1:assumed-role/pipeline/job",
        }
    }
    assert resolve_actor(event)[:4] == ("user-123", "human", "identity_center", "high")
    automation = {
        "userIdentity": {
            "type": "AssumedRole",
            "arn": "arn:aws:sts::1:assumed-role/athena-pipeline/nightly",
        }
    }
    assert resolve_actor(automation)[1] == "automation"


def test_result_reuse_gate_rejects_nondeterministic_queries():
    assert _result_reuse_eligible(
        "SELECT customer_id FROM db.sales", "DML", "on_demand"
    )
    assert not _result_reuse_eligible(
        "SELECT random() FROM db.sales", "DML", "on_demand"
    )
    assert not _result_reuse_eligible(
        "SELECT customer_id FROM db.sales", "DML", "federated"
    )


class _Paginator:
    def __init__(self, fn):
        self.fn = fn

    def paginate(self, **kwargs):
        return self.fn(**kwargs)


class _Athena:
    def __init__(self, executions):
        self.executions = executions

    def get_paginator(self, name):
        if name == "list_work_groups":
            return _Paginator(lambda **_: [{"WorkGroups": [{"Name": "one"}, {"Name": "two"}]}])
        if name == "list_query_executions":
            return _Paginator(
                lambda WorkGroup: [{"QueryExecutionIds": [
                    item["QueryExecutionId"] for item in self.executions
                    if item["WorkGroup"] == WorkGroup
                ]}]
            )
        raise AssertionError(name)

    def get_work_group(self, WorkGroup):
        return {
            "WorkGroup": {
                "Name": WorkGroup,
                "Configuration": {"PublishCloudWatchMetricsEnabled": True},
            }
        }

    def batch_get_query_execution(self, QueryExecutionIds):
        return {
            "QueryExecutions": [
                item for item in self.executions
                if item["QueryExecutionId"] in QueryExecutionIds
            ]
        }


class _CloudWatch:
    def __init__(self, totals):
        self.totals = totals

    def get_metric_statistics(self, **kwargs):
        assert kwargs["MetricName"] == "ProcessedBytes"
        workgroup = kwargs["Dimensions"][0]["Value"]
        return {"Datapoints": [{"Sum": self.totals.get(workgroup, 0)}]}


class _CostExplorer:
    def get_cost_and_usage(self, **kwargs):
        metric = kwargs["Metrics"][0]
        days = []
        for day in ("2026-07-22", "2026-07-23"):
            days.append(
                {
                    "TimePeriod": {"Start": day},
                    "Groups": [
                        {
                            "Keys": ["DataScanned-Bytes"],
                            "Metrics": {metric: {"Amount": "3", "Unit": "USD"}},
                        }
                    ],
                }
            )
        return {"ResultsByTime": days}


class _SingleDayCostExplorer:
    def get_cost_and_usage(self, **kwargs):
        metric = kwargs["Metrics"][0]
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-07-23"},
                    "Groups": [
                        {
                            "Keys": ["DataScanned-Bytes"],
                            "Metrics": {metric: {"Amount": "3", "Unit": "USD"}},
                        }
                    ],
                }
            ]
        }


class _CloudTrail:
    def __init__(self, executions):
        self.executions = executions

    def get_paginator(self, name):
        assert name == "lookup_events"

        def pages(**_):
            return [
                {
                    "Events": [
                        {
                            "CloudTrailEvent": json.dumps(
                                {
                                    "responseElements": {
                                        "queryExecutionId": item["QueryExecutionId"]
                                    },
                                    "userIdentity": {
                                        "type": "AssumedRole",
                                        "sourceIdentity": "maria",
                                    },
                                }
                            )
                        }
                        for item in self.executions
                    ]
                }
            ]

        return _Paginator(pages)


class _Glue:
    def get_table(self, **_):
        return {
            "Table": {
                "PartitionKeys": [{"Name": "dt"}],
                "StorageDescriptor": {"InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"},
            }
        }


def test_collection_reconciles_all_workgroups_and_never_serializes_raw_execution_data():
    now = datetime(2026, 7, 24, 15, tzinfo=timezone.utc)
    executions = []
    for index, (days_ago, literal) in enumerate(((2, "01"), (2, "02"), (1, "03"))):
        executions.append(
            {
                "QueryExecutionId": f"secret-query-id-{index}",
                "WorkGroup": "one",
                "Query": (
                    "SELECT * FROM db.sales "
                    f"WHERE dt = DATE '2026-07-{literal}' AND customer = 'private-{literal}'"
                ),
                "StatementType": "DML",
                "Status": {
                    "State": "SUCCEEDED",
                    "SubmissionDateTime": now - timedelta(days=days_ago),
                },
                "Statistics": {
                    "DataScannedInBytes": MB,
                    "EngineExecutionTimeInMillis": 100 + index,
                    "ResultReuseInformation": {"ReusedPreviousResult": False},
                },
            }
        )
    analysis = collect_analysis(
        _Athena(executions),
        cloudwatch_client=_CloudWatch({"one": 3 * MB, "two": 0}),
        cloudtrail_client=_CloudTrail(executions),
        glue_client=_Glue(),
        ce_client=_CostExplorer(),
        now=now,
    )
    assert analysis.coverage.workgroups_covered == 2
    assert analysis.coverage.cost_quality == "reconciled"
    assert analysis.coverage.currency == "USD"
    assert len(analysis.queries) == 1
    query = analysis.queries[0]
    assert query.recurring is True
    assert query.actor_count == 1
    assert query.selects_star is True
    assert query.missing_partition_filters == []
    assert query.allocated_cost == 6

    account = Account(
        account_id="123",
        currency="USD",
        athena_queries=analysis.queries,
        athena_actor_usage=analysis.actors,
        athena_coverage=analysis.coverage,
    )
    serialized = json.dumps(account_to_dataset(account))
    assert "secret-query-id" not in serialized
    assert "private-" not in serialized
    assert "2026-07-0" not in serialized

    integrated = analyze_account(account, scan_id="athena-monthly-test")
    report_html = renderer.render_html(integrated.vm)
    report_json = renderer.render_json(integrated.vm, integrated.opportunities)
    email_html, email_text = renderer.render_email(integrated.vm)
    assert "Athena — padrões de query e uso por pessoa" in report_html
    assert '"athena":' in report_json
    assert "Athena integrado" in email_html
    assert "Athena integrado" in email_text
    assert integrated.account.athena_actor_usage[0].opportunity_refs
    assert "athena-report" not in report_html + report_json + email_html + email_text


def test_result_reuse_saving_uses_only_exact_nearby_duplicates():
    now = datetime(2026, 7, 24, 15, tzinfo=timezone.utc)
    submitted = datetime(2026, 7, 23, 10, tzinfo=timezone.utc)
    executions = [
        {
            "QueryExecutionId": f"repeat-{index}",
            "WorkGroup": "one",
            "Query": "SELECT customer_id FROM db.sales WHERE dt = DATE '2026-07-23'",
            "StatementType": "DML",
            "Status": {
                "State": "SUCCEEDED",
                "SubmissionDateTime": submitted + timedelta(minutes=20 * index),
            },
            "Statistics": {"DataScannedInBytes": 10 * MB},
        }
        for index in range(3)
    ]
    analysis = collect_analysis(
        _Athena(executions),
        cloudwatch_client=_CloudWatch({"one": 30 * MB, "two": 0}),
        glue_client=_Glue(),
        ce_client=_SingleDayCostExplorer(),
        now=now,
    )
    query = analysis.queries[0]
    assert analysis.coverage.cost_quality == "reconciled"
    assert query.reuse_eligible_runs == 2
    assert query.reuse_avoidable_billed_bytes == 20 * MB
    assert query.reuse_avoidable_cost == 2

    estimate = athena_estimation.result_reuse_saving(query, DEFAULT_CONFIG)
    assert estimate.baseline_cost == 3
    assert estimate.estimated_saving == 2
    assert estimate.projected_cost == 1
    assert estimate.baseline_quality == "allocated"
    assert estimate.saving_quality == "measured"
    assert estimate.avoidable_bytes == 20 * MB

    account = Account(
        account_id="123",
        currency="USD",
        athena_queries=analysis.queries,
        athena_actor_usage=analysis.actors,
        athena_coverage=analysis.coverage,
    )
    report_html = renderer.render_html(analyze_account(account, scan_id="reuse").vm)
    assert "2 repetições exatas elegíveis" in report_html
    assert "US$ 2 evitável" in report_html
    assert "SAVE Medido" in report_html
    assert "US$ 3 (Alocado)" in report_html


def test_new_athena_patterns_do_not_invent_cost_when_reconciliation_is_partial():
    query = AthenaQuery(
        query_id="pattern",
        structural_fingerprint="pattern",
        executions_per_month=10,
        billed_bytes=1024**4,
        cost_quality="partial",
    )
    estimate = athena_estimation.projection_saving(query, DEFAULT_CONFIG)
    assert estimate.baseline_cost == 0
    assert estimate.estimated_saving == 0
    assert estimate.baseline_quality == "unavailable"
    assert estimate.saving_quality == "unavailable"
    assert estimate.projected_bytes is None
    assert "não reconciliado" in estimate.assumptions[-1]


def test_partition_and_projection_require_a_measured_counterfactual():
    query = AthenaQuery(
        query_id="pattern",
        structural_fingerprint="pattern",
        executions_per_month=4,
        billed_bytes=400 * MB,
        allocated_cost=8,
        cost_quality="reconciled",
    )
    partition = athena_estimation.partition_pruning_saving(query, DEFAULT_CONFIG)
    projection = athena_estimation.projection_saving(query, DEFAULT_CONFIG)
    for estimate in (partition, projection):
        assert estimate.baseline_cost == 8
        assert estimate.estimated_saving == 0
        assert estimate.projected_cost == 8
        assert estimate.baseline_quality == "allocated"
        assert estimate.saving_quality == "unavailable"
        assert estimate.baseline_bytes == 400 * MB
        assert estimate.projected_bytes is None


def test_history_persists_only_approved_athena_aggregates(tmp_path):
    now = datetime(2026, 7, 24, 15, tzinfo=timezone.utc)
    executions = [
        {
            "QueryExecutionId": f"ephemeral-{index}",
            "WorkGroup": "one",
            "Query": "SELECT * FROM db.sales",
            "StatementType": "DML",
            "Status": {
                "State": "SUCCEEDED",
                "SubmissionDateTime": now - timedelta(days=index + 1),
            },
            "Statistics": {"DataScannedInBytes": 300 * 1024**3},
        }
        for index in range(3)
    ]
    collected = collect_analysis(_Athena(executions), glue_client=_Glue(), now=now)
    account = Account(
        account_id="123",
        athena_queries=collected.queries,
        athena_actor_usage=collected.actors,
        athena_coverage=collected.coverage,
    )
    analyzed = analyze_account(account, scan_id="baseline-test")
    athena_opportunity = next(
        opportunity for opportunity in analyzed.opportunities
        if opportunity.asset_type == "athena_query"
    )
    with HistoryStore(tmp_path / "history.duckdb") as history:
        history.record_run(account, analyzed.opportunities, "detected", source="test")
        assert history._db.execute(
            "SELECT count(*) FROM athena_recommendation_baselines"
        ).fetchone()[0] == 0

        athena_opportunity.status = "accepted"
        history.record_run(account, analyzed.opportunities, "accepted", source="test")
        row = history._db.execute(
            """
            SELECT fingerprint, actor, billed_bytes, executions
            FROM athena_recommendation_baselines
            """
        ).fetchone()
        assert row[0] == account.athena_queries[0].structural_fingerprint
        assert row[1] == "desconhecido"
        assert row[2] > 0
        assert row[3] == 3
        missing = history.compare_athena_baseline(
            account.account_id,
            account.athena_queries[0].structural_fingerprint,
            None,
        )
        assert missing["estimated_saving"] is None
        assert missing["status"] == "not_observed"


class _RichGlue:
    def get_table(self, DatabaseName, Name):
        if Name == "wide_csv":
            return {
                "Table": {
                    "Parameters": {"classification": "csv"},
                    "PartitionKeys": [],
                    "StorageDescriptor": {
                        "Columns": [{"Name": f"c{i}", "Type": "string"} for i in range(60)],
                        "Location": "s3://bucket/wide/",
                        "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                        "SerdeInfo": {
                            "SerializationLibrary": "org.apache.hadoop.hive.serde2.OpenCSVSerde"
                        },
                    },
                }
            }
        return {
            "Table": {
                "Parameters": {
                    "classification": "parquet",
                    "projection.enabled": "false",
                    "parquet.compression": "UNCOMPRESSED",
                },
                "PartitionKeys": [{"Name": "dt", "Type": "date"}],
                "StorageDescriptor": {
                    "Columns": [{"Name": "id", "Type": "bigint"}],
                    "Location": "s3://bucket/partitioned/",
                    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    "Compressed": False,
                },
            }
        }

    def get_paginator(self, name):
        assert name == "get_partitions"
        return _Paginator(
            lambda **_: [{"Partitions": [{"Values": [str(i)]} for i in range(1000)]}]
        )


class _RichS3:
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(
            lambda Prefix, **_: [
                {
                    "Contents": [
                        {
                            "Key": f"{Prefix}part-{index}.csv",
                            "Size": 20 * MB,
                        }
                        for index in range(100)
                    ]
                }
            ]
        )


def test_requested_rules_are_grouped_by_pattern_and_actor():
    now = datetime(2026, 7, 24, 15, tzinfo=timezone.utc)
    sql = (
        "SELECT * FROM db.wide_csv w "
        "JOIN db.partitioned p ON p.id = w.c1"
    )
    executions = [
        {
            "QueryExecutionId": f"rich-{index}",
            "WorkGroup": "one",
            "Query": sql,
            "StatementType": "DML",
            "Status": {
                "State": "SUCCEEDED",
                "SubmissionDateTime": now - timedelta(days=index + 1),
            },
            "Statistics": {
                "DataScannedInBytes": 300 * 1024**3,
                "QueryPlanningTimeInMillis": 1500,
            },
        }
        for index in range(3)
    ]
    collected = collect_analysis(
        _Athena(executions),
        cloudtrail_client=_CloudTrail(executions),
        glue_client=_RichGlue(),
        s3_client=_RichS3(),
        now=now,
    )
    query = collected.queries[0]
    assert query.recurring is True
    assert query.actors == ["maria"]
    assert query.max_table_columns == 60
    assert query.full_scan_confirmed is True
    assert query.unpartitioned_tables == ["db.wide_csv"]
    assert query.row_format_uncompressed == ["db.wide_csv"]
    assert query.columnar_uncompressed == ["db.partitioned"]
    assert query.partition_projection_candidates == ["db.partitioned"]

    account = Account(
        account_id="123",
        athena_queries=collected.queries,
        athena_actor_usage=collected.actors,
        athena_coverage=collected.coverage,
    )
    rule_ids = {
        opportunity.rule_id
        for opportunity in athena_detector.detect(account, DEFAULT_CONFIG, "rules-test")
    }
    assert {
        "ATHENA-SELECT-STAR-WIDE",
        "ATHENA-FULL-TABLE-SCAN",
        "ATHENA-TABLE-NOT-PARTITIONED",
        "ATHENA-UNCOMPRESSED-ROW-FORMAT",
        "ATHENA-COLUMNAR-COMPRESSION",
        "ATHENA-PARTITION-PROJECTION",
    } <= rule_ids

    analyzed = analyze_account(account, scan_id="grouping-test")
    athena_opportunities = [
        opportunity
        for opportunity in analyzed.opportunities
        if opportunity.asset_type == "athena_query"
    ]
    assert len(athena_opportunities) == 1
    assert account.athena_actor_usage[0].opportunity_refs == [
        athena_opportunities[0].opportunity_id
    ]
