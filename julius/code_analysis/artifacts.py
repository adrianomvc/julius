"""Carregamento seguro de scripts Glue previamente coletados em modo read-only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from julius.inventory.model import Account, CollectionHealth


@dataclass(frozen=True)
class GlueCodeArtifact:
    asset_name: str
    source: str
    content: str
    sha256: str
    truncated: bool = False
    path: str = ""


def load_glue_artifacts(
    manifest_path: str | Path,
    account_id: str,
) -> list[GlueCodeArtifact]:
    """Valida o manifesto e lê somente os scripts Glue referenciados por ele."""
    path = Path(manifest_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != "1.0"
        or str(raw.get("account_id")) != account_id
        or raw.get("read_only") is not True
        or not isinstance(raw.get("artifacts"), list)
    ):
        raise ValueError("manifesto de artefatos não pertence à conta ou não é read-only")

    root = path.parent.resolve()
    artifacts: list[GlueCodeArtifact] = []
    for item in raw["artifacts"]:
        if not isinstance(item, dict) or item.get("kind") != "glue_script":
            continue
        filename = item.get("file")
        if not isinstance(filename, str):
            raise ValueError("entrada de script Glue inválida no manifesto")
        artifact_path = (root / filename).resolve()
        if root not in artifact_path.parents or not artifact_path.is_file():
            raise ValueError(f"arquivo técnico ausente ou fora do bundle: {filename}")
        content = artifact_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        expected = str(item.get("sha256") or "")
        if expected and digest != expected:
            raise ValueError(f"hash divergente no artefato técnico: {filename}")
        artifacts.append(
            GlueCodeArtifact(
                asset_name=str(item.get("asset_name") or ""),
                source=str(item.get("source") or ""),
                content=content,
                sha256=digest,
                truncated=bool(item.get("truncated")),
                path=artifact_path.as_posix(),
            )
        )
    return artifacts


def summarize_glue_artifact_health(
    manifest_path: str | Path,
    account: Account,
    artifacts: list[GlueCodeArtifact],
) -> CollectionHealth:
    path = Path(manifest_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    expected_assets = {
        job.name for job in account.glue_jobs if job.script_location
    }
    collected_assets = {
        artifact.asset_name
        for artifact in artifacts
        if artifact.asset_name in expected_assets
    }
    errors = [
        item
        for item in raw.get("errors", [])
        if isinstance(item, dict) and item.get("kind") == "glue_script"
    ]
    expected = len(expected_assets)
    collected = len(collected_assets)
    coverage = collected / expected if expected else 1.0
    if expected and collected == 0:
        status = "unavailable"
    elif collected < expected or errors:
        status = "partial"
    else:
        status = "ok"
    updated = datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    category = ""
    if errors:
        category = "artifact_errors"
    elif status == "unavailable":
        category = "no_data"
    elif status == "partial":
        category = "partial_data"
    return CollectionHealth(
        source="Glue Scripts",
        status=status,
        affects_status=expected > 0,
        started_at=updated,
        completed_at=updated,
        collected=collected,
        expected=expected,
        coverage=round(coverage, 4),
        data_through=updated[:10],
        error_category=category,
        impact=(
            "análise de código não cobre todos os jobs com ScriptLocation"
            if status != "ok"
            else ""
        ),
        next_action=(
            "revisar erros do manifesto e permissões s3:GetObject"
            if status != "ok"
            else ""
        ),
    )
