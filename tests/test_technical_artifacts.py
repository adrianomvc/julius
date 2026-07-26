"""Coleta técnica read-only para análise pelo Devin."""

from __future__ import annotations

import io
import json

import pytest

from julius.analysis import prepare_agent_workspace
from julius.collection.collectors.glue.scripts import (
    ArtifactBundle,
    IdentityMismatchError,
    TechnicalArtifact,
    collect_technical_artifacts,
    write_artifact_bundle,
)
from julius.collection.models import Account, AthenaQuery, GlueJob, StateMachine
from julius.pipeline import analyze


class FakePaginator:
    def paginate(self):
        return [
            {
                "stateMachines": [
                    {
                        "name": "orquestra",
                        "stateMachineArn": (
                            "arn:aws:states:sa-east-1:123456789012:"
                            "stateMachine:orquestra"
                        ),
                    }
                ]
            }
        ]


class FakeSts:
    def __init__(self, account="123456789012"):
        self.account = account

    def get_caller_identity(self):
        return {
            "Account": self.account,
            "Arn": f"arn:aws:sts::{self.account}:assumed-role/JuliusReadOnly/devin",
        }


class FakeS3:
    def __init__(self):
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Body": io.BytesIO(
                b"password=should-not-leak\nprint('safe glue script')\n"
            )
        }


class FakeStepFunctions:
    def get_paginator(self, name):
        assert name == "list_state_machines"
        return FakePaginator()

    def describe_state_machine(self, **kwargs):
        return {
            "definition": json.dumps(
                {
                    "StartAt": "Run",
                    "States": {
                        "Run": {
                            "Type": "Task",
                            "Resource": "arn:aws:states:::glue:startJobRun.sync",
                            "End": True,
                        }
                    },
                }
            )
        }


class FakeSession:
    def __init__(self, account="123456789012"):
        self.sts = FakeSts(account)
        self.s3 = FakeS3()
        self.stepfunctions = FakeStepFunctions()

    def client(self, name):
        return {
            "sts": self.sts,
            "s3": self.s3,
            "stepfunctions": self.stepfunctions,
        }[name]


def _account() -> Account:
    return Account(
        account_id="123456789012",
        region="sa-east-1",
        glue_jobs=[
            GlueJob(
                name="transforma",
                script_location="s3://scripts-bucket/jobs/transforma.py",
            )
        ],
        athena_queries=[
            AthenaQuery(
                query_id="query-1",
                workgroup="julius",
                statement="SELECT * FROM tabela WHERE token=abc123",
            )
        ],
        state_machines=[StateMachine(name="orquestra")],
    )


def test_collects_glue_sql_and_asl_with_read_only_calls(tmp_path):
    session = FakeSession()
    bundle = collect_technical_artifacts(session, _account())
    manifest_path = write_artifact_bundle(bundle, tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert {item.kind for item in bundle.artifacts} == {
        "glue_script",
        "athena_sql",
        "stepfunctions_asl",
    }
    assert "should-not-leak" not in bundle.artifacts[0].content
    assert "abc123" not in next(
        item.content for item in bundle.artifacts if item.kind == "athena_sql"
    )
    assert session.s3.calls[0]["Range"].startswith("bytes=0-")
    assert manifest["read_only"] is True
    assert all("content" not in item for item in manifest["artifacts"])


def test_blocks_identity_mismatch_before_artifact_clients_are_used():
    with pytest.raises(IdentityMismatchError, match="não corresponde"):
        collect_technical_artifacts(FakeSession("999999999999"), _account())


def test_prepare_references_only_verified_manifest_files(tmp_path):
    analysis = analyze("data/sample/consumer-avi.json")
    bundle = ArtifactBundle(
        account_id=analysis.account.account_id,
        caller_arn="offline-test",
        artifacts=[
            TechnicalArtifact(
                kind="athena_sql",
                asset_name="query-offline",
                source="athena://offline/query",
                content="SELECT 1",
            )
        ],
    )
    manifest = write_artifact_bundle(bundle, tmp_path / "artifacts")
    context, _ = prepare_agent_workspace(
        analysis,
        tmp_path / "agent",
        top=2,
        artifacts_manifest=manifest,
    )

    assert len(context.technical_artifacts) == 1
    reference = context.technical_artifacts[0]
    assert "content" not in reference
    assert reference["path"].endswith(".sql")
