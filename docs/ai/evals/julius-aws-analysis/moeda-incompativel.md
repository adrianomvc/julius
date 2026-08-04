---
skill: julius-aws-analysis
case: needs_evidence
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_estimate_contract.py::test_a_currency_that_differs_from_the_baseline_is_refused
---

## lacuna

Somar duas moedas produz um número bem formado e errado, indetectável depois.

## entrada

Estimativa em BRL sobre baseline em USD.

## saída esperada

Rebaixada, com a divergência de moeda nomeada.

## critério de aceitação

A comparação é contra a moeda do baseline, não contra uma constante.
