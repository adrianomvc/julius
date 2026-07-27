"""Montagem de uma `Opportunity` a partir das quatro coisas que a compõem.

`build` pedia 27 parâmetros. O sintoma era chamada longa; o problema era
amplificação de mudança — acrescentar um campo na entidade obrigava a tocar
todas as regras, que foi como `blocked`, `source_process` e `today` se
acumularam. Agora cada regra monta o que lhe diz respeito e entrega quatro
objetos coesos mais o contexto do scan.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

from julius.config import JULIUS_VERSION, KNOWLEDGE_VERSION
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.scoring import build_gain, evidence_quality, impact
from julius.scoring import confidence as conf_mod
from julius.scoring import priority as prioritizer


@dataclass(frozen=True)
class RuleContext:
    """O que é igual para toda regra dentro de um scan."""

    account: str
    config: Any
    scan_id: str
    today: date | None = None


def resolve_owner(owner_tag: str | None) -> tuple[str | None, str, float]:
    """Precedência do MVP 1B: tag oficial; outras fontes entram no MVP 2."""
    if owner_tag:
        return owner_tag, "tag oficial", 1.0
    return None, "desconhecido", 0.0


def build(
    finding: Finding,
    recommendation: Recommendation,
    evidence: Evidence,
    estimation: Estimation,
    ctx: RuleContext,
) -> Opportunity:
    config = ctx.config
    estimation.currency = config.pricing.currency

    confidence, conf_label = _confidence(evidence, estimation, config)
    coverage = conf_mod.coverage_ratio(
        evidence.observed_runs, evidence.coverage_days, config
    )
    # A mesma escala que ordena achados por confiança governa a largura da
    # faixa: quem não mede não pode apresentar incerteza estreita.
    quality = evidence_quality.combined(
        estimation.baseline_quality, estimation.saving_quality
    )
    gain = build_gain(
        estimation.estimated_saving,
        recommendation.difficulty,
        config,
        monthly_low=estimation.estimated_saving_low,
        monthly_high=estimation.estimated_saving_high,
        today=ctx.today,
        is_strategic=estimation.is_strategic,
        quality=quality,
    )
    owner, owner_source, owner_conf = resolve_owner(evidence.owner_tag)

    opportunity = Opportunity(
        # `hash()` muda entre processos Python; o ID precisa permanecer estável
        # para que a revisão humana continue ligada à mesma oportunidade.
        opportunity_id=f"{finding.rule_id}-{_digest(finding.asset_name)}",
        account=ctx.account,
        asset_type=finding.asset_type,
        asset_name=finding.asset_name,
        category=finding.category,
        origin=finding.origin,
        rule_id=finding.rule_id,
        finding=finding.title,
        why=finding.why,
        recommended_action=recommendation.action,
        how_to_apply=recommendation.how_to_apply,
        how_to_validate=recommendation.how_to_validate,
        evidence=list(evidence.items),
        risks=list(recommendation.risks),
        doc_links=list(recommendation.docs),
        estimated_gain=gain,
        estimation=estimation,
        gain_score=impact.gain_score(
            estimation.estimated_saving,
            config,
            is_strategic=estimation.is_strategic,
        ),
        difficulty_score=recommendation.difficulty,
        confidence=confidence,
        confidence_label=conf_label,
        evidence_coverage=coverage,
        evidence_quality=quality.name.lower(),
        missing_evidence=_missing(evidence, estimation),
        data_sources=list(evidence.sources),
        owner=owner,
        owner_source=owner_source,
        owner_confidence=owner_conf,
        rule_version=finding.rule_version,
        knowledge_version=KNOWLEDGE_VERSION,
        julius_version=JULIUS_VERSION,
        scan_id=ctx.scan_id,
        source_process=finding.source_process,
        blocked=recommendation.blocked,
    )
    prioritizer.assign(
        opportunity, risk=recommendation.risk, blocked=recommendation.blocked
    )
    return opportunity


def _confidence(
    evidence: Evidence, estimation: Estimation, config: Any
) -> tuple[float, str]:
    """Confiança da coleta, com teto pela qualidade da economia estimada.

    Economia modelada por faixa de regra não pode ser apresentada com a mesma
    confiança de economia medida, por mais execuções que a coleta tenha visto.
    """
    confidence, label = conf_mod.score_and_label(
        evidence.observed_runs,
        evidence.coverage_days,
        evidence.has_optional_metrics,
        config,
    )
    if estimation.saving_quality == "modeled_rule":
        return min(confidence, 0.50), "Baixa"
    if estimation.saving_quality == "modeled_evidence":
        capped = min(confidence, 0.70)
        return capped, ("Média" if capped >= 0.55 else "Baixa")
    return confidence, label


def _missing(evidence: Evidence, estimation: Estimation) -> list[str]:
    missing = [] if evidence.has_optional_metrics else ["métricas operacionais parciais"]
    if estimation.saving_quality == "unavailable":
        missing.append("contrafactual de bytes/custo ainda não medido")
    return missing


def _digest(asset_name: str) -> str:
    return hashlib.sha1(asset_name.encode("utf-8")).hexdigest()[:8]
