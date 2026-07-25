"""Scanner determinístico de custo para scripts AWS Glue."""

from __future__ import annotations

import json

from julius.aws.technical_artifacts import (
    ArtifactBundle,
    TechnicalArtifact,
    write_artifact_bundle,
)
from julius.code_analysis import GlueCodeArtifact, load_glue_artifacts, scan_glue_script
from julius.config import DATASET_SCHEMA_VERSION, DEFAULT_CONFIG
from julius.inventory.model import Account, GlueJob
from julius.opportunities.detectors import glue_code
from julius.pipeline import analyze
from julius.report import renderer


SPARK_SCRIPT = """
import boto3
from awsglue.context import GlueContext
from pyspark.sql.functions import udf

client = boto3.client("dynamodb")
normalize = udf(lambda value: value)
job.init("job-code", {})
spark.conf.set("spark.sql.shuffle.partitions", 2000)
source = glue_context.create_dynamic_frame.from_catalog(
    database="db", table_name="events"
)
raw = spark.read.parquet("s3://bucket/raw/").filter("event_date >= '2026-01-01'")
jdbc = spark.read.format("jdbc").option("url", "jdbc:test").load()
df = source.toDF()
rows = df.repartition(1).collect()
cached = df.cache()
df.count()
df.show()
df.count()
for column in ["a", "b"]:
    df = df.withColumn(column, df[column])
for day in ["2026-01-01"]:
    df.write.mode("overwrite").parquet("s3://bucket/out/" + day)

def lookup(row):
    return client.get_item(Key={"id": row.id})

mapped = df.rdd.map(lookup)
joined = df.join(df, "id")
try:
    joined.count()
except Exception:
    pass
"""


def _artifact(content: str, *, truncated: bool = False) -> GlueCodeArtifact:
    technical = TechnicalArtifact(
        kind="glue_script",
        asset_name="job-code",
        source="s3://scripts/job-code.py",
        content=content,
        truncated=truncated,
    )
    return GlueCodeArtifact(
        asset_name=technical.asset_name,
        source=technical.source,
        content=technical.content,
        sha256=technical.sha256,
        truncated=technical.truncated,
    )


def test_scanner_finds_glue_cost_patterns_without_returning_source():
    findings = {item.rule_id: item for item in scan_glue_script(SPARK_SCRIPT)}
    assert {
        "GLUE-CODE-PUSHDOWN",
        "GLUE-CODE-S3-FULL-SCAN",
        "GLUE-CODE-JDBC-SINGLE-READER",
        "GLUE-CODE-SINGLE-PARTITION",
        "GLUE-CODE-DRIVER-MATERIALIZATION",
        "GLUE-CODE-PYTHON-UDF",
        "GLUE-CODE-REPEATED-ACTIONS",
        "GLUE-CODE-CACHE-LIFECYCLE",
        "GLUE-CODE-ITERATIVE-PLAN",
        "GLUE-CODE-ROW-EXTERNAL-IO",
        "GLUE-CODE-FULL-OVERWRITE",
        "GLUE-CODE-SMALL-FILES",
        "GLUE-CODE-SHUFFLE",
        "GLUE-CODE-SHUFFLE-PARTITIONS",
        "GLUE-CODE-SWALLOWED-EXCEPTION",
        "GLUE-CODE-BOOKMARK-CONTEXT",
        "GLUE-CODE-BOOKMARK-COMMIT",
    } <= set(findings)
    assert all(item.lines for item in findings.values())
    assert all("get_item" not in item.signal for item in findings.values())


def test_python_shell_candidate_requires_complete_non_spark_script():
    job = GlueJob(
        name="job-code",
        command_type="glueetl",
        number_of_workers=10,
        worker_type="G.1X",
        runs_in_window=30,
        avg_execution_sec=600,
        observed_runs=20,
        coverage_days=30,
        avg_worker_utilization=0.05,
    )
    account = Account(account_id="123456789012", glue_jobs=[job])
    script = "import boto3\nclient = boto3.client('s3')\nclient.list_buckets()\n"
    assert {
        item.rule_id for item in scan_glue_script(script)
    } == {"GLUE-SPARK-TO-PYTHON-SHELL"}
    found = glue_code.detect(
        account, [_artifact(script)], DEFAULT_CONFIG, "scan-code"
    )
    candidate = next(
        item for item in found if item.rule_id == "GLUE-SPARK-TO-PYTHON-SHELL"
    )
    assert candidate.blocked is True
    assert candidate.actionable is False
    assert candidate.estimation is not None
    assert candidate.estimation.projected_cost < candidate.estimation.baseline_cost
    assert candidate.evidence_refs[0]["sha256"]

    job.job_bookmark = True
    assert not any(
        item.rule_id == "GLUE-SPARK-TO-PYTHON-SHELL"
        for item in glue_code.detect(
            account, [_artifact(script)], DEFAULT_CONFIG, "scan-bookmark"
        )
    )
    job.job_bookmark = False
    assert not any(
        item.rule_id == "GLUE-SPARK-TO-PYTHON-SHELL"
        for item in glue_code.detect(
            account,
            [_artifact(script, truncated=True)],
            DEFAULT_CONFIG,
            "scan-truncated",
        )
    )


def test_manifest_hash_is_verified_and_pipeline_consumes_code_findings(tmp_path):
    dataset = tmp_path / "account.json"
    dataset.write_text(
        json.dumps(
            {
                "dataset_schema_version": DATASET_SCHEMA_VERSION,
                "account": "consumer-code",
                "region": "sa-east-1",
                "period": "jul/2026",
                "lookback_days": 30,
                "cost_explorer": {"services": []},
                "glue_jobs": [
                    {
                        "name": "job-code",
                        "command_type": "glueetl",
                        "worker_type": "G.1X",
                        "number_of_workers": 10,
                        "runs_in_window": 30,
                        "avg_execution_sec": 600,
                        "timeout_min": 60,
                        "observed_runs": 20,
                        "coverage_days": 30,
                        "avg_worker_utilization": 0.05,
                    }
                ],
                "governance": {
                    "producer_candidates": [],
                    "previous_results": [],
                },
            }
        ),
        encoding="utf-8",
    )
    bundle = ArtifactBundle(
        account_id="consumer-code",
        caller_arn="offline-test",
        artifacts=[
            TechnicalArtifact(
                kind="glue_script",
                asset_name="job-code",
                source="s3://scripts/job-code.py",
                content="import boto3\nboto3.client('s3').list_buckets()\n",
            )
        ],
    )
    manifest = write_artifact_bundle(bundle, tmp_path / "artifacts")

    loaded = load_glue_artifacts(manifest, "consumer-code")
    assert loaded[0].asset_name == "job-code"
    analysis = analyze(dataset, artifacts_manifest=manifest)
    assert any(
        item.rule_id == "GLUE-SPARK-TO-PYTHON-SHELL"
        for item in analysis.opportunities
    )
    report_payload = json.loads(
        renderer.render_json(analysis.vm, analysis.opportunities)
    )
    code_rows = [
        item
        for item in report_payload["opportunities"]
        if item["rule_id"] == "GLUE-SPARK-TO-PYTHON-SHELL"
    ]
    assert code_rows[0]["blocked"] is True
    assert "list_buckets" not in json.dumps(report_payload)
    health = {
        item["source"]: item
        for item in report_payload["collection_health"]["sources"]
    }
    assert health["Glue Scripts"]["status"] == "ok"
    assert health["Glue Scripts"]["coverage"] == "100%"
    assert report_payload["collection_health"]["status"] == "partial"

    artifact_path = next((tmp_path / "artifacts").glob("*glue_script*"))
    artifact_path.write_text("modified", encoding="utf-8")
    try:
        load_glue_artifacts(manifest, "consumer-code")
    except ValueError as exc:
        assert "hash divergente" in str(exc)
    else:
        raise AssertionError("manifesto adulterado deveria ser rejeitado")
