"""Log persistente e idempotente dos envios ativos."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from julius.reporting.delivery.models import SendResult


@dataclass(frozen=True)
class SendRecord:
    idempotency_key: str
    transport: str
    status: str
    sent_at: str
    provider_message_id: str | None = None


class SendLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def was_sent(self, idempotency_key: str) -> bool:
        item = self._load().get(idempotency_key)
        return bool(item and item.get("status") == "sent")

    def record(self, idempotency_key: str, result: SendResult) -> SendRecord:
        record = SendRecord(
            idempotency_key=idempotency_key,
            transport=result.transport,
            status=result.status,
            sent_at=datetime.now(timezone.utc).isoformat(),
            provider_message_id=result.provider_message_id,
        )
        data = self._load()
        data[idempotency_key] = asdict(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)
        return record
