---
skill: julius-aws-analysis
case: needs_evidence
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_estimate_contract.py::test_a_missing_region_is_refused
---

## lacuna

Sem região declarada não há como recusar a soma de duas tarifas de lugares diferentes — e essa soma não dá erro em lugar nenhum.

## entrada

Estimativa completa, sem região de tarifa.

## saída esperada

Rebaixada para `needs_evidence`, com a região ausente nomeada.

## critério de aceitação

Procedência ausente rebaixa, não passa com aviso.
