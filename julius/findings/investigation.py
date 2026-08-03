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
    """A conta que o motor fez a partir do cenário que a análise escolheu.

    Os campos de procedência não são burocracia. `Estimation`, do caminho
    determinístico, sempre carregou região, moeda e versão do cálculo; esta
    estrutura nasceu sem eles e virou uma segunda forma de dizer a mesma coisa,
    pior. Sem região não há como recusar a soma de dois números tarifados em
    lugares diferentes; sem moeda, a de duas moedas; sem período, a de um mês com
    uma janela de noventa dias. As três somas são fáceis de fazer sem perceber, e
    nenhuma delas é detectável depois.
    """

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
    #: Em que ponto do amadurecimento este número está. Ver `findings/maturity.py`.
    maturity: str = "pilot_required"
    #: Procedência da tarifa: sem isto não há como impedir mistura de região,
    #: de moeda ou de período.
    pricing_region: str = ""
    currency: str = "USD"
    period: str = "monthly"
    #: A versão do método que produziu estes números. Mudar a fórmula é criar uma
    #: versão nova, nunca reescrever a anterior — estimativa antiga continua
    #: explicável.
    method_version: str = ""
    #: A assinatura da evidência sobre a qual esta conta foi feita. É o que
    #: permite invalidá-la quando o script muda de hash ou a métrica aparece.
    evidence_hash: str = ""


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

