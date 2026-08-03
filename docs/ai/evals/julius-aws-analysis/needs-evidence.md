---
skill: julius-aws-analysis
case: needs_evidence
rule_id: GLUE-CODE-SHUFFLE
enforced_by: tests/test_ai_estimates_and_cadence.py::test_express_estimate_requires_real_benchmark
---

## lacuna

Sem esta resposta a análise fica entre afirmar sem base e calar. As duas são
piores: a primeira inventa, a segunda faz o silêncio parecer ausência de problema.

## entrada

Um job Glue com `join`/`groupBy` no script e nenhuma medição de shuffle ou spill
no event log.

## saída esperada

Veredito `needs_evidence`, com `missing_evidence` nomeando exatamente o que falta:
shuffle read/write ou spill medidos. Sem faixa, sem cifra.

## critério de aceitação

O que falta precisa ser **nomeado**, não descrito como "faltam dados". A diferença
é entre alguém saber o que ligar e alguém não saber por onde começar.
