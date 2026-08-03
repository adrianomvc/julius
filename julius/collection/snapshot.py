"""Snapshots locais versionados para fontes explicitamente elegíveis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SnapshotPolicy:
    """Contrato explícito de cache; fonte sem política nunca é reutilizada."""

    ttl_seconds: int
    collector_version: str
    serialize: Callable[[Any], Any]
    deserialize: Callable[[Any], Any]
    scope: Callable[[Any], dict[str, Any]]


@dataclass(frozen=True)
class SnapshotHit:
    value: Any
    age_seconds: int


class CollectionSnapshotStore:
    """Store JSON isolado por conta, região, fonte, escopo e versão."""

    def __init__(
        self,
        root: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def load(
        self,
        *,
        account_id: str,
        region: str,
        source: str,
        scope: dict[str, Any],
        policy: SnapshotPolicy,
    ) -> SnapshotHit | None:
        scope_hash = _stable_hash(scope)
        path = self._path(account_id, region, source, scope_hash)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict):
                return None
            expected = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "account_id": account_id,
                "region": region,
                "source": source,
                "scope_hash": scope_hash,
                "collector_version": policy.collector_version,
            }
            if any(envelope.get(key) != value for key, value in expected.items()):
                return None
            payload = envelope["payload"]
            if envelope.get("payload_hash") != _stable_hash(payload):
                return None
            collected_at = datetime.fromisoformat(str(envelope["collected_at"]))
            if collected_at.tzinfo is None:
                collected_at = collected_at.replace(tzinfo=timezone.utc)
            age = max(0, int((self._now() - collected_at).total_seconds()))
            if age > policy.ttl_seconds:
                return None
            return SnapshotHit(policy.deserialize(payload), age)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(
        self,
        *,
        account_id: str,
        region: str,
        source: str,
        scope: dict[str, Any],
        policy: SnapshotPolicy,
        value: Any,
    ) -> Path:
        scope_hash = _stable_hash(scope)
        payload = policy.serialize(value)
        envelope = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "account_id": account_id,
            "region": region,
            "source": source,
            "scope_hash": scope_hash,
            "collector_version": policy.collector_version,
            "collected_at": self._now().astimezone(timezone.utc).isoformat(),
            "payload_hash": _stable_hash(payload),
            "payload": payload,
        }
        path = self._path(account_id, region, source, scope_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def _path(
        self, account_id: str, region: str, source: str, scope_hash: str
    ) -> Path:
        identity = _stable_hash({"account_id": account_id, "region": region})[:24]
        source_key = hashlib.sha256(source.encode()).hexdigest()[:16]
        return self.root / identity / source_key / f"{scope_hash}.json"


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
