---
skill: julius-aws-analysis
case: confirmed
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_generative_estimate.py::test_a_range_above_the_baseline_is_cut_to_it
---

## lacuna

Economia acima do custo do próprio ativo exigiria custo downstream comprovado, e comprová-lo é outro cálculo.

## entrada

Faixa proposta com máximo dez vezes maior que o custo do job.

## saída esperada

Teto cortado no baseline, com o corte registrado em evidência ausente.

## critério de aceitação

O corte precisa aparecer. Cortar em silêncio esconderia que a proposta estava fora de escala.
