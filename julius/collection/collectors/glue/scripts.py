"""Coleta read-only de artefatos técnicos para análise contextual pelo Devin."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from julius.agent.context import redact_secrets
from julius.collection.models import Account


class IdentityMismatchError(RuntimeError):
    """A credencial AWS ativa não corresponde à conta que será analisada."""


@dataclass
class TechnicalArtifact:
    kind: str
    asset_name: str
    source: str
    content: str
    truncated: bool = False
    sha256: str = ""

    def __post_init__(self) -> None:
        self.content = redact_secrets(self.content)
        self.sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class ArtifactBundle:
    account_id: str
    caller_arn: str
    artifacts: list[TechnicalArtifact] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


def collect_technical_artifacts(
    session,
    account: Account,
    *,
    max_bytes: int = 256_000,
) -> ArtifactBundle:
    """Lê apenas definições e código; não executa nem altera recursos."""
    if max_bytes < 1 or max_bytes > 1_000_000:
        raise ValueError("max_bytes deve estar entre 1 e 1000000")
    if not (account.account_id.isdigit() and len(account.account_id) == 12):
        raise IdentityMismatchError(
            "coleta técnica ao vivo exige account_id AWS numérico de 12 dígitos"
        )
    identity = session.client("sts").get_caller_identity()
    actual_account = str(identity.get("Account") or "")
    if actual_account != account.account_id:
        raise IdentityMismatchError(
            f"identidade AWS {actual_account or 'desconhecida'} não corresponde "
            f"à conta esperada {account.account_id}"
        )
    bundle = ArtifactBundle(
        account_id=account.account_id,
        caller_arn=str(identity.get("Arn") or ""),
    )
    s3 = session.client("s3")
    for job in account.glue_jobs:
        if not job.script_location:
            continue
        try:
            content, truncated = _read_s3_text(
                s3, job.script_location, max_bytes=max_bytes
            )
            bundle.artifacts.append(
                TechnicalArtifact(
                    kind="glue_script",
                    asset_name=job.name,
                    source=job.script_location,
                    content=content,
                    truncated=truncated,
                )
            )
        except Exception as exc:
            bundle.errors.append(
                {
                    "kind": "glue_script",
                    "asset_name": job.name,
                    "error": type(exc).__name__,
                }
            )

    for query in account.athena_queries:
        if query.statement:
            text = query.statement
            encoded = text.encode("utf-8")
            truncated = len(encoded) > max_bytes
            if truncated:
                text = encoded[:max_bytes].decode("utf-8", errors="ignore")
            bundle.artifacts.append(
                TechnicalArtifact(
                    kind="athena_sql",
                    asset_name=query.query_id,
                    source=f"athena://{query.workgroup}/{query.query_id}",
                    content=text,
                    truncated=truncated,
                )
            )

    _collect_state_machine_definitions(session, account, bundle, max_bytes)
    return bundle


def write_artifact_bundle(
    bundle: ArtifactBundle,
    output_dir: str | Path,
) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest_items = []
    for index, artifact in enumerate(bundle.artifacts, start=1):
        suffix = ".sql" if artifact.kind == "athena_sql" else ".json"
        if artifact.kind == "glue_script":
            suffix = Path(urlparse(artifact.source).path).suffix or ".txt"
        name = f"{index:03d}-{artifact.kind}-{_safe_name(artifact.asset_name)}{suffix}"
        path = output / name
        path.write_text(artifact.content, encoding="utf-8")
        item = asdict(artifact)
        item.pop("content")
        item["file"] = name
        manifest_items.append(item)
    manifest = {
        "schema_version": "1.0",
        "account_id": bundle.account_id,
        "caller_arn": bundle.caller_arn,
        "read_only": True,
        "artifacts": manifest_items,
        "errors": bundle.errors,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_path


def _read_s3_text(client, uri: str, *, max_bytes: int) -> tuple[str, bool]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("ScriptLocation não é um URI S3 válido")
    response = client.get_object(
        Bucket=parsed.netloc,
        Key=parsed.path.lstrip("/"),
        Range=f"bytes=0-{max_bytes}",
    )
    data = response["Body"].read(max_bytes + 1)
    truncated = len(data) > max_bytes
    return data[:max_bytes].decode("utf-8", errors="replace"), truncated


def _collect_state_machine_definitions(
    session,
    account: Account,
    bundle: ArtifactBundle,
    max_bytes: int,
) -> None:
    wanted = {machine.name for machine in account.state_machines}
    if not wanted:
        return
    client = session.client("stepfunctions")
    try:
        paginator = client.get_paginator("list_state_machines")
        summaries = [
            item
            for page in paginator.paginate()
            for item in page.get("stateMachines", [])
            if item.get("name") in wanted
        ]
    except Exception as exc:
        bundle.errors.append(
            {
                "kind": "stepfunctions_asl",
                "asset_name": "*",
                "error": type(exc).__name__,
            }
        )
        return
    found = set()
    for summary in summaries:
        name = str(summary["name"])
        found.add(name)
        try:
            detail = client.describe_state_machine(
                stateMachineArn=summary["stateMachineArn"]
            )
            text = str(detail.get("definition") or "{}")
            encoded = text.encode("utf-8")
            truncated = len(encoded) > max_bytes
            if truncated:
                text = encoded[:max_bytes].decode("utf-8", errors="ignore")
            bundle.artifacts.append(
                TechnicalArtifact(
                    kind="stepfunctions_asl",
                    asset_name=name,
                    source=str(summary["stateMachineArn"]),
                    content=text,
                    truncated=truncated,
                )
            )
        except Exception as exc:
            bundle.errors.append(
                {
                    "kind": "stepfunctions_asl",
                    "asset_name": name,
                    "error": type(exc).__name__,
                }
            )
    for missing in sorted(wanted - found):
        bundle.errors.append(
            {
                "kind": "stepfunctions_asl",
                "asset_name": missing,
                "error": "NotFound",
            }
        )


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:100] or "asset"
