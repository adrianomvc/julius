"""Um sinal que a análise contextual sustentou vira achado rastreável.

O que muda na promoção não é o dinheiro — é o estado epistêmico. Antes o padrão
era hipótese que ninguém tinha lido; depois é hipótese que alguém leu contra o
script inteiro e sustentou com justificativa. Isso não vale um número, e a
promoção não inventa nenhum: `estimated_saving` fica em zero e a qualidade da
economia é `unavailable`, exatamente como numa investigação bloqueada.

O que a promoção dá é o que faltava para o achado existir no produto: ID
estável, fingerprint, status, e portanto acesso a `julius lifecycle`,
`julius review` e `julius validate`. Sem isso o julgamento da IA morria numa
linha de relatório.

`origin="ai_confirmed"` é o que permite, mais tarde, medir se esse julgamento
valia — separando a precisão da camada contextual da precisão das regras.
"""

from __future__ import annotations

from typing import Any

from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal

PROMOTED_RULE_VERSION = "1.0.0"


def promote(
    signal: Signal,
    rationale: str,
    *,
    account: str,
    config: Any,
    scan_id: str,
) -> Opportunity:
    """Monta a investigação que nasce de um sinal confirmado."""
    estimation = Estimation(
        method=f"{signal.rule_id.lower().replace('-', '_')}_ai_confirmed_v1",
        baseline_cost=0.0,
        projected_cost=0.0,
        estimated_saving=0.0,
        assumptions=[
            "confirmação contextual não quantifica economia",
            "medir o custo do padrão antes de propor mudança",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
        saving_quality="unavailable",
    )
    opportunity = build(
        Finding(
            asset_type=signal.asset_type,
            asset_name=signal.asset_name,
            rule_id=signal.rule_id,
            rule_version=PROMOTED_RULE_VERSION,
            title=signal.observation,
            why=rationale,
            origin="ai_confirmed",
        ),
        Recommendation(
            difficulty=3,
            action="Medir o custo do padrão confirmado antes de alterar o ativo",
            how_to_apply=(
                "Coletar a evidência que falta e comparar uma execução de "
                "controle antes de mudar a definição."
            ),
            how_to_validate=(
                "Comparar duração, consumo e saída antes e depois, no mesmo volume."
            ),
            risks=["confirmação contextual não substitui benchmark"],
            docs=list(signal.doc_links),
            blocked=True,
        ),
        Evidence(
            items=[
                signal.observation,
                *(
                    [f"artefato {signal.artifact_sha256[:16]}"]
                    if signal.artifact_sha256
                    else []
                ),
                *(
                    [f"linhas={','.join(str(line) for line in signal.lines[:20])}"]
                    if signal.lines
                    else []
                ),
            ],
            sources=["análise contextual validada", f"sinal {signal.rule_id}"],
            observed_runs=1,
            coverage_days=0,
            has_optional_metrics=False,
            owner_tag=None,
        ),
        estimation,
        RuleContext(account=account, config=config, scan_id=scan_id),
    )
    opportunity.missing_evidence = list(signal.missing_evidence)
    if signal.artifact_sha256:
        opportunity.evidence_refs.append(
            {"sha256": signal.artifact_sha256, "lines": list(signal.lines)}
        )
    return opportunity
