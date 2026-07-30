"""A ordem em que as ações são apresentadas.

Toda lista de ações que o produto mostra — a tabela do relatório, o corte
executável do Pareto, o Top 10 cuja precisão é medida — sai da mesma chave. Antes
eram quatro ordenações diferentes, e a divergência não era estética: a única
oportunidade que entrava no portfólio do dataset de exemplo (US$ 1.412,60/mês)
aparecia em nono, abaixo de cinco itens de cifra não validada valendo zero.
"""

from __future__ import annotations

from julius.findings.opportunity import EstimatedGain, Opportunity
from julius.reporting.pareto import compute
from julius.scoring.priority import ranking_key


def _oportunidade(
    *,
    rule_id: str,
    execution: int,
    monthly: float,
    strategic: bool = False,
    blocked: bool = False,
    saving_quality: str = "measured",
    bucket: str = "fazer_agora",
    difficulty: int = 1,
) -> Opportunity:
    from julius.findings.opportunity import Estimation

    o = Opportunity(
        opportunity_id=rule_id,
        category="cost",
        account="1",
        asset_type="glue",
        asset_name=rule_id,
        rule_id=rule_id,
        finding=rule_id,
        recommended_action="agir",
        how_to_validate="conferir",
        owner="dono",
        evidence=["evidência"],
        estimation=Estimation(
            method="teste",
            baseline_cost=monthly,
            projected_cost=0.0,
            estimated_saving=monthly,
            saving_quality=saving_quality,
            is_strategic=strategic,
        ),
    )
    o.estimated_gain = EstimatedGain(
        monthly_expected=monthly, realizable_year=monthly * 12, is_strategic=strategic
    )
    o.calibrated_gain = None
    o.execution_priority = execution
    o.difficulty_score = difficulty
    o.confidence = 0.9
    o.actionable = True
    o.blocked = blocked
    o.bucket = bucket
    return o


def test_the_portfolio_gate_outranks_a_higher_execution_score():
    """Cifra não validada não encabeça a lista, por mais alto que pontue.

    É o caso real do dataset de exemplo: estratégico recebe ganho fixo 92 com
    dificuldade 1, então vence economia medida com dificuldade 2 — e ocupava o
    topo valendo zero.
    """
    fora = _oportunidade(
        rule_id="GLUE-TIMEOUT-EXCESSIVE",
        execution=63,
        monthly=0.0,
        strategic=True,
        saving_quality="unavailable",
    )
    dentro = _oportunidade(
        rule_id="REDSHIFT-IDLE-CLUSTER", execution=43, monthly=1412.60
    )

    assert fora.include_in_portfolio is False
    assert dentro.include_in_portfolio is True
    assert sorted([fora, dentro], key=ranking_key, reverse=True)[0] is dentro


def test_inside_the_gate_the_composite_decides_not_the_raw_value():
    """Ação cara e difícil não vem antes da barata e imediata."""
    cara = _oportunidade(
        rule_id="CARA", execution=20, monthly=900.0, difficulty=4
    )
    barata = _oportunidade(
        rule_id="BARATA", execution=70, monthly=120.0, difficulty=1
    )

    ordem = sorted([cara, barata], key=ranking_key, reverse=True)

    assert [o.rule_id for o in ordem] == ["BARATA", "CARA"]


def test_the_order_does_not_depend_on_the_order_the_rules_ran():
    """Empate em tudo não pode deixar a ordem de chegada decidir.

    A chave precisa ser ordem **total**, senão a mesma conta produz listas
    diferentes conforme a ordem de avaliação das regras, e o diff entre dois
    scans deixa de ser legível.
    """
    itens = [
        _oportunidade(rule_id=f"R{i}", execution=50, monthly=100.0) for i in range(6)
    ]

    uma = [o.rule_id for o in sorted(itens, key=ranking_key, reverse=True)]
    outra = [
        o.rule_id for o in sorted(list(reversed(itens)), key=ranking_key, reverse=True)
    ]

    assert uma == outra


def test_the_executable_cut_follows_the_implementation_order():
    """O corte executável é lista de implantação, não corte financeiro."""
    cara = _oportunidade(rule_id="CARA", execution=20, monthly=900.0, difficulty=4)
    barata = _oportunidade(rule_id="BARATA", execution=70, monthly=120.0)

    pareto = compute([cara, barata])

    assert [o.rule_id for o in pareto.executable_focus] == ["BARATA", "CARA"]


def test_the_financial_cut_stays_ordered_by_value():
    """Ali a pergunta é quantas ações somam 80% — e exige as maiores primeiro."""
    cara = _oportunidade(rule_id="CARA", execution=20, monthly=900.0, difficulty=4)
    barata = _oportunidade(rule_id="BARATA", execution=70, monthly=120.0)

    pareto = compute([cara, barata])

    assert [o.rule_id for o in pareto.financial_focus] == ["CARA"]
    assert pareto.financial_pct >= 80
