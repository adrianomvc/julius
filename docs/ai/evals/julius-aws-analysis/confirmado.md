---
skill: julius-aws-analysis
case: confirmed
rule_id: SFN-STANDARD-TO-EXPRESS
enforced_by: tests/test_semantic_facts.py::test_a_confirmed_verdict_asserts_the_signal_hypothesis
---

## lacuna

A regra `SFN-STANDARD-TO-EXPRESS` exigia `idempotent is True` para propor a
migração, e nenhum coletor preenchia o campo — a maior economia unitária do Step
Functions nunca disparava em conta real. A regra existia inteira e estava
desligada por um campo vazio.

## entrada

Uma state machine Standard cuja ASL não tem Task com efeito colateral não
idempotente: sem escrita sem chave de deduplicação, sem notificação, sem cobrança.

## saída esperada

Veredito `confirmed`, com justificativa citando as Tasks conferidas. O fato
`at_least_once_safe` escreve `idempotent = True` no inventário, e a regra passa a
propor a migração na execução seguinte.

## critério de aceitação

A direção precisa estar certa: confirmar afirma **a hipótese do sinal**, que é
*esta máquina deveria migrar*. Ler ao contrário recomendaria Express para a
máquina que duplica cobrança — o dano exato que a pergunta existe para evitar.
