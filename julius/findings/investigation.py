"""Fila não financeira para hipóteses analisadas pela camada contextual."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AIRecommendation:
    recommended_action: str
    why: str
    risks: list[str] = field(default_factory=list)
    required_validation: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AIEstimationProposal:
    method: str
    target: dict[str, float | int | str | bool] = field(default_factory=dict)
    evidence_refs: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ContextualEstimate:
    method: str
    status: str
    baseline_cost: float | None = None
    estimated_low: float | None = None
    estimated_expected: float | None = None
    estimated_high: float | None = None
    confidence: str = "low"
    include_in_portfolio: bool = False
    missing_evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Investigation:
    fingerprint: str
    account: str
    rule_id: str
    asset_type: str
    asset_name: str
    status: str
    rationale: str
    recommendation: AIRecommendation | None = None
    proposal: AIEstimationProposal | None = None
    estimate: ContextualEstimate | None = None
    evidence_hash: str = ""
    scan_id: str = ""
    prompt_version: str = ""
    decided_at: str = ""

