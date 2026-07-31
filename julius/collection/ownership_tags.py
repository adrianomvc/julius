"""Dono declarado em tag, lido de qualquer uma das formas que a AWS usa.

Sete pontos da coleta liam tag de dono, em três formas diferentes — dicionário
`{"Owner": ...}` no Glue, lista `[{"Key": ..., "Value": ...}]` no SageMaker e no
Redshift, e um helper próprio no SageMaker de Apps. Os sete tinham o mesmo par de
defeitos: **uma única chave**, comparada com **caixa exata**.

Conta que padroniza `owner`, `Squad`, `Team` ou `CostCenter` ficava inteira sem
dono. E dono não é detalhe cosmético aqui: `check_actionable` exige dono ou ator,
e `assign()` trata `owner is None` como bloqueador de `fazer_agora`. Sem ele o
achado nasce em `investigar_primeiro` por falta de responsável, não por falta de
evidência.

Nada disto altera cifra: resolver dono move achado de fila, não muda um centavo.
"""

from __future__ import annotations

from typing import Any

#: Chaves aceitas como declaração de propriedade, em ordem de precedência.
#: Comparadas sem caixa e ignorando separador, então `cost_center`, `CostCenter`
#: e `cost-center` são a mesma chave. A ordem importa: `Owner` é declaração
#: direta de dono, enquanto `CostCenter` é rateio contábil que costuma apontar
#: para a área certa mas não para o time — fica por último de propósito.
OWNER_TAG_KEYS: tuple[str, ...] = (
    "owner",
    "ownername",
    "squad",
    "team",
    "time",
    "tribe",
    "tribo",
    "businessunit",
    "costcenter",
    "centrodecusto",
)


def _normalizado(chave: str) -> str:
    return "".join(c for c in str(chave).lower() if c.isalnum())


def owner_from_tags(
    tags: Any, *, keys: tuple[str, ...] = OWNER_TAG_KEYS
) -> str | None:
    """Primeiro valor não vazio entre as chaves aceitas, ou `None`.

    Aceita as duas formas em que a AWS devolve tag — dicionário e lista de
    `{Key, Value}` — porque a coleta encontra as duas e nenhuma delas é mais
    correta que a outra.
    """
    achadas = _como_dicionario(tags)
    if not achadas:
        return None
    for chave in keys:
        valor = achadas.get(_normalizado(chave))
        if valor:
            return valor
    return None


def _como_dicionario(tags: Any) -> dict[str, str]:
    itens: list[tuple[Any, Any]]
    if isinstance(tags, dict):
        itens = list(tags.items())
    elif isinstance(tags, (list, tuple)):
        itens = [
            (item.get("Key"), item.get("Value"))
            for item in tags
            if isinstance(item, dict)
        ]
    else:
        return {}
    return {
        _normalizado(chave): str(valor).strip()
        for chave, valor in itens
        if chave and str(valor or "").strip()
    }
