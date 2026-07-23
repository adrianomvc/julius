"""Backlog persistente por fingerprint (histórico entre execuções).

Backend operacional: JSON portável. O histórico analítico complementar é
persistido por `HistoryStore` em DuckDB e exportado para Parquet.

Responsabilidades (base do ciclo de vida e da comparação temporal):
- carregar o backlog anterior;
- para cada oportunidade atual, preservar `first_seen` e o status; marcar `last_seen`;
- identificar oportunidades que **desapareceram** (candidatas a resolvidas);
- persistir o backlog atualizado.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from julius.opportunities.base import Opportunity


@dataclass
class Reconciliation:
    new: list[str] = field(default_factory=list)          # fingerprints vistos pela 1ª vez
    persisting: list[str] = field(default_factory=list)   # já existiam
    disappeared: list[dict] = field(default_factory=list)  # no backlog, ausentes agora


class BacklogStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def reconcile(
        self,
        opportunities: list[Opportunity],
        scan_id: str,
        today: date | None = None,
    ) -> Reconciliation:
        """Atualiza first_seen/last_seen (in place) e devolve o diff. Persiste o backlog."""
        today = today or date.today()
        stamp = today.isoformat()
        store = self._load()
        rec = Reconciliation()
        seen: set[str] = set()

        for o in opportunities:
            fp = o.fingerprint()
            seen.add(fp)
            prev = store.get(fp)
            if prev:
                o.first_seen = prev.get("first_seen", stamp)
                o.status = prev.get("status", o.status)
                rec.persisting.append(fp)
            else:
                o.first_seen = stamp
                rec.new.append(fp)
            o.last_seen = stamp
            store[fp] = {
                "account": o.account,
                "asset_type": o.asset_type,
                "asset_name": o.asset_name,
                "rule_id": o.rule_id,
                "first_seen": o.first_seen,
                "last_seen": stamp,
                "last_scan_id": scan_id,
                "monthly_expected": o.estimated_gain.monthly_expected,
                "status": o.status,
            }

        # Oportunidades que sumiram nesta conta (candidatas a resolvidas).
        accounts = {o.account for o in opportunities}
        for fp, entry in list(store.items()):
            if fp not in seen and entry.get("account") in accounts:
                if entry.get("status") not in ("resolved", "expired"):
                    rec.disappeared.append(entry)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return rec
