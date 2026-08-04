---
skill: julius-aws-analysis
case: needs_evidence
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_generative_estimate.py::test_an_asset_without_cost_produces_no_range
---

## lacuna

Zero se lê como "não há o que ganhar aqui", quando o caso real é "não sei quanto vale".

## entrada

O mesmo sinal, num job sem DPU-hora observada na janela.

## saída esperada

`needs_evidence` com o baseline ausente nomeado. Nenhuma faixa.

## critério de aceitação

Ausência de baseline nunca vira zero, e o motivo aparece por escrito.
