"""Análise é para processo que repete. O que não repete não se ajusta.

Dimensionar worker, ligar Auto Scaling, apertar timeout, trocar Standard por
Express — toda recomendação de ajuste parte de um comportamento observado várias
vezes. Sobre uma execução única não existe média, não existe perfil e não existe
padrão: existe uma amostra. Recomendar a partir dela é opinião com cara de
número, e o produto inteiro se define por não fazer isso.

Por isso o processo sem execução recorrente na janela não gera achado. Mas
sumir em silêncio seria trocar um erro por outro: um job que roda duas vezes por
mês e queima seiscentas DPU-hora é dinheiro real, e some junto. Esse caso vira
**sinal** — hipótese rastreável, sem economia atribuída e fora do ranking, que é
exatamente o estado epistêmico de "custa caro e eu não sei dizer como ajustar".

O limiar de consumo é DPU-hora, não dólar. Todos os limiares deste pacote são
razão, contagem ou bytes; nenhum é moeda, porque moeda depende de região e de
câmbio e mora em `knowledge/pricing`.
"""

from __future__ import annotations

from typing import Any

from julius.collection.settings import DAYS_PER_MONTH

#: Consumo na janela acima do qual um processo não recorrente ainda merece ser
#: olhado. 50 DPU-hora é da ordem de vinte dólares por mês em sa-east-1: abaixo
#: disso, apagar o processo inteiro não paga a atenção de quem leria o achado.
NON_RECURRING_DPU_HOURS_MIN: float = 50.0

#: Fração da janela em que o processo precisa ter ficado ativo para contar como
#: contínuo. Metade é folgado de propósito: um streaming que caiu por alguns
#: dias continua sendo um processo contínuo com um incidente, não um job
#: esporádico.
CONTINUOUS_WINDOW_FRACTION: float = 0.5


def runs_per_month(process: Any) -> float:
    """Execuções por mês do processo, normalizadas pela janela observada.

    A máquina de estado já entrega `executions_per_month`. O job entrega
    execuções na janela, e a janela é configurável (`--lookback-days`) — sem
    normalizar, mudar a janela mudaria quem é recorrente sem que nada no
    ambiente tivesse mudado.
    """
    declarado = getattr(process, "executions_per_month", None)
    if declarado is not None:
        return float(declarado)
    janela = float(getattr(process, "window_days", 0) or 0)
    execucoes = float(getattr(process, "runs_in_window", 0) or 0)
    if janela <= 0:
        return execucoes
    return execucoes * DAYS_PER_MONTH / janela


def is_continuous(process: Any) -> bool:
    """O processo ficou ativo durante boa parte da janela?

    Contar execuções cala o job de streaming: ele roda ininterrupto e aparece
    como **uma** execução na janela, não trinta. Pelo critério de contagem, o
    processo mais caro que uma conta pode ter — DPU 24 por 7 — seria classificado
    como esporádico e teria os achados suprimidos.

    E o motivo pelo qual esporádico não se ajusta some aqui: não falta
    observação sobre um job que rodou o mês inteiro, sobra.
    """
    janela = float(getattr(process, "window_days", 0) or 0)
    ativo = float(getattr(process, "active_seconds_window", 0) or 0)
    if janela <= 0 or ativo <= 0:
        return False
    return ativo >= janela * 86400 * CONTINUOUS_WINDOW_FRACTION


def is_recurrent(process: Any, minimum_per_month: int) -> bool:
    """O processo repete — ou dura — o bastante para um ajuste ser afirmável?"""
    return runs_per_month(process) >= minimum_per_month or is_continuous(process)


def consumption_dpu_hours(process: Any) -> float:
    """DPU-hora consumida na janela; zero quando o processo não a expõe."""
    for attribute in ("total_dpu_hours_window", "dpu_hours_window", "dpu_hours"):
        value = getattr(process, attribute, None)
        if value:
            return float(value)
    return 0.0


def deserves_signal(
    process: Any, *, minimum_dpu_hours: float = NON_RECURRING_DPU_HOURS_MIN
) -> bool:
    """Não recorrente, mas caro o bastante para a pergunta valer a pena."""
    return consumption_dpu_hours(process) >= minimum_dpu_hours
