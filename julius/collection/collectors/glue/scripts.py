"""Coleta read-only de artefatos técnicos para análise contextual pelo Devin."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from julius.collection.models import Account
from julius.collection.redaction import redact_secrets


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

    for sagemaker_job in account.sagemaker_jobs:
        if not sagemaker_job.code_location:
            continue
        try:
            content, truncated = _read_code(
                s3,
                sagemaker_job.code_location,
                entry_point=sagemaker_job.code_entry_point,
                max_bytes=max_bytes,
            )
            bundle.artifacts.append(
                TechnicalArtifact(
                    kind="sagemaker_script",
                    asset_name=sagemaker_job.name,
                    source=sagemaker_job.code_location,
                    content=content,
                    truncated=truncated,
                )
            )
        except Exception as exc:
            bundle.errors.append(
                {
                    "kind": "sagemaker_script",
                    "asset_name": sagemaker_job.name,
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
        elif artifact.kind == "sagemaker_script":
            # A extensão do `source` seria `.gz`, e o conteúdo é o `.py` de
            # dentro do pacote — nomear pelo invólucro esconderia o que é.
            suffix = ".py"
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
    data, truncated = _read_s3_bytes(client, uri, max_bytes=max_bytes)
    return data.decode("utf-8", errors="replace"), truncated


def _read_s3_bytes(client, uri: str, *, max_bytes: int) -> tuple[bytes, bool]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("localização de código não é um URI S3 válido")
    response = client.get_object(
        Bucket=parsed.netloc,
        Key=parsed.path.lstrip("/"),
        Range=f"bytes=0-{max_bytes}",
    )
    data = response["Body"].read(max_bytes + 1)
    return data[:max_bytes], len(data) > max_bytes


def _read_code(client, uri: str, *, entry_point: str, max_bytes: int) -> tuple[str, bool]:
    """Lê o script, descompactando o `sourcedir.tar.gz` quando for o caso."""
    if not uri.endswith(".tar.gz"):
        return _read_s3_text(client, uri, max_bytes=max_bytes)
    data, truncated_download = _read_s3_bytes(client, uri, max_bytes=max_bytes)
    if truncated_download:
        # Tar truncado não descomprime, e adivinhar o resto produziria um script
        # pela metade que o scanner leria como se fosse o arquivo inteiro.
        raise ValueError("sourcedir maior que o limite do bundle")
    return _extract_entry_point(data, entry_point, max_bytes=max_bytes)


#: Teto de membros inspecionados dentro de um tar. Um `sourcedir` de projeto
#: real tem dezenas de arquivos; milhares indicam outra coisa.
_MAX_TAR_MEMBERS = 500


def _extract_entry_point(data: bytes, entry_point: str, *, max_bytes: int) -> tuple[str, bool]:
    """Extrai só o arquivo de entrada, tratando o tar como entrada hostil.

    O pacote vem de um bucket que a coleta não controla, então nada aqui confia
    no que ele declara. O nome do membro é validado antes de qualquer leitura —
    caminho absoluto e `..` saem do diretório de destino em qualquer extração
    ingênua — e o tamanho é medido enquanto lê, não lido do cabeçalho: um
    membro pode declarar 1 KB e descomprimir gigabytes.
    """
    if not entry_point:
        raise ValueError("pacote tar sem entry point declarado")
    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tar:
        for indice, member in enumerate(tar):
            if indice >= _MAX_TAR_MEMBERS:
                raise ValueError("sourcedir com membros demais")
            nome = member.name.lstrip("./")
            if not member.isfile() or nome != entry_point:
                continue
            if nome.startswith("/") or ".." in Path(nome).parts:
                raise ValueError(f"membro com caminho inseguro: {member.name}")
            if not nome.endswith(".py"):
                raise ValueError(f"entry point não é Python: {member.name}")
            handle = tar.extractfile(member)
            if handle is None:
                raise ValueError(f"membro ilegível: {member.name}")
            conteudo = handle.read(max_bytes + 1)
            if len(conteudo) > max_bytes:
                raise ValueError(f"entry point maior que o limite: {member.name}")
            return conteudo.decode("utf-8", errors="replace"), False
    raise ValueError(f"entry point ausente no pacote: {entry_point}")


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
