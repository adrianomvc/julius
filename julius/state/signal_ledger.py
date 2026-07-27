"""O que a análise contextual já julgou, e o que isso passa a valer.

Um sinal é uma pergunta. Sem memória, a mesma pergunta volta a cada execução e
a análise se gasta re-julgando o que já julgou — um único script Glue produz até
quinze sinais, e o validador exige veredito para todos. Em uma conta com dezenas
de jobs isso deixa de ser desperdício e passa a ser o que impede a análise de
chegar ao que ainda não foi visto.

Este livro é o que dá memória. Ele espelha `BacklogStore` na forma e no backend
— JSON portável, chaveado por fingerprint — mas tem relógio próprio, e é isso
que justifica não ser o mesmo arquivo: uma oportunidade se reconcilia dentro do
scan, enquanto o veredito de um sinal chega **depois**, por `agent validate`. O
que o scan de hoje grava é o que o de amanhã lê.

Um descarte silencia o sinal enquanto a evidência não mudar. Script com hash
novo, métrica que apareceu, limiar reconfigurado — qualquer um deles muda a
assinatura e a pergunta volta, porque o que a sustentava mudou. É a mesma
disciplina de `dismissed` em `state/store.py`, e pelo mesmo motivo: um "não"
dado com base numa evidência não é um "não" para sempre.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from julius.findings.signal import Signal


class Verdict(Protocol):
    """O julgamento, visto de fora da camada que o produziu.

    Declarado como protocolo, e não importado de `analysis`, porque a seta
    aponta só para baixo: o estado guarda decisões sobre achados e não pode
    depender de quem conversa com o provedor de análise.
    """

    rule_id: str
    asset_name: str
    verdict: str
    rationale: str

#: `needs_evidence` mantém o sinal no pacote porque a pergunta segue de pé.
#: `confirmed` também, mas só até a promoção acontecer: a partir daí quem
#: carrega o assunto é o achado no backlog, e continuar perguntando seria
#: exatamente o desperdício que este livro existe para evitar.
_KEEPS_OPEN = frozenset({"needs_evidence"})


@dataclass
class SignalDecision:
    """O que ficou decidido sobre um sinal, e sobre qual evidência."""

    fingerprint: str
    account: str
    rule_id: str
    asset_type: str
    asset_name: str
    verdict: str
    rationale: str
    evidence_hash: str
    scan_id: str
    prompt_version: str
    decided_at: str
    promoted: bool = False


@dataclass
class Suppression:
    """O que entrou no pacote e o que ficou de fora, com o porquê."""

    open: list[Signal] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)


class SignalLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, store: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record_verdicts(
        self,
        verdicts: Sequence[Verdict],
        signals: list[Signal],
        *,
        account: str,
        scan_id: str,
        prompt_version: str,
        decided_at: datetime | None = None,
    ) -> int:
        """Grava o julgamento junto da evidência sobre a qual ele foi feito.

        Sem a assinatura de evidência o veredito seria um carimbo permanente; é
        ela que permite reabrir a pergunta quando o artefato muda.
        """
        by_key = {(item.rule_id, item.asset_name): item for item in signals}
        stamp = (decided_at or datetime.now(timezone.utc)).isoformat()
        store = self._load()
        recorded = 0
        for verdict in verdicts:
            signal = by_key.get((verdict.rule_id, verdict.asset_name))
            if signal is None:
                continue
            fingerprint = signal.fingerprint(account)
            previous = store.get(fingerprint) or {}
            store[fingerprint] = {
                "account": account,
                "rule_id": signal.rule_id,
                "asset_type": signal.asset_type,
                "asset_name": signal.asset_name,
                "verdict": verdict.verdict,
                "rationale": verdict.rationale,
                "evidence_hash": signal.evidence_signature(),
                "scan_id": scan_id,
                "prompt_version": prompt_version,
                "decided_at": stamp,
                # Um novo veredito sobre a mesma evidência não desfaz uma
                # promoção já feita; o achado promovido tem vida própria no
                # backlog a partir daí.
                "promoted": bool(previous.get("promoted")),
            }
            recorded += 1
        self._save(store)
        return recorded

    def suppress(self, signals: list[Signal], account: str) -> Suppression:
        """Deixa passar só o que ainda está em aberto."""
        store = self._load()
        result = Suppression()
        for signal in signals:
            entry = store.get(signal.fingerprint(account))
            if entry is None:
                result.open.append(signal)
                continue
            verdict = str(entry.get("verdict") or "")
            if verdict in _KEEPS_OPEN or (
                verdict == "confirmed" and not entry.get("promoted")
            ):
                result.open.append(signal)
                continue
            if str(entry.get("evidence_hash") or "") != signal.evidence_signature():
                # A evidência que sustentava o descarte mudou.
                result.open.append(signal)
                result.reopened.append(signal.fingerprint(account))
                continue
            result.suppressed.append(signal.fingerprint(account))
        return result

    def pending_promotions(self, account: str) -> list[SignalDecision]:
        """Confirmados que ainda não viraram achado no backlog."""
        return [
            _decision(fingerprint, entry)
            for fingerprint, entry in self._load().items()
            if entry.get("account") == account
            and entry.get("verdict") == "confirmed"
            and not entry.get("promoted")
        ]

    def mark_promoted(self, fingerprints: list[str]) -> None:
        if not fingerprints:
            return
        store = self._load()
        for fingerprint in fingerprints:
            if fingerprint in store:
                store[fingerprint]["promoted"] = True
        self._save(store)

    def decisions_for(self, account: str) -> list[SignalDecision]:
        return [
            _decision(fingerprint, entry)
            for fingerprint, entry in self._load().items()
            if entry.get("account") == account
        ]


def _decision(fingerprint: str, entry: dict) -> SignalDecision:
    return SignalDecision(
        fingerprint=fingerprint,
        account=str(entry.get("account") or ""),
        rule_id=str(entry.get("rule_id") or ""),
        asset_type=str(entry.get("asset_type") or ""),
        asset_name=str(entry.get("asset_name") or ""),
        verdict=str(entry.get("verdict") or ""),
        rationale=str(entry.get("rationale") or ""),
        evidence_hash=str(entry.get("evidence_hash") or ""),
        scan_id=str(entry.get("scan_id") or ""),
        prompt_version=str(entry.get("prompt_version") or ""),
        decided_at=str(entry.get("decided_at") or ""),
        promoted=bool(entry.get("promoted")),
    )
