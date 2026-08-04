---
skill: julius-aws-analysis
case: needs_evidence
rule_id: GLUE-CODE-PYTHON-UDF
enforced_by: tests/test_generative_estimate.py::test_promotional_documentation_is_not_official_documentation
---

## lacuna

Página de produto tratada como tarifa é como uma frase promocional vira conta.

## entrada

Faixa sustentada apenas por `aws.amazon.com/glue/pricing/`.

## saída esperada

Rebaixada, com a documentação oficial ausente nomeada.

## critério de aceitação

Só `docs.aws.amazon.com` sustenta mecanismo de cobrança.
