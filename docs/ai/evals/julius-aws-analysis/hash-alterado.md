---
skill: julius-aws-analysis
case: needs_evidence
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_estimate_contract.py::test_a_changed_artifact_changes_the_signature_that_invalidates_it
---

## lacuna

Estimativa reaproveitada sobre um script que mudou continua valendo sobre algo que não existe mais.

## entrada

O mesmo sinal, com o artefato reescrito e hash novo.

## saída esperada

A assinatura de evidência muda, e o livro de sinais reabre a pergunta.

## critério de aceitação

A invalidação é por assinatura, não por data — e mora no livro de sinais, não duplicada aqui.
