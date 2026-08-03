---
skill: julius-signal-economic-analysis
case: confirmed
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_generative_estimate.py::test_a_generative_estimate_never_enters_the_portfolio
---

## lacuna

É a trava central do caminho generativo: faixa contextual não é economia medida.

## entrada

Qualquer faixa contextual aceita e completa.

## saída esperada

`include_in_portfolio` falso e maturidade que nunca soma.

## critério de aceitação

Vale para toda faixa deste caminho, sem exceção e sem configuração que a ligue.
