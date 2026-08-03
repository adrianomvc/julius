"""O que um veredito de sinal passa a valer como fato do inventário.

Alguns campos do modelo não são coletáveis. `StateMachine.idempotent` é o caso
exemplar: tolerar semântica at-least-once é propriedade da lógica de negócio, e
nenhuma API da AWS responde isso. O modelo sempre soube disso — o comentário do
campo diz que ele fica `None` "até a análise contextual julgar a ASL".

O que faltava era o retorno. A pergunta era feita (sinal
`SFN-STANDARD-TO-EXPRESS`), o julgamento era registrado (livro de sinais), e
nada escrevia a resposta de volta no inventário. `detect` exigia
`idempotent is True` para propor a migração, e como ninguém preenchia o campo, a
maior economia unitária do Step Functions nunca disparava em conta real. A regra
existia inteira — estimativa, gate de benchmark, método permitido para a IA
propor — e estava desligada por um campo vazio.

**A direção do veredito importa e é fácil de inverter.** Um sinal confirmado
afirma a hipótese do sinal, e a hipótese de `SFN-STANDARD-TO-EXPRESS` é *esta
máquina deveria migrar*. Confirmar é dizer que a reexecução é tolerável — daí
`idempotent = True`. Ler ao contrário recomendaria Express para a máquina que
duplica cobrança, que é exatamente o dano que a pergunta existe para evitar.

Só `confirmed` escreve. `rejected` já silencia o sinal pelo livro, e ele pode
significar coisas diferentes — "duplica cobrança" e "não vale o esforço" chegam
com o mesmo rótulo. Derivar `idempotent = False` de um "não" ambíguo seria
inventar um fato a partir de um silêncio.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from julius.collection.models import Account


def apply_verdicts(account: Account, decisions: Iterable[Any]) -> list[str]:
    """Escreve no inventário o que já foi julgado. Devolve o que mudou."""
    aplicados: list[str] = []
    por_maquina = {machine.name: machine for machine in account.state_machines}
    for decision in decisions:
        if getattr(decision, "verdict", "") != "confirmed":
            continue
        if getattr(decision, "rule_id", "") != "SFN-STANDARD-TO-EXPRESS":
            continue
        machine = por_maquina.get(str(getattr(decision, "asset_name", "")))
        if machine is None or machine.idempotent is True:
            continue
        machine.idempotent = True
        aplicados.append(f"{machine.name}: idempotent=True por veredito contextual")
    return aplicados
