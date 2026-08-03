"""A ordem de grandeza de um sinal, calculada num lugar só.

Um sinal sem número é fácil de ignorar. Quinze hipóteses num relatório, todas
sem cifra, ordenam-se por nada — e a que vale dez mil por mês fica ao lado da
que vale dez. A faixa existe para responder **vale a pena investigar isto
antes daquilo**, e só isso.

O que ela não é: uma economia. A fração aplicada ao custo do ativo é premissa
sobre o padrão, não medição dele — se houvesse medição o achado teria virado
`Opportunity` e teria uma cifra de verdade. Por isso todo caminho passa por
aqui, em vez de cada regra montar a sua: uma faixa construída em quinze lugares
vira quinze convenções, e a décima sexta esquece de dizer que é hipótese.

As três funções que constroem a base — DPU-hora do Glue, custo atribuído do
SageMaker, transição do Step Functions — vivem nas regras de cada serviço, que
é onde a tarifa e a janela já são conhecidas. Aqui fica só a forma da faixa.
"""

from __future__ import annotations

from julius.findings.signal import PotentialRange

#: A fração do padrão é o teto otimista, não o valor esperado. Os multiplicadores
#: abaixo abrem a faixa para baixo porque o caso típico não é o melhor caso, e
#: uma faixa que começa no teto não é faixa — é uma promessa com margem de erro
#: para cima.
_LOW = 0.4
_EXPECTED = 0.7


def potential(
    baseline: float | None,
    *,
    fraction: float,
    basis: str,
    caveat: str,
) -> PotentialRange | None:
    """Faixa sobre o custo do ativo, ou `None` quando não há custo.

    `None` e zero dizem coisas diferentes e são fáceis de confundir: zero se lê
    como "não há o que ganhar aqui", quando o caso real é "não sei quanto vale".
    """
    if not baseline or baseline <= 0 or fraction <= 0:
        return None
    teto = baseline * min(fraction, 0.5)
    return PotentialRange(
        low=round(teto * _LOW, 2),
        expected=round(teto * _EXPECTED, 2),
        high=round(teto, 2),
        basis=basis,
        caveat=caveat,
    )
