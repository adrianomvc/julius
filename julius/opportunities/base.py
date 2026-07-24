"""Entidade central: `Opportunity`.

Unifica findings de custo (e, nas fases seguintes, governança) num backlog
rastreável. Ganho, dificuldade, confiança e prioridade são DETERMINÍSTICOS.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass
class EstimatedGain:
    """Projeção financeira sempre exibida: potencial × realizável."""

    monthly_low: float = 0.0
    monthly_expected: float = 0.0
    monthly_high: float = 0.0
    annual_potential: float = 0.0
    likely_implementation_date: str | None = None
    realization_factor: float = 0.8
    realizable_year: float = 0.0
    is_strategic: bool = False  # ganho não financeiro (ex.: migração)


@dataclass
class Estimation:
    """Registro auditável do cálculo financeiro (por detector, versionado)."""

    method: str
    baseline_cost: float
    projected_cost: float
    estimated_saving: float
    estimated_saving_low: float | None = None
    estimated_saving_high: float | None = None
    assumptions: list[str] = field(default_factory=list)
    pricing_region: str = "sa-east-1"
    estimation_version: str = "1.0"
    baseline_quality: str = "modeled"
    saving_quality: str = "modeled"
    baseline_bytes: int | None = None
    projected_bytes: int | None = None
    avoidable_bytes: int | None = None


@dataclass
class Opportunity:
    opportunity_id: str
    account: str
    asset_type: str
    asset_name: str
    category: str
    rule_id: str
    finding: str
    recommended_action: str
    how_to_apply: str = ""
    how_to_validate: str = ""
    why: str = ""
    evidence: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    doc_links: list[str] = field(default_factory=list)

    estimated_gain: EstimatedGain = field(default_factory=EstimatedGain)
    estimation: Estimation | None = None

    # Scores determinísticos.
    gain_score: int = 0
    difficulty_score: int = 1
    confidence: float = 0.0
    confidence_label: str = "Baixa"
    urgency: float = 1.0
    execution_priority: int = 0
    strategic_priority: int = 0
    bucket: str = "monitorar"

    # Gate de acionabilidade.
    actionable: bool = True
    next_action: str | None = None

    # Processo gerador (linhagem) que pode ser pausado/desligado — quando aplicável.
    source_process: str | None = None
    downstream_consumers: int = 0
    process_criticality: float = 0.0
    calibration_factor: float = 1.0

    # Evidência / cobertura.
    evidence_coverage: float = 0.0
    missing_evidence: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)

    # Responsável (Squad) e contato (pessoa/ator).
    owner: str | None = None
    owner_source: str = "desconhecido"
    owner_confidence: float = 0.0
    actor: str | None = None
    actor_source: str | None = None
    actor_confidence: float = 0.0

    # Auditoria / versionamento.
    rule_version: str = "1.0.0"
    knowledge_version: str = ""
    julius_version: str = ""
    scan_id: str = ""

    # Ciclo de vida.
    status: str = "detected"
    first_seen: str = ""
    last_seen: str = ""

    def fingerprint(self, scope: str = "default") -> str:
        raw = f"{self.account}|{self.asset_type}:{self.asset_name}|{self.rule_id}|{scope}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"{raw}#{digest}"

    def evidence_signature(self) -> str:
        """Assinatura estável das evidências que podem justificar reabertura."""
        payload = {
            "evidence": sorted(self.evidence),
            "missing_evidence": sorted(self.missing_evidence),
            "data_sources": sorted(self.data_sources),
            "confidence": round(self.confidence, 4),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)
