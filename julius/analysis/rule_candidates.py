"""A fila que faz o catálogo determinístico crescer.

Quando a análise contextual encontra desperdício que nenhuma `rule_id` cobre,
esse achado não vira oportunidade: ele não tem economia calculada nem posição no
ranking, e ninguém verificou se o padrão se repete. O que ele é, de fato, é uma
proposta de regra.

Aqui a proposta é acumulada entre execuções e contas. Um padrão que apareceu uma
vez é anedota; o mesmo padrão em três contas e cinco scans é uma regra
determinística esperando ser escrita — e `occurrences` é o que separa os dois
casos sem depender da memória de quem revisa.

A chave de deduplicação é `(conta, proposed_rule_id, sha256)`: o mesmo padrão no
mesmo artefato da mesma conta é uma ocorrência, não uma entrada nova.
"""

from __future__ import annotations

import json
from pathlib import Path

from julius.analysis.response_validator import AgentOutputError, ContextualAnalysis

SCHEMA_VERSION = "1.0"


def append_candidates(
    analysis: ContextualAnalysis,
    path: str | Path,
    *,
    observed_on: str = "",
) -> int:
    """Acumula os achados não cobertos e devolve quantos foram registrados."""
    if not analysis.uncovered_findings:
        return 0
    target = Path(path)
    existing = _load(target)
    by_key = {_key(item): item for item in existing}
    stamp = observed_on or analysis.scan_id

    for finding in analysis.uncovered_findings:
        entry: dict[str, object] = {
            "account": analysis.account,
            "proposed_rule_id": finding.proposed_rule_id,
            "title": finding.title,
            "asset_type": finding.asset_type,
            "asset_name": finding.asset_name,
            "why_not_covered": finding.why_not_covered,
            "confidence_basis": finding.confidence_basis,
            "sha256": finding.evidence_ref.sha256,
            "lines": list(finding.evidence_ref.lines),
        }
        key = _key(entry)
        current = by_key.get(key)
        if current is None:
            entry["first_seen_scan"] = stamp
            entry["last_seen_scan"] = stamp
            entry["occurrences"] = 1
            by_key[key] = entry
            continue
        # Reencontro do mesmo padrão: o texto mais recente vale, o histórico
        # de quantas vezes ele apareceu é o que decide a promoção a regra.
        occurrences = int(current.get("occurrences", 1))
        first_seen = current.get("first_seen_scan", stamp)
        entry["first_seen_scan"] = first_seen
        entry["last_seen_scan"] = stamp
        entry["occurrences"] = occurrences + 1
        by_key[key] = entry

    rows = sorted(
        by_key.values(),
        key=lambda item: (-int(item.get("occurrences", 1)), str(item["proposed_rule_id"])),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "candidates": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return len(analysis.uncovered_findings)


def _key(entry: dict) -> tuple[str, str, str]:
    return (
        str(entry.get("account", "")),
        str(entry.get("proposed_rule_id", "")),
        str(entry.get("sha256", "")),
    )


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentOutputError(
            f"fila de candidatos ilegível: {type(exc).__name__}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
        raise AgentOutputError("fila de candidatos inválida")
    return [item for item in raw["candidates"] if isinstance(item, dict)]
