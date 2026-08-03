---
skill: julius-aws-analysis
case: rejected
rule_id: SFN-STANDARD-TO-EXPRESS
enforced_by: tests/test_semantic_facts.py::test_a_rejected_verdict_writes_nothing
---

## lacuna

Um "não" ambíguo tratado como fato produz o pior erro possível aqui: derivar
`idempotent = False` de um descarte que podia significar "não vale o esforço"
seria inventar fato a partir de silêncio.

## entrada

A mesma máquina, mas com uma Task de cobrança sem chave de deduplicação na ASL.

## saída esperada

Veredito `rejected`, com justificativa nomeando a Task e a linha. **Nada** é
escrito no inventário.

## critério de aceitação

`rejected` silencia o sinal pelo livro e não escreve campo nenhum. O sinal volta
se o hash da ASL mudar — um "não" dado sobre uma evidência não é um "não" para
sempre.
