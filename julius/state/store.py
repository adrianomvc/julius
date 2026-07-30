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

from julius.findings.lifecycle import LifecycleEvent, transition
from julius.findings.opportunity import Opportunity
from julius.scoring import priority as prioritizer

_LEGACY_RULE_IDS = {"GLUE-IS-IDLE-TIMEOUT": "GLUE-IS-IDLE"}


@dataclass
class Reconciliation:
    new: list[str] = field(default_factory=list)          # fingerprints vistos pela 1ª vez
    persisting: list[str] = field(default_factory=list)   # já existiam
    disappeared: list[dict] = field(default_factory=list)  # no backlog, ausentes agora
    reopened: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    status_changes: list[dict] = field(default_factory=list)


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

    def _save(self, store: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def reconcile(
        self,
        opportunities: list[Opportunity],
        scan_id: str,
        today: date | None = None,
        account_id: str | None = None,
        ignored_rule_ids: frozenset[str] = frozenset(),
        protected_signal_keys: frozenset[tuple[str, str, str]] = frozenset(),
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
            if prev is None and o.rule_id in _LEGACY_RULE_IDS:
                legacy_rule = _LEGACY_RULE_IDS[o.rule_id]
                legacy_fp = next(
                    (
                        key
                        for key, entry in store.items()
                        if entry.get("account") == o.account
                        and entry.get("asset_type") == o.asset_type
                        and entry.get("asset_name") == o.asset_name
                        and entry.get("rule_id") == legacy_rule
                    ),
                    None,
                )
                if legacy_fp is not None:
                    prev = store.pop(legacy_fp)
            if prev:
                o.first_seen = prev.get("first_seen", stamp)
                previous_status = prev.get("status", o.status)
                previous_signature = prev.get("evidence_hash", "")
                current_signature = o.evidence_signature()
                evidence_changed = bool(
                    previous_signature
                    and previous_signature != current_signature
                )
                should_reopen = previous_status == "expired" or (
                    previous_status in {"dismissed", "validated"}
                    and evidence_changed
                )
                if should_reopen:
                    o.status = "detected"
                    rec.reopened.append(fp)
                    rec.status_changes.append(
                        {
                            "fingerprint": fp,
                            "account": o.account,
                            "opportunity_id": o.opportunity_id,
                            "from_status": previous_status,
                            "to_status": "detected",
                            "reason": "problema retornou ou surgiu nova evidência",
                        }
                    )
                else:
                    o.status = previous_status
                if (
                    previous_status in {"dismissed", "validated"}
                    and not should_reopen
                ):
                    rec.suppressed.append(fp)
                previous_gain = float(prev.get("monthly_expected") or 0.0)
                current_gain = o.portfolio_gain.monthly_expected
                if previous_gain > 0 and current_gain > previous_gain * 1.2:
                    ratio = min(1.5, current_gain / previous_gain)
                    o.urgency = max(o.urgency, float(prev.get("urgency") or 1.0) * ratio)
                    o.execution_priority = prioritizer.execution_priority(o)
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
                "opportunity_id": o.opportunity_id,
                "first_seen": o.first_seen,
                "last_seen": stamp,
                "last_scan_id": scan_id,
                "monthly_expected": o.portfolio_gain.monthly_expected,
                "technical_monthly_expected": o.estimated_gain.monthly_expected,
                "status": o.status,
                "urgency": o.urgency,
                "evidence_hash": o.evidence_signature(),
            }

        # Oportunidades que sumiram nesta conta (candidatas a resolvidas).
        accounts = {o.account for o in opportunities}
        if account_id:
            accounts.add(account_id)
        for fp, entry in list(store.items()):
            if fp not in seen and entry.get("account") in accounts:
                if entry.get("rule_id") in ignored_rule_ids:
                    continue
                if (
                    str(entry.get("asset_type") or ""),
                    str(entry.get("asset_name") or ""),
                    str(entry.get("rule_id") or ""),
                ) in protected_signal_keys:
                    continue
                if entry.get("status") not in (
                    "resolved",
                    "expired",
                    "validated",
                    "dismissed",
                ):
                    previous_status = entry.get("status", "detected")
                    new_status = (
                        "validated" if previous_status == "implemented" else "expired"
                    )
                    entry["status"] = new_status
                    entry["last_scan_id"] = scan_id
                    rec.disappeared.append(entry)
                    rec.status_changes.append(
                        {
                            "fingerprint": fp,
                            "account": entry.get("account", ""),
                            "opportunity_id": entry.get("opportunity_id", ""),
                            "from_status": previous_status,
                            "to_status": new_status,
                            "reason": "oportunidade desapareceu na nova execução",
                        }
                    )

        self._save(store)
        return rec

    def transition(
        self,
        fingerprint: str,
        to_status: str,
        *,
        actor: str,
        reason: str,
    ) -> LifecycleEvent:
        store = self._load()
        entry = store.get(fingerprint)
        if entry is None:
            raise KeyError(f"Fingerprint não encontrado no backlog: {fingerprint}")
        event = transition(
            fingerprint=fingerprint,
            account=str(entry.get("account") or ""),
            opportunity_id=str(entry.get("opportunity_id") or ""),
            from_status=str(entry.get("status") or "detected"),
            to_status=to_status,
            actor=actor,
            reason=reason,
        )
        entry["status"] = to_status
        entry.setdefault("transitions", []).append(
            {
                "from_status": event.from_status,
                "to_status": event.to_status,
                "actor": event.actor,
                "reason": event.reason,
                "occurred_at": event.occurred_at.isoformat(),
                "automatic": event.automatic,
            }
        )
        self._save(store)
        return event

    def status_for(self, fingerprint: str) -> str | None:
        entry = self._load().get(fingerprint)
        return str(entry.get("status")) if entry else None
