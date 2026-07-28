"""Processos instalados pela plataforma, que a conta não pode alterar.

Algumas aplicações chegam na conta Consumer prontas: monitoria, aquecimento de
dado, coisas que a plataforma instala e mantém. O time dono da conta não tem
poder de alterar a infraestrutura delas nem de dar manutenção — e uma
recomendação que ninguém pode executar não é economia, é ruído competindo por
posição no ranking com o que dá para fazer.

**Eles continuam sendo coletados.** A tentação é filtrá-los na coleta, e isso
quebraria a atribuição de custo de todos os outros: `glue/cost.allocate_costs`
rateia a fatura real entre os jobs proporcionalmente à DPU-hora de cada um. Tirar
um job do inventário não tira o consumo dele da fatura — só redistribui a parte
dele sobre os demais, inflando o custo de quem sobrou. O inventário fica
completo; o que não sai é recomendação.
"""

from __future__ import annotations

#: Nome exato. `analytics-gluejob-mdp-custom-metrics` é a monitoria da
#: plataforma e existe em todas as contas.
MANAGED_EXACT_NAMES: frozenset[str] = frozenset(
    {
        "analytics-gluejob-mdp-custom-metrics",
    }
)

#: Sufixos: o prefixo varia por conta, a terminação identifica a aplicação.
MANAGED_NAME_SUFFIXES: tuple[str, ...] = (
    "analytics-data-warmer-glue",   # aquecimento de dado (Glue)
    "analytics-data-warm-sfn",      # aquecimento de dado (Step Functions)
)


def is_managed(
    name: str,
    *,
    exact: frozenset[str] = MANAGED_EXACT_NAMES,
    suffixes: tuple[str, ...] = MANAGED_NAME_SUFFIXES,
) -> bool:
    """O processo pertence à plataforma e não à conta que o hospeda?

    A comparação é sobre o nome cru, em minúsculas: o nome do recurso na AWS é
    o próprio identificador e não passa por normalização em lugar nenhum.
    """
    normalizado = str(name or "").strip().lower()
    if not normalizado:
        return False
    if normalizado in {item.lower() for item in exact}:
        return True
    return any(normalizado.endswith(suffix.lower()) for suffix in suffixes)
