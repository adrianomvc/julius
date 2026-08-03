---
skill: julius-signal-economic-analysis
case: rejected
rule_id: GLUE-CODE-SHUFFLE
enforced_by: tests/test_generative_estimate.py::test_a_rule_outside_the_allowlist_is_refused
---

## lacuna

Elegibilidade que vira categoria deixa de ser lista: qualquer sinal passaria a aceitar número.

## entrada

Sinal confirmado de shuffle — que tem fórmula no motor — com uma faixa contextual bem preenchida.

## saída esperada

Recusa dura. Havendo fórmula, o caminho é a proposta de cenário, e o motor executa a conta.

## critério de aceitação

A recusa não depende da qualidade da proposta: um `rule_id` fora da lista não recebe número por melhor que a faixa esteja.
