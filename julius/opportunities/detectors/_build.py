"""Builder compartilhado: monta uma `Opportunity` a partir de uma Estimation."""

from __future__ import annotations

import hashlib
from datetime import date

from julius.config import Config, JULIUS_VERSION, KNOWLEDGE_VERSION
from julius.estimation import build_gain
from julius.opportunities import confidence as conf_mod
from julius.opportunities import effort, impact, prioritizer
from julius.opportunities.base import Estimation, Opportunity


def resolve_owner(owner_tag: str | None) -> tuple[str | None, str, float]:
    """Precedência do MVP 1B: tag oficial; outras fontes entram no MVP 2."""
    if owner_tag:
        return owner_tag, "tag oficial", 1.0
    return None, "desconhecido", 0.0


def build(
    *,
    account: str,
    asset_type: str,
    asset_name: str,
    rule_id: str,
    rule_version: str,
    difficulty: int,
    estimation: Estimation,
    finding: str,
    why: str,
    recommended_action: str,
    how_to_apply: str,
    how_to_validate: str,
    evidence: list[str],
    risks: list[str],
    doc_links: list[str],
    data_sources: list[str],
    observed_runs: int,
    coverage_days: int,
    has_optional_metrics: bool,
    owner_tag: str | None,
    config: Config,
    scan_id: str,
    risk: float = 0.6,
    is_strategic: bool = False,
    blocked: bool = False,
    today: date | None = None,
    source_process: str | None = None,
) -> Opportunity:
    confidence, conf_label = conf_mod.score_and_label(
        observed_runs, coverage_days, has_optional_metrics, config
    )
    if estimation.saving_quality == "modeled_rule":
        confidence, conf_label = min(confidence, 0.50), "Baixa"
    elif estimation.saving_quality == "modeled_evidence":
        confidence = min(confidence, 0.70)
        conf_label = "Média" if confidence >= 0.55 else "Baixa"
    coverage = conf_mod.coverage_ratio(observed_runs, coverage_days, config)
    gain = build_gain(
        estimation.estimated_saving,
        difficulty,
        config,
        monthly_low=estimation.estimated_saving_low,
        monthly_high=estimation.estimated_saving_high,
        today=today,
        is_strategic=is_strategic,
    )
    gain_score = impact.gain_score(estimation.estimated_saving, config, is_strategic=is_strategic)
    owner, owner_source, owner_conf = resolve_owner(owner_tag)

    # `hash()` muda entre processos Python; o ID precisa permanecer estável para
    # que a revisão humana continue ligada à mesma oportunidade.
    asset_digest = hashlib.sha1(asset_name.encode("utf-8")).hexdigest()[:8]
    slug = f"{rule_id}-{asset_digest}"
    missing_evidence = [] if has_optional_metrics else ["métricas operacionais parciais"]
    if estimation.saving_quality == "unavailable":
        missing_evidence.append("contrafactual de bytes/custo ainda não medido")
    o = Opportunity(
        opportunity_id=slug,
        account=account,
        asset_type=asset_type,
        asset_name=asset_name,
        category="cost_optimization",
        rule_id=rule_id,
        finding=finding,
        why=why,
        recommended_action=recommended_action,
        how_to_apply=how_to_apply,
        how_to_validate=how_to_validate,
        evidence=evidence,
        risks=risks,
        doc_links=doc_links,
        estimated_gain=gain,
        estimation=estimation,
        gain_score=gain_score,
        difficulty_score=difficulty,
        confidence=confidence,
        confidence_label=conf_label,
        evidence_coverage=coverage,
        missing_evidence=missing_evidence,
        data_sources=data_sources,
        owner=owner,
        owner_source=owner_source,
        owner_confidence=owner_conf,
        rule_version=rule_version,
        knowledge_version=KNOWLEDGE_VERSION,
        julius_version=JULIUS_VERSION,
        scan_id=scan_id,
        source_process=source_process,
    )
    o.confidence_label = conf_label
    prioritizer.assign(o, risk=risk, blocked=blocked)
    return o
