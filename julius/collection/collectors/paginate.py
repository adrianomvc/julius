"""Paginação que sobrevive a uma permissão negada.

Quatro coletores tinham o mesmo defeito, escrito de forma idêntica:

    try:
        paginator = client.get_paginator("describe_clusters")
        pages = paginator.paginate()
    except Exception:
        return []
    for page in pages:            # <- a chamada HTTP acontece AQUI
        out.extend(page.get(key, []))

`paginate()` é **lazy**: não faz chamada nenhuma, só devolve um iterador. A
requisição sai na primeira iteração, que está fora do bloco. O `except Exception`
ali nunca capturou um `AccessDenied`.

O erro subia até `CollectionRecorder.capture`, que o pega e devolve o default —
então a coleta não parava, mas **a fonte inteira virava vazia**. Um crawler
negado zerava todos os crawlers; um banco negado sob Lake Formation zerava o
catálogo, e com ele o escopo de S3, que é derivado da `location` das tabelas. O
relatório então afirmava que a conta não usa o serviço, que é pior do que dizer
que faltou permissão.

Aqui o `try` envolve a iteração, que é onde o erro de verdade acontece. E o que
já foi lido antes da falha é devolvido: evidência truncada nunca vira zero, a
mesma regra que `s3_evidence.list_objects` segue. Quem chama recebe se a
paginação completou e por que não completou, para que a saúde da coleta consiga
dizer "faltou permissão" em vez de "não havia nada".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.health.recorder import error_category


@dataclass
class Paginated:
    """O que uma listagem paginada devolve, incluindo o que deu errado."""

    items: list[dict] = field(default_factory=list)
    #: Falso quando a paginação parou antes do fim — por erro ou por teto.
    complete: bool = True
    #: Categoria estável do erro (`permission_denied`, `throttled`, …), vazia
    #: quando não houve erro. Nunca a mensagem: ela pode conter nome de recurso.
    error_category: str = ""

    def __bool__(self) -> bool:
        return bool(self.items)


def safe_pages(
    client,
    operation: str,
    key: str,
    *,
    max_pages: int | None = None,
    **kwargs,
) -> Paginated:
    """Itens de uma operação paginada, sem nunca levantar.

    `key` é o campo da resposta que carrega a lista (`Clusters`, `Crawls`,
    `CrawlerMetricsList`). `max_pages` limita o alcance quando listar custa
    dinheiro; atingi-lo marca `complete=False`, que é o lado honesto — erra para
    "pode haver mais".
    """
    if client is None:
        return Paginated(complete=False)

    try:
        pages = client.get_paginator(operation).paginate(**kwargs)
    except Exception:
        # Operação sem paginador neste cliente. Não é erro de AWS: uma chamada
        # única responde a mesma pergunta, e é ela que pode ser negada.
        try:
            resposta = getattr(client, operation)(**kwargs)
        except Exception as exc:
            return Paginated(complete=False, error_category=error_category(exc))
        return Paginated(items=list(resposta.get(key, []) or []))

    itens: list[dict] = []
    try:
        for indice, page in enumerate(pages, start=1):
            itens.extend(page.get(key, []) or [])
            if max_pages is not None and indice >= max_pages:
                return Paginated(items=itens, complete=False)
    except Exception as exc:
        return Paginated(items=itens, complete=False, error_category=error_category(exc))
    return Paginated(items=itens)


def safe_call(client, operation: str, **kwargs) -> tuple[dict, str]:
    """Uma chamada AWS isolada: a resposta, ou vazio e a categoria do erro.

    Para o recurso individual dentro de um laço — o `describe_state_machine` de
    uma máquina, o `list_targets_by_rule` de uma regra. Sem isso, um único
    recurso negado zera a listagem inteira e o relatório passa a afirmar que o
    serviço não é usado nesta conta.

    Recebe o **nome** da operação, e não o método já resolvido, porque o
    `getattr` também precisa estar protegido: um cliente boto3 levanta
    `AttributeError` para operação que a versão instalada não conhece, e
    resolver o método fora do `try` deixaria esse erro escapar — justamente no
    caso das APIs mais novas, que é onde ele acontece.
    """
    if client is None:
        return {}, "not_configured"
    try:
        return getattr(client, operation)(**kwargs) or {}, ""
    except Exception as exc:
        return {}, error_category(exc)
