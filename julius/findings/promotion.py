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


def _com_piloto(signal: Signal, config: Any, pilot: Any) -> Estimation:
    """A cifra que o piloto mediu, reduzida pelo fator contextual."""
    medido = float(getattr(pilot, "measured_monthly", 0.0))
    fator = float(getattr(config, "contextual_realization_factor", 0.6))
    return Estimation(
        method=f"{signal.rule_id.lower().replace('-', '_')}_pilot_validated_v1",
        baseline_cost=medido,
        projected_cost=0.0,
        estimated_saving=round(medido * fator, 2),
        assumptions=[
            f"piloto mediu {medido:.2f}/mês; fator contextual {fator:.0%} aplicado",
            f"validado por {getattr(pilot, 'actor', '?')} "
            f"em {str(getattr(pilot, 'validated_at', ''))[:10]}",
            "o piloto confirma uma execução; a generalização é premissa",
        ],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
        # O piloto mediu a cobrança real do ativo, antes e depois. Isso é
        # `allocated` no vocabulário de baseline — ancorado na fatura, não em
        # tarifa. A distinção decide o portão de preço: exigir tabela verificada
        # aqui bloquearia uma cifra que não veio de tabela nenhuma.
        #
        # `measured` seria o valor intuitivo e está fora desta escala — ele
        # pertence a `saving_quality`. Inventar um valor faria o achado cair no
        # fallback e receber a pior nota da escala, silenciosamente.
        baseline_quality="allocated",
        # A economia foi medida numa execução e generalizada para o escopo. Isso
        # é evidência modelada, não medição do todo — e o fator conservador é o
        # que cobre a diferença.
        saving_quality="modeled_evidence",
    )


def promote(
    signal: Signal,
    rationale: str,
    *,
    account: str,
    config: Any,
    scan_id: str,
    pilot: Any = None,
) -> Opportunity:
    """Monta a investigação que nasce de um sinal confirmado.

    Com `pilot`, o que nasce é outra coisa: um achado com cifra. É o único
    caminho pelo qual economia nascida de interpretação encosta no total oficial,
    e ele exige as duas metades — alguém mediu, e alguém assinou.

    O fator conservador é mais duro que o determinístico e a razão não é o
    tamanho do número, é a origem: a oportunidade determinística parte de fato
    medido e o piloto confirma a conta; a contextual parte de leitura de código,
    e o piloto confirma **uma execução**. O que se generaliza dali é menos.
    """
    estimation = (
        _com_piloto(signal, config, pilot)
        if pilot is not None
        else Estimation(
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
            action=(
                "Aplicar a mudança validada pelo piloto no restante do escopo"
                if pilot is not None
                else "Medir o custo do padrão confirmado antes de alterar o ativo"
            ),
            how_to_apply=(
                "Repetir no restante do escopo a mudança que o piloto mediu, "
                "conferindo que o volume e o padrão de entrada são comparáveis."
                if pilot is not None
                else "Coletar a evidência que falta e comparar uma execução de "
                "controle antes de mudar a definição."
            ),
            how_to_validate=(
                "Comparar duração, consumo e saída antes e depois, no mesmo volume."
            ),
            risks=(
                [
                    "o piloto mediu uma execução; o restante do escopo pode "
                    "responder diferente",
                    "o fator conservador cobre parte dessa diferença, não toda",
                ]
                if pilot is not None
                else ["confirmação contextual não substitui benchmark"]
            ),
            docs=list(signal.doc_links),
            # Bloqueado enquanto ninguém mediu. O piloto é exatamente o que
            # desbloqueia — e é por isso que ele exige quem assina.
            blocked=pilot is None,
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
