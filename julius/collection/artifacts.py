"""Carregamento seguro de artefatos de código previamente coletados em read-only.

Um `kind` por tipo de artefato, e o carregador filtra por ele: script Glue,
script SageMaker e definição ASL vêm do mesmo bundle e são analisados por
regras diferentes. O que não muda entre eles é a verificação — o manifesto
precisa declarar a mesma conta e `read_only`, o arquivo precisa estar dentro do
bundle, e o conteúdo precisa bater com o sha256 registrado na coleta.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from julius.collection.models import Account, CollectionHealth


@dataclass(frozen=True)
class CodeArtifact:
    asset_name: str
    source: str
    content: str
    sha256: str
    truncated: bool = False
    path: str = ""
    #: `glue_script`, `sagemaker_script` ou `stepfunctions_asl`. Vazio só em
    #: artefato construído à mão em teste anterior ao campo.
    kind: str = ""


#: O nome anterior, quando só existiam scripts Glue.
GlueCodeArtifact = CodeArtifact


def load_code_artifacts(
    manifest_path: str | Path,
    account_id: str,
    *,
    kinds: tuple[str, ...] = ("glue_script",),
) -> list[CodeArtifact]:
    """Valida o manifesto e lê somente os artefatos dos tipos pedidos."""
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
    artifacts: list[CodeArtifact] = []
    for item in raw["artifacts"]:
        if not isinstance(item, dict) or item.get("kind") not in kinds:
            continue
        filename = item.get("file")
        if not isinstance(filename, str):
            raise ValueError("entrada de artefato inválida no manifesto")
        artifact_path = (root / filename).resolve()
        if root not in artifact_path.parents or not artifact_path.is_file():
            raise ValueError(f"arquivo técnico ausente ou fora do bundle: {filename}")
        content = artifact_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        expected = str(item.get("sha256") or "")
        if expected and digest != expected:
            raise ValueError(f"hash divergente no artefato técnico: {filename}")
        artifacts.append(
            CodeArtifact(
                asset_name=str(item.get("asset_name") or ""),
                source=str(item.get("source") or ""),
                content=content,
                sha256=digest,
                truncated=bool(item.get("truncated")),
                path=artifact_path.as_posix(),
                kind=str(item.get("kind") or ""),
            )
        )
    return artifacts


def load_glue_artifacts(
    manifest_path: str | Path,
    account_id: str,
) -> list[CodeArtifact]:
    """Compatibilidade: o caminho que existia quando só havia script Glue."""
    return load_code_artifacts(manifest_path, account_id, kinds=("glue_script",))


def summarize_glue_artifact_health(
    manifest_path: str | Path,
    account: Account,
    artifacts: list[CodeArtifact],
) -> CollectionHealth:
    return summarize_artifact_health(
        manifest_path,
        artifacts,
        kind="glue_script",
        source="Glue Scripts",
        expected_assets={job.name for job in account.glue_jobs if job.script_location},
        impact="análise de código não cobre todos os jobs com ScriptLocation",
        next_action="revisar erros do manifesto e permissões s3:GetObject",
    )


def summarize_artifact_health(
    manifest_path: str | Path,
    artifacts: list[CodeArtifact],
    *,
    kind: str,
    source: str,
    expected_assets: set[str],
    impact: str,
    next_action: str,
) -> CollectionHealth:
    """Cobertura de um tipo de artefato: quantos ativos que têm código foram lidos.

    `expected_assets` vem de quem sabe o que deveria existir — job com
    `script_location`, training job com `code_location`, máquina cuja definição
    foi lida. Sem esse denominador, um bundle com um arquivo pareceria completo.
    """
    path = Path(manifest_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    collected_assets = {
        artifact.asset_name
        for artifact in artifacts
        if artifact.asset_name in expected_assets
    }
    errors = [
        item
        for item in raw.get("errors", [])
        if isinstance(item, dict) and item.get("kind") == kind
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
        source=source,
        status=status,
        affects_status=expected > 0,
        started_at=updated,
        completed_at=updated,
        collected=collected,
        expected=expected,
        coverage=round(coverage, 4),
        data_through=updated[:10],
        error_category=category,
        impact=impact if status != "ok" else "",
        next_action=next_action if status != "ok" else "",
    )
