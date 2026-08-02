"""Travessia da Amazon States Language, compartilhada entre coleta e regras.

A ASL é uma árvore, não uma lista: `Parallel` guarda ramos em `Branches`, e
`Map`/`DistributedMap` guardam o corpo em `ItemProcessor` (ou `Iterator`, na
sintaxe anterior). Um estado dentro de um `Map` é tão cobrado quanto um estado
no topo, então qualquer análise que só olhe `definition["States"]` enxerga
metade da máquina — e a metade que ela perde é justamente a que multiplica
transições.

O walker morava privado no coletor, que o usava para achar chamadas a Glue e
contar `MaxAttempts`. Ele sai daqui porque as regras precisam da mesma
travessia, e `knowledge` importa `collection` em todo o repositório, nunca o
contrário: a única casa que não inverte a direção do import é esta.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def parse_definition(value: str | dict | None) -> dict:
    """A definição como dicionário, ou vazio quando ilegível.

    ASL chega como texto (`describe_state_machine`) ou já decodificada (bundle
    de artefatos). Definição inválida devolve `{}` em vez de erro: uma máquina
    cuja ASL não parseia é uma máquina sem análise, não uma coleta interrompida.
    """
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def walk_states(definition: dict) -> Iterator[dict]:
    """Cada estado da definição, descendo em ramos e sub-processadores."""
    for state in (definition.get("States") or {}).values():
        if not isinstance(state, dict):
            continue
        yield state
        for branch in state.get("Branches") or ():
            if isinstance(branch, dict):
                yield from walk_states(branch)
        for chave in ("ItemProcessor", "Iterator"):
            sub = state.get(chave)
            if isinstance(sub, dict):
                yield from walk_states(sub)


def named_states(definition: dict) -> Iterator[tuple[str, dict]]:
    """Como `walk_states`, mas com o nome — a âncora de evidência da ASL.

    Um achado em JSON minificado não tem linha útil para apontar. O que
    identifica o trecho para quem vai corrigir é o nome do estado, e ele só
    existe como chave do dicionário que contém o estado.

    Nome de estado é único apenas dentro do seu escopo: dois ramos de um
    `Parallel` podem ter um `Validate` cada. O prefixo do estado que os contém
    desfaz a ambiguidade sem inventar identificador que não existe na ASL.
    """
    yield from _named(definition, prefixo="")


def _named(definition: dict, *, prefixo: str) -> Iterator[tuple[str, dict]]:
    for nome, state in (definition.get("States") or {}).items():
        if not isinstance(state, dict):
            continue
        completo = f"{prefixo}{nome}"
        yield completo, state
        for indice, branch in enumerate(state.get("Branches") or ()):
            if isinstance(branch, dict):
                yield from _named(branch, prefixo=f"{completo}[{indice}].")
        for chave in ("ItemProcessor", "Iterator"):
            sub = state.get(chave)
            if isinstance(sub, dict):
                yield from _named(sub, prefixo=f"{completo}.")


def resource_of(state: Any) -> str:
    """O `Resource` do estado em minúsculas, ou vazio se não for um `Task`."""
    if not isinstance(state, dict):
        return ""
    return str(state.get("Resource") or "").lower()


#: Integrações que a AWS cobra por transição e que oferecem o padrão `.sync`:
#: chamar sem ele obriga a máquina a perguntar "já terminou?" num laço, e cada
#: pergunta é uma transição cobrada.
_SYNC_CAPABLE = (
    "glue:startjobrun",
    "ecs:runtask",
    "batch:submitjob",
    "elasticmapreduce:addjobflowsteps",
    "sagemaker:createtrainingjob",
    "sagemaker:createtransformjob",
    "sagemaker:createprocessingjob",
    "states:startexecution",
    "databrew:startjobrun",
)

#: Efeito externo que a reexecução repetiria. A lista é de recursos cuja
#: duplicação é observável **fora** do Step Functions — não de qualquer Task.
_SIDE_EFFECTS = {
    "sns:publish": "ConditionExpression",
    "ses:sendemail": "",
    "ses:sendtemplatedemail": "",
    "sqs:sendmessage": "MessageDeduplicationId",
    "dynamodb:putitem": "ConditionExpression",
    "dynamodb:updateitem": "ConditionExpression",
    "eventbridge:putevents": "",
    "events:putevents": "",
}

#: Padrões que o Express não suporta. Não é preferência: a API recusa.
_EXPRESS_BLOCKERS = (".sync", ".waitfortasktoken")

#: Teto de duração de uma execução Express, em segundos.
_EXPRESS_MAX_DURATION_SEC = 300

#: Os padrões que `scan_patterns` reconhece, e o que cada um afirma. O texto
#: mora aqui, junto da detecção, para o detector e a frase não divergirem.
ASL_PATTERNS = {
    "manual_polling": (
        "integração com padrão .sync disponível consultada em laço de espera"
    ),
    "retry_unbounded": "Retry com muitas tentativas e sem espaçamento entre elas",
    "catch_swallow": "Catch conduz direto a Succeed: a falha vira sucesso",
    "unbounded_fanout": "Map sem MaxConcurrency: paralelismo sem teto declarado",
    "external_side_effect": (
        "efeito externo sem chave de deduplicação declarada na definição"
    ),
}


def scan_patterns(definition: dict) -> dict[str, list[str]]:
    """Padrões de custo na definição, mapeados aos estados que os produzem.

    O Standard cobra por transição, então a ASL é fonte financeira direta: um
    `Wait` em laço, um `Retry` sem espaçamento e um `Map` sem teto são
    transições cobradas antes de qualquer execução acontecer.

    O que a definição **não** diz é intenção, e o scanner para aí. Que um
    `sns:publish` exista é fato; se reexecutá-lo duplicaria algo que importa é
    julgamento sobre o negócio. Por isso a saída aqui é fato bruto — quem
    transforma em pergunta é a camada de regras.
    """
    estados = list(named_states(definition))
    encontrados: dict[str, list[str]] = {}

    def registrar(padrao: str, nomes: list[str]) -> None:
        if nomes:
            encontrados[padrao] = sorted(set(nomes))

    if any(state.get("Type") == "Wait" for _, state in estados):
        registrar(
            "manual_polling",
            [
                nome
                for nome, state in estados
                if any(alvo in resource_of(state) for alvo in _SYNC_CAPABLE)
                and ".sync" not in resource_of(state)
            ],
        )
    registrar(
        "retry_unbounded",
        [nome for nome, state in estados if _retry_sem_espacamento(state)],
    )
    registrar(
        "catch_swallow",
        [nome for nome, state in estados if _catch_para_sucesso(state, estados)],
    )
    registrar(
        "unbounded_fanout",
        [
            nome
            for nome, state in estados
            if str(state.get("Type") or "") == "Map" and not state.get("MaxConcurrency")
        ],
    )
    registrar(
        "external_side_effect",
        [nome for nome, state in estados if _efeito_externo_sem_dedup(state)],
    )
    return encontrados


def express_blockers(definition: dict) -> list[str]:
    """Por que esta definição não roda em Express — vazio quando roda.

    Não é sinal, é supressão. Propor uma migração que a AWS recusa gasta a
    credibilidade do relatório inteiro, e o motivo cabe numa frase que a própria
    definição já entrega.
    """
    motivos: list[str] = []
    for nome, state in named_states(definition):
        resource = resource_of(state)
        motivos += [
            f"{nome} usa {bloqueador}"
            for bloqueador in _EXPRESS_BLOCKERS
            if bloqueador in resource
        ]
        timeout = state.get("TimeoutSeconds")
        if isinstance(timeout, int) and timeout > _EXPRESS_MAX_DURATION_SEC:
            motivos.append(f"{nome} declara TimeoutSeconds={timeout}")
    topo = definition.get("TimeoutSeconds")
    if isinstance(topo, int) and topo > _EXPRESS_MAX_DURATION_SEC:
        motivos.append(f"a máquina declara TimeoutSeconds={topo}")
    return sorted(set(motivos))


def _retry_sem_espacamento(state: dict) -> bool:
    """Muitas tentativas coladas: repaga o trabalho sem dar tempo de recuperar.

    `BackoffRate` ausente vale 2.0 pela especificação da ASL, então só conta
    quem declarou 1.0 ou menos — quem desligou o espaçamento de propósito.
    """
    for retry in state.get("Retry") or ():
        if not isinstance(retry, dict):
            continue
        try:
            tentativas = int(retry.get("MaxAttempts", 3))
            backoff = float(retry.get("BackoffRate", 2.0))
        except (TypeError, ValueError):
            continue
        if tentativas >= 5 and backoff <= 1.0:
            return True
    return False


def _catch_para_sucesso(state: dict, estados: list[tuple[str, dict]]) -> bool:
    por_nome = {nome.rsplit(".", 1)[-1]: item for nome, item in estados}
    for catch in state.get("Catch") or ():
        if not isinstance(catch, dict):
            continue
        destino = por_nome.get(str(catch.get("Next") or ""))
        if isinstance(destino, dict) and destino.get("Type") == "Succeed":
            return True
    return False


def _efeito_externo_sem_dedup(state: dict) -> bool:
    resource = resource_of(state)
    parametros = str(state.get("Parameters") or {})
    return any(
        chave in resource and not (dedup and dedup in parametros)
        for chave, dedup in _SIDE_EFFECTS.items()
    )

