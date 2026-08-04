---
skill: julius-aws-analysis
case: needs_evidence
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_estimate_contract.py::test_an_incomparable_period_is_refused
---

## lacuna

Converter trimestre em mês exigiria uma premissa que ninguém declarou.

## entrada

Estimativa com período trimestral.

## saída esperada

Rebaixada, com o período incomparável nomeado.

## critério de aceitação

Só períodos que o motor sabe comparar passam; o resto é mistura esperando acontecer.
