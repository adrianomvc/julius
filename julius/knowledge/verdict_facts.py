"""A porta de entrada do que um veredito escreve no inventário.

A lógica e o catálogo moram em `semantic_facts.py`. Este módulo continua sendo o
nome que o pipeline chama, e a razão de ele existir separado é a mesma de sempre:
`pipeline.analyze_account` aplica vereditos **antes** de as regras rodarem, porque
um fato julgado é condição de entrada delas — aplicá-lo depois deixaria a regra
decidir sobre o inventário velho.

O que mudou é que "qual campo um veredito pode escrever" deixou de ser um `if`
com um `rule_id` dentro e passou a ser uma entrada de catálogo, com a justificativa
de por que aquele campo não é coletável. A diferença aparece na segunda entrada,
não na primeira: sem catálogo, o segundo fato entra como mais um `if`, e ninguém
percebe que ele sobrescreve algo que a API já responde.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from julius.collection.models import Account
from julius.knowledge.semantic_facts import apply_confirmed


def apply_verdicts(account: Account, decisions: Iterable[Any]) -> list[str]:
    """Escreve no inventário o que já foi julgado. Devolve o que mudou."""
    return apply_confirmed(account, decisions)
