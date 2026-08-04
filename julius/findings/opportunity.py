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


@dataclass(frozen=True)
class CalibrationMetadata:
    """Como o histórico transformou o potencial técnico em expectativa."""

    factor_low: float
    factor_expected: float
    factor_high: float
    sample_count: int
    median_error: float
    confidence: str
    segment: str
    fallback_level: str


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
    currency: str = "USD"
    baseline_quality: str = "modeled"
    saving_quality: str = "modeled"
    baseline_bytes: int | None = None
    projected_bytes: int | None = None
    avoidable_bytes: int | None = None
    #: Custos pontuais não devem ser subtraídos de todos os meses. Estes campos
    #: tornam explícitos o primeiro mês e o ponto de equilíbrio.
    one_time_cost: float | None = None
    monthly_recurring_saving: float | None = None
    first_month_net_saving: float | None = None
    break_even_months: float | None = None
    maximum_profitable_reads: float | None = None
    #: Quanto custa **uma** ocorrência do risco, quando a frequência dele não foi
    #: observada. Não é economia e nunca entra em total nenhum: economia exige
    #: frequência, e inventá-la seria o oposto do que este produto faz. Existe
    #: porque "US$ 0,00" faz o time ignorar um achado cuja única informação útil
    #: é a ordem de grandeza do estrago.
    exposure_cost: float | None = None
    # Ganho não financeiro (migração, governança): existe economia de risco ou
    # de esforço, mas não um número em USD a somar no portfólio.
    is_strategic: bool = False
    # Seções da tabela versionada necessárias para sustentar a cifra modelada.
    # Economia medida diretamente não depende deste campo.
    pricing_dependencies: tuple[str, ...] = ()


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
    # `rule` ou `ai_confirmed`; ver `Finding.origin`.
    origin: str = "rule"
    # A que ação de remediação este achado pertence. Vem do catálogo em
    # `knowledge/remediation.py` e chega preenchido por `classify_opportunities` —
    # `findings` não enxerga `knowledge`, então o resultado viaja no campo.
    #
    # É a chave do agrupamento: dois achados do mesmo ativo em famílias diferentes
    # são duas ações de verdade (redimensionar e ligar bookmark), e fundi-los
    # transformava a segunda numa linha dentro dos riscos da primeira.
    remediation_family: str = ""
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
    blocked: bool = False

    # Processo gerador (linhagem) que pode ser pausado/desligado — quando aplicável.
    source_process: str | None = None
    process_cost_window: float | None = None
    process_cost_monthly: float | None = None
    window_end: str | None = None
    downstream_consumers: int = 0
    process_criticality: float = 0.0
    calibration_factor: float = 1.0
    # `estimated_gain` é sempre a estimativa técnica original. A calibração
    # vive separada para que auditoria e validação nunca aprendam sobre um
    # número que já foi alterado pelo próprio aprendizado.
    calibrated_gain: EstimatedGain | None = None
    calibration: CalibrationMetadata | None = None

    # Evidência / cobertura.
    evidence_coverage: float = 0.0
    # Escala comparável derivada do elo mais fraco entre a qualidade do baseline
    # e a da economia. Vive na entidade, não só no relatório, para que backlog e
    # histórico consigam ordenar achados por confiança.
    evidence_quality: str = "modeled_rule"
    missing_evidence: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    evidence_refs: list[dict] = field(default_factory=list)

    # Responsável (Squad) e contato (pessoa/ator).
    owner: str | None = None
    owner_source: str = "desconhecido"
    owner_confidence: float = 0.0
    owner_event_time: str = ""
    owner_event_name: str = ""
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

    @property
    def portfolio_gain(self) -> EstimatedGain:
        """Valor usado em ranking e totais, sem apagar o potencial técnico."""
        return self.calibrated_gain or self.estimated_gain

    @property
    def portfolio_exclusion_reasons(self) -> tuple[str, ...]:
        """Razões determinísticas que impedem somar a cifra ao portfólio."""
        reasons: list[str] = []
        if self.blocked:
            reasons.append("blocked")
        if self.estimated_gain.is_strategic:
            reasons.append("strategic")
        if self.estimation is None or self.estimation.saving_quality in {
            "unavailable",
            "potential",
        }:
            reasons.append("saving_not_validated")
        if self.portfolio_gain.monthly_expected <= 0:
            reasons.append("nonpositive_saving")
        return tuple(reasons)

    @property
    def include_in_portfolio(self) -> bool:
        """Único gate usado por ranking, KPIs e totais financeiros."""
        return not self.portfolio_exclusion_reasons

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
        payload = asdict(self)
        payload["include_in_portfolio"] = self.include_in_portfolio
        payload["portfolio_exclusion_reasons"] = list(
            self.portfolio_exclusion_reasons
        )
        return payload
