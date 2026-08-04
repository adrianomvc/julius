"""Worker contextual por domínio, isolado da coleta AWS.

O provider recebe somente um checkpoint JSON imutável. A resposta aceita não
contém economia, prioridade, confiança, dificuldade ou ciclo de vida: esses
campos pertencem ao motor determinístico e não podem ser sobrescritos aqui.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from julius.state import RunStore, RunTask, file_sha256

DOMAIN_RESULT_SCHEMA_VERSION = "1.0"
MERGED_RESULT_SCHEMA_VERSION = "1.0"
_RESULT_FIELDS = {
    "schema_version",
    "account_id",
    "scan_id",
    "domain",
    "context_hash",
    "provider",
    "status",
    "summary",
    "observations",
    "suspected_injections",
}
_OBSERVATION_FIELDS = {
    "subject_id",
    "diagnosis",
    "implementation_steps",
    "risks",
    "evidence_refs",
}


class DomainResultError(ValueError):
    """Envelope contextual inválido ou incompatível com o checkpoint."""


class ObsoleteTaskError(DomainResultError):
    """Job de um hash anterior; deve ser arquivado, nunca reexecutado."""


class ResultNotReadyError(RuntimeError):
    """Provider assíncrono ainda não publicou a resposta esperada."""


class ProviderExecutionError(RuntimeError):
    """Falha isolada do provider, distinta de checkpoint ou resposta inválida."""


class DomainProvider(Protocol):
    """Provider sem boto3; implementações podem usar processo ou API próprios."""

    name: str

    def analyze(
        self, checkpoint: dict[str, Any], *, task: RunTask
    ) -> dict[str, Any]: ...


class InboxDomainProvider:
    """Provider auditável: consome resposta publicada em caminho determinístico."""

    def __init__(self, inbox: str | Path, *, name: str = "file") -> None:
        self.inbox = Path(inbox)
        self.name = name

    def expected_path(self, task: RunTask) -> Path:
        return (
            self.inbox
            / task.account_id
            / task.scan_id
            / f"{task.domain}-{task.context_hash[:16]}.json"
        )

    def analyze(
        self, checkpoint: dict[str, Any], *, task: RunTask
    ) -> dict[str, Any]:
        del checkpoint  # o arquivo contém o envelope e ancora o hash do task
        path = self.expected_path(task)
        if not path.is_file():
            raise ResultNotReadyError(str(path))
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainResultError("arquivo contextual não é JSON válido") from exc


@dataclass(frozen=True)
class DomainObservation:
    subject_id: str
    diagnosis: str
    implementation_steps: tuple[str, ...]
    risks: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class DomainResult:
    schema_version: str
    account_id: str
    scan_id: str
    domain: str
    context_hash: str
    provider: str
    status: str
    summary: str
    observations: tuple[DomainObservation, ...]
    suspected_injections: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["observations"] = [asdict(item) for item in self.observations]
        payload["suspected_injections"] = list(self.suspected_injections)
        return payload


@dataclass(frozen=True)
class WorkerOutcome:
    task_id: str
    status: str
    result_path: str = ""
    error_category: str = ""


def process_next(
    store: RunStore,
    provider: DomainProvider,
    result_root: str | Path,
) -> WorkerOutcome | None:
    """Reserva, executa, valida e persiste um job; nunca recebe sessão AWS."""
    task = store.claim_next()
    if task is None:
        return None
    try:
        checkpoint = store.verified_checkpoint(
            task.account_id, task.scan_id, task.domain
        )
        _require_current_task(task, checkpoint.payload_hash, checkpoint.payload_path)
        payload = json.loads(Path(checkpoint.payload_path).read_text(encoding="utf-8"))
        _validate_checkpoint_envelope(payload, task)
        try:
            raw = provider.analyze(payload, task=task)
        except (DomainResultError, ResultNotReadyError):
            raise
        except Exception as exc:
            raise ProviderExecutionError from exc
        result = validate_domain_result(raw, task=task, provider=provider.name)
        encoded = json.dumps(
            result.as_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        path = (
            Path(result_root)
            / task.account_id
            / task.scan_id
            / f"{task.domain}-{digest[:16]}.json"
        )
        _write_immutable(path, encoded)
        store.record_contextual_result(
            task.task_id,
            provider=result.provider,
            status=result.status,
            result_path=str(path),
            result_hash=digest,
        )
        store.complete_ai(task.task_id)
        return WorkerOutcome(task.task_id, "completed", str(path))
    except ObsoleteTaskError:
        store.transition_task(task.task_id, "superseded")
        return WorkerOutcome(task.task_id, "superseded")
    except ResultNotReadyError as exc:
        store.transition_task(
            task.task_id, "pending", error_category="result_not_ready"
        )
        return WorkerOutcome(
            task.task_id,
            "pending",
            result_path=str(exc),
            error_category="result_not_ready",
        )
    except DomainResultError:
        store.transition_task(task.task_id, "failed", error_category="invalid_result")
        return WorkerOutcome(task.task_id, "failed", error_category="invalid_result")
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        store.transition_task(task.task_id, "failed", error_category="invalid_checkpoint")
        return WorkerOutcome(task.task_id, "failed", error_category="invalid_checkpoint")
    except ProviderExecutionError:
        store.transition_task(task.task_id, "failed", error_category="provider_error")
        return WorkerOutcome(task.task_id, "failed", error_category="provider_error")
    except Exception:
        store.transition_task(task.task_id, "failed", error_category="worker_error")
        return WorkerOutcome(task.task_id, "failed", error_category="worker_error")


def validate_domain_result(
    payload: Any,
    *,
    task: RunTask,
    provider: str,
) -> DomainResult:
    if not isinstance(payload, dict) or set(payload) != _RESULT_FIELDS:
        raise DomainResultError("campos do resultado contextual inválidos")
    expected = {
        "schema_version": DOMAIN_RESULT_SCHEMA_VERSION,
        "account_id": task.account_id,
        "scan_id": task.scan_id,
        "domain": task.domain,
        "context_hash": task.context_hash,
        "provider": provider,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise DomainResultError(f"{field} diverge do job reservado")
    status = _enum(payload.get("status"), {"enriched", "needs_evidence"}, "status")
    summary = _text(payload.get("summary"), "summary", maximum=8_000)
    observations_raw = payload.get("observations")
    if not isinstance(observations_raw, list) or len(observations_raw) > 100:
        raise DomainResultError("observations deve ser lista limitada")
    observations = tuple(_observation(item) for item in observations_raw)
    injections = tuple(
        _string_list(
            payload.get("suspected_injections"),
            "suspected_injections",
            maximum_items=100,
            maximum_text=2_000,
        )
    )
    return DomainResult(
        schema_version=DOMAIN_RESULT_SCHEMA_VERSION,
        account_id=task.account_id,
        scan_id=task.scan_id,
        domain=task.domain,
        context_hash=task.context_hash,
        provider=provider,
        status=status,
        summary=summary,
        observations=observations,
        suspected_injections=injections,
    )


def merge_results(
    store: RunStore,
    account_id: str,
    scan_id: str,
    output: str | Path,
) -> Path:
    """Compõe resultados verificados sem tocar o dataset determinístico."""
    merged: list[dict[str, Any]] = []
    for entry in store.contextual_results(account_id, scan_id):
        checkpoint = store.verified_checkpoint(account_id, scan_id, entry.domain)
        if checkpoint.payload_hash != entry.context_hash:
            raise DomainResultError(f"resultado obsoleto para domínio {entry.domain}")
        path = Path(entry.result_path)
        if not path.is_file() or file_sha256(path) != entry.result_hash:
            raise DomainResultError(f"resultado corrompido para domínio {entry.domain}")
        task = store.task_for_context(
            account_id, scan_id, entry.domain, entry.context_hash
        )
        if task.status != "completed":
            raise DomainResultError(f"resultado ainda não concluído: {entry.domain}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = validate_domain_result(payload, task=task, provider=entry.provider)
        merged.append(result.as_payload())
    statuses = {item["status"] for item in merged}
    document = {
        "schema_version": MERGED_RESULT_SCHEMA_VERSION,
        "account_id": account_id,
        "scan_id": scan_id,
        "status": (
            "needs_evidence"
            if not merged or "needs_evidence" in statuses
            else "enriched"
        ),
        "results": merged,
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path = Path(output)
    _write_immutable(path, encoded)
    return path


def _observation(payload: Any) -> DomainObservation:
    if not isinstance(payload, dict) or set(payload) != _OBSERVATION_FIELDS:
        raise DomainResultError("campos de observation inválidos")
    return DomainObservation(
        subject_id=_text(payload.get("subject_id"), "subject_id", maximum=512),
        diagnosis=_text(payload.get("diagnosis"), "diagnosis", maximum=8_000),
        implementation_steps=tuple(
            _string_list(payload.get("implementation_steps"), "implementation_steps")
        ),
        risks=tuple(_string_list(payload.get("risks"), "risks")),
        evidence_refs=tuple(
            _string_list(payload.get("evidence_refs"), "evidence_refs")
        ),
    )


def _validate_checkpoint_envelope(payload: Any, task: RunTask) -> None:
    if not isinstance(payload, dict):
        raise DomainResultError("checkpoint não é objeto")
    account = payload.get("account")
    if not isinstance(account, dict) or account.get("id") != task.account_id:
        raise DomainResultError("conta do checkpoint divergente")
    if payload.get("scan_id") != task.scan_id or payload.get("domain") != task.domain:
        raise DomainResultError("scan ou domínio do checkpoint divergente")


def _require_current_task(task: RunTask, payload_hash: str, payload_path: str) -> None:
    if task.context_hash != payload_hash:
        raise ObsoleteTaskError("job aponta para checkpoint obsoleto")
    if Path(task.payload_path).resolve() != Path(payload_path).resolve():
        raise ObsoleteTaskError("caminho do job diverge do checkpoint")


def _text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DomainResultError(f"{field} deve ser texto não vazio e limitado")
    return value.strip()


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise DomainResultError(f"{field} inválido")
    return value


def _string_list(
    value: Any,
    field: str,
    *,
    maximum_items: int = 30,
    maximum_text: int = 2_000,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise DomainResultError(f"{field} deve ser lista limitada")
    return [_text(item, field, maximum=maximum_text) for item in value]


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise DomainResultError(f"colisão de artefato contextual: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
