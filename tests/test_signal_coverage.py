"""Qual fórmula existente atende cada hipótese — e qual não atende, apesar de parecer.

Setenta regras emitem sinal e nove tinham cálculo. As demais não estavam sem fórmula
por decisão: ninguém tinha perguntado se alguma das existentes servia.
`glue_shuffle_reduction_v1` responde três regras da família `shuffle_partitioning` e
estava ligada a uma.

O teste que mais importa aqui é o do serviço. A primeira versão deste levantamento
sugeria `glue_interactive_capacity_reduction_v1` como precedente para
`REDSHIFT-RESIZE-TARGET`: as duas ajustam capacidade provisionada, e nenhuma linha da
fórmula de sessão Glue sabe o que é um nó de Redshift. Reaproveitamento assim parece
economia de esforço e é erro de cálculo.
"""

from __future__ import annotations

from julius.findings.signal import Signal
from julius.knowledge.contextual_estimation import allowed_methods
from julius.knowledge.coverage import (
    coverage_for,
    coverage_for_signals,
    service_of,
    summary,
)
from julius.knowledge.generative_estimation import eligible_rule_ids
from julius.knowledge.remediation import CATALOG


def _linha(rule_id: str):
    return next(item for item in coverage_for([rule_id]) if item.rule_id == rule_id)


def test_a_rule_with_its_own_method_is_calculated():
    linha = _linha("SFN-STANDARD-TO-EXPRESS")
    assert linha.status == "calculated"
    assert linha.method == "sfn_standard_to_express_v1"


def test_a_rule_with_a_contextual_range_is_calculated():
    linha = _linha("GLUE-CODE-PYTHON-UDF")
    assert linha.status == "calculated"
    assert linha.generative is True


def test_a_sibling_of_the_same_family_and_service_is_a_candidate():
    linha = _linha("GLUE-CODE-JDBC-SINGLE-READER")
    assert linha.status == "candidate"
    assert linha.sibling_method == "glue_shuffle_reduction_v1"


def test_a_glue_formula_is_never_offered_to_redshift():
    """O erro que este levantamento cometeu na primeira versão.

    `capacity_sizing` é a família das duas, e é justamente por isso que a família
    sozinha não basta: a fórmula está presa a um serviço, a um tipo de ativo e ao
    inventário que os descreve.
    """
    linha = _linha("REDSHIFT-RESIZE-TARGET")
    assert linha.family == "capacity_sizing"
    assert linha.sibling_method == ""
    assert linha.status == "uncovered"
    assert "REDSHIFT" in linha.reason


def test_the_reason_says_which_service_has_no_formula():
    assert "em SM" in _linha("SM-APP-INSTANCE-FIT").reason


def test_an_unclassified_rule_says_to_classify_it_first():
    linha = _linha("REGRA-QUE-NAO-EXISTE")
    assert linha.status == "uncovered"
    assert "classificar" in linha.reason


def test_service_comes_from_the_identifier_prefix():
    assert service_of("GLUE-CODE-SHUFFLE") == "GLUE"
    assert service_of("SM-TRAINING-SPOT-CANDIDATE") == "SM"
    assert service_of("S3-SMALL-FILES") == "S3"


def test_no_rule_has_both_a_method_and_a_contextual_range():
    """A dupla seria ambígua: o motor não saberia qual caminho a análise pediu."""
    assert not (set(allowed_methods()) & set(eligible_rule_ids()))


def test_every_calculated_rule_is_classified():
    """Cálculo sem família ficaria fora do agrupamento e da dedup de economia."""
    for rule_id in (*allowed_methods(), *eligible_rule_ids()):
        assert CATALOG.get(rule_id), f"{rule_id} tem cálculo e não tem família"


def test_candidates_are_listed_before_the_rest():
    """A lista existe para ser agida; o acionável vem primeiro."""
    linhas = coverage_for(
        ["SFN-STANDARD-TO-EXPRESS", "REDSHIFT-RESIZE-TARGET", "GLUE-CODE-JDBC-SINGLE-READER"]
    )
    assert [item.status for item in linhas][0] == "candidate"
    assert [item.status for item in linhas][-1] == "calculated"


def test_the_summary_counts_every_line_once():
    linhas = coverage_for(sorted(CATALOG)[:40])
    assert sum(summary(linhas).values()) == len(linhas)


def test_it_reads_the_rule_ids_out_of_real_signals():
    sinal = Signal(
        kind="code",
        rule_id="GLUE-CODE-SHUFFLE",
        asset_type="glue_job",
        asset_name="etl",
        observation="",
        question="",
    )
    assert coverage_for_signals([sinal, sinal])[0].rule_id == "GLUE-CODE-SHUFFLE"


def test_the_expansion_kept_the_shuffle_family_together():
    """As três regras de shuffle passam pela mesma conta, como a evidência já dizia."""
    metodos = allowed_methods()
    assert (
        metodos["GLUE-CODE-SHUFFLE"]
        == metodos["GLUE-CODE-SHUFFLE-PARTITIONS"]
        == metodos["GLUE-CODE-SINGLE-PARTITION"]
        == "glue_shuffle_reduction_v1"
    )


def test_the_command_runs_without_touching_aws():
    from typer.testing import CliRunner

    from julius.cli import app

    resultado = CliRunner().invoke(
        app,
        ["signals", "coverage", "--input", "data/sample/consumer-avi.json", "--show", "all"],
    )
    assert resultado.exit_code == 0, resultado.output
    assert "regra(s) distinta(s)" in resultado.output
