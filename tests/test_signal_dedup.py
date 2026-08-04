"""O mesmo dinheiro não pode ser contado duas vezes, nem por dois caminhos.

Um sinal e um achado sobre o mesmo ativo e a mesma correção derivam do mesmo custo:
`code_pattern_saving` e `potential()` chamam ambos `window_baseline(job)`. Mostrados
lado a lado com números próprios, fazem o leitor somar a mesma linha da fatura duas
vezes — e é a leitura que o produto existe para consertar.

Estes testes montam os objetos à mão porque o dataset de exemplo não produz sinal com
faixa: as faixas nascem da análise de código, que só roda com `--artifacts-manifest`.
"""

from __future__ import annotations

from julius.findings.consolidation import (
    absorb_quantified,
    cap_ranges_by_asset,
    deduplicate,
)
from julius.findings.opportunity import EstimatedGain, Estimation, Opportunity
from julius.findings.signal import PotentialRange, Signal

CUSTO_DO_JOB = 4200.0


def _achado(
    rule_id: str,
    familia: str,
    mensal: float,
    *,
    asset: str = "etl",
    blocked: bool = False,
) -> Opportunity:
    return Opportunity(
        opportunity_id=f"{rule_id}-x",
        account="123",
        asset_type="glue_job",
        asset_name=asset,
        category="custo",
        rule_id=rule_id,
        finding="achado",
        recommended_action="agir",
        remediation_family=familia,
        blocked=blocked,
        estimated_gain=EstimatedGain(monthly_expected=mensal),
        estimation=Estimation(
            method="m",
            baseline_cost=CUSTO_DO_JOB,
            projected_cost=CUSTO_DO_JOB - mensal,
            estimated_saving=mensal,
            saving_quality="measured",
        ),
    )


def _sinal(
    rule_id: str,
    familia: str,
    esperado: float | None,
    *,
    asset: str = "etl",
) -> Signal:
    faixa = (
        PotentialRange(
            low=esperado * 0.6,
            expected=esperado,
            high=esperado * 1.4,
            basis="DPU-hora da janela",
            caveat="fração típica do padrão",
            baseline=CUSTO_DO_JOB,
        )
        if esperado is not None
        else None
    )
    return Signal(
        kind="code",
        rule_id=rule_id,
        asset_type="glue_job",
        asset_name=asset,
        observation="padrão observado",
        question="custa capacidade aqui?",
        missing_evidence=["benchmark A/B"],
        remediation_family=familia,
        potential_range=faixa,
    )


# --- R2 -------------------------------------------------------------------


def test_a_quantified_action_absorbs_the_hypothesis_of_the_same_family():
    achados = [_achado("GLUE-SHUFFLE-SPILL", "shuffle_partitioning", 300.0)]
    sinais = [_sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 200.0)]
    saida = absorb_quantified(achados, sinais)
    assert saida[0].potential_range is None
    assert any("já está no portfólio" in item for item in saida[0].missing_evidence)


def test_the_hypothesis_survives_with_its_lines_and_hash():
    """Some a faixa, não o sinal: as linhas são o que alguém precisa para agir."""
    achados = [_achado("GLUE-SHUFFLE-SPILL", "shuffle_partitioning", 300.0)]
    sinal = _sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 200.0)
    saida = absorb_quantified(achados, [sinal])
    assert len(saida) == 1
    assert saida[0].rule_id == sinal.rule_id
    assert saida[0].observation == sinal.observation


def test_a_different_family_keeps_its_range():
    achados = [_achado("GLUE-SHUFFLE-SPILL", "shuffle_partitioning", 300.0)]
    sinais = [_sinal("GLUE-CODE-PYTHON-UDF", "row_level_processing", 200.0)]
    assert absorb_quantified(achados, sinais)[0].potential_range is not None


def test_a_different_asset_keeps_its_range():
    achados = [_achado("GLUE-SHUFFLE-SPILL", "shuffle_partitioning", 300.0, asset="etl")]
    sinais = [_sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 200.0, asset="outro")]
    assert absorb_quantified(achados, sinais)[0].potential_range is not None


def test_a_blocked_action_answers_nothing_and_absorbs_nothing():
    """Achado bloqueado não respondeu a pergunta — suprimir a faixa por causa dele
    esconderia a única estimativa disponível sobre o ativo."""
    achados = [
        _achado("GLUE-SHUFFLE-SPILL", "shuffle_partitioning", 0.0, blocked=True)
    ]
    sinais = [_sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 200.0)]
    assert absorb_quantified(achados, sinais)[0].potential_range is not None


def test_an_unclassified_signal_is_never_absorbed():
    achados = [_achado("GLUE-SHUFFLE-SPILL", "shuffle_partitioning", 300.0)]
    sinais = [_sinal("REGRA-NOVA", "", 200.0)]
    assert absorb_quantified(achados, sinais)[0].potential_range is not None


# --- R4 -------------------------------------------------------------------


def test_ranges_of_one_asset_fit_in_what_is_left_of_it():
    """Quatro sinais de um job de US$ 4.200 pediam US$ 6.300 juntos."""
    sinais = [
        _sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 2000.0),
        _sinal("GLUE-CODE-PYTHON-UDF", "row_level_processing", 1800.0),
        _sinal("GLUE-CODE-CACHE-LIFECYCLE", "driver_memory_cache", 1500.0),
        _sinal("GLUE-CODE-PUSHDOWN", "read_pruning", 1000.0),
    ]
    assert sum(item.potential_range.expected for item in sinais) > CUSTO_DO_JOB

    saida = cap_ranges_by_asset([], sinais)
    total = sum(item.potential_range.expected for item in saida)
    assert total <= CUSTO_DO_JOB + 0.05


def test_the_identified_economy_takes_its_share_first():
    achados = [_achado("GLUE-OVERPROVISIONED", "capacity_sizing", 3000.0)]
    sinais = [_sinal("GLUE-CODE-PYTHON-UDF", "row_level_processing", 2000.0)]
    saida = cap_ranges_by_asset(achados, sinais)
    # Sobrou 1200 do custo do ativo; a faixa não pode passar disso.
    assert saida[0].potential_range.expected <= 1200.05


def test_nobody_is_zeroed_for_arriving_later():
    """A ordem entre hipóteses do mesmo ativo é arbitrária e não decide quem some."""
    sinais = [
        _sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 3000.0),
        _sinal("GLUE-CODE-PYTHON-UDF", "row_level_processing", 3000.0),
    ]
    saida = cap_ranges_by_asset([], sinais)
    assert all(item.potential_range.expected > 0 for item in saida)
    assert (
        saida[0].potential_range.expected == saida[1].potential_range.expected
    ), "pediram o mesmo, devem receber o mesmo"


def test_the_reduction_is_written_down():
    sinais = [
        _sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 3000.0),
        _sinal("GLUE-CODE-PYTHON-UDF", "row_level_processing", 3000.0),
    ]
    saida = cap_ranges_by_asset([], sinais)
    assert "faixa reduzida" in saida[0].potential_range.caveat


def test_a_range_that_already_fits_is_untouched():
    sinais = [_sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 100.0)]
    saida = cap_ranges_by_asset([], sinais)
    assert saida[0].potential_range == sinais[0].potential_range


def test_a_range_without_a_baseline_is_left_alone():
    """Sem saber de que custo a faixa veio, reduzi-la seria inventar um teto."""
    sinal = _sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 9000.0)
    sem_base = sinal.__class__(
        **{
            **{k: getattr(sinal, k) for k in sinal.__dataclass_fields__},
            "potential_range": PotentialRange(
                low=1.0, expected=9000.0, high=9999.0, basis="?", caveat="?"
            ),
        }
    )
    assert cap_ranges_by_asset([], [sem_base])[0].potential_range.expected == 9000.0


# --- ordem ----------------------------------------------------------------


def test_absorbing_first_leaves_more_room_for_the_rest():
    """Se o teto fosse repartido antes de absorver, sobraria menos para quem fica."""
    achados = [_achado("GLUE-SHUFFLE-SPILL", "shuffle_partitioning", 1000.0)]
    sinais = [
        _sinal("GLUE-CODE-SHUFFLE", "shuffle_partitioning", 2500.0),
        _sinal("GLUE-CODE-PYTHON-UDF", "row_level_processing", 2500.0),
    ]
    saida = deduplicate(achados, sinais)
    absorvido, remanescente = saida
    assert absorvido.potential_range is None
    # Sobraram 3200 do ativo e só uma hipótese disputa: ela cabe inteira.
    assert remanescente.potential_range.expected == 2500.0
