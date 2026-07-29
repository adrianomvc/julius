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

#: Prefixo das aplicações de aquecimento de dado da plataforma — `-warmer-glue`
#: no Glue, `-warm-sfn` no Step Functions, e o que mais vier com essa origem.
#:
#: A âncora no **começo** do nome é o que separa a aplicação da plataforma de um
#: job da conta que apenas menciona o mesmo texto:
#: `analytics-data-warmer-glue-x` é da plataforma;
#: `consumer-avi-analytics-data-warmer-glue` é da conta.
#:
#: E é `analytics-data`, não `analytics-`: o prefixo curto é a convenção de
#: nomenclatura do domínio inteiro, usada também pelos jobs da conta. Ignorar
#: por ele apagaria justamente onde está a economia.
MANAGED_NAME_PREFIXES: tuple[str, ...] = ("analytics-data",)


def is_managed(
    name: str,
    *,
    exact: frozenset[str] = MANAGED_EXACT_NAMES,
    prefixes: tuple[str, ...] = MANAGED_NAME_PREFIXES,
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
    return any(normalizado.startswith(prefix.lower()) for prefix in prefixes)


def _nome(valor) -> str:
    return str(valor or "").strip()


def managed_asset_names(account) -> frozenset[str]:
    """Todo ativo que pertence a um processo da plataforma, não só o processo.

    `is_managed` casa o **nome do processo**. Mas o achado que chega no relatório
    quase nunca se chama assim: é o prefixo S3 do event log daquele job, a tabela
    que ele escreve, o schedule que dispara a máquina de estado dele. Nenhum
    desses nomes casa com `analytics-data`, e todos passavam pelo filtro — a
    aplicação da plataforma saía do ranking pela porta da frente e voltava pela
    janela, como recomendação sobre um artefato dela.

    Vale a mesma regra do módulo: isto **não** tira nada do inventário. O rateio
    da fatura continua contando o consumo deles; o que não sai é recomendação.
    """
    processos = {
        _nome(item.name)
        for grupo in ("glue_jobs", "state_machines", "glue_crawlers", "databrew_jobs")
        for item in getattr(account, grupo, None) or ()
        if is_managed(_nome(item.name))
    }
    if not processos:
        return frozenset()

    derivados: set[str] = set(processos)
    for job in getattr(account, "glue_jobs", None) or ():
        if _nome(job.name) not in processos:
            continue
        for atributo in ("spark_event_logs_path", "script_location"):
            if caminho := _nome(getattr(job, atributo, "")):
                derivados.add(caminho)
    for prefixo in getattr(account, "s3_prefixes", None) or ():
        if _nome(getattr(prefixo, "source_asset", "")) in processos:
            derivados.add(prefixo.location)
    for tabela in getattr(account, "tables", None) or ():
        if _nome(getattr(tabela, "written_by", "")) in processos:
            derivados.add(_nome(tabela.name))
            if local := _nome(getattr(tabela, "location", "")):
                derivados.add(local)
    for agenda in getattr(account, "schedules", None) or ():
        if _nome(getattr(agenda, "target_name", "")) in processos:
            derivados.add(_nome(agenda.name))
    for gatilho in getattr(account, "glue_triggers", None) or ():
        acionados = getattr(gatilho, "job_names", None) or ()
        if any(_nome(alvo) in processos for alvo in acionados):
            derivados.add(_nome(gatilho.name))
    return frozenset(nome for nome in derivados if nome)
