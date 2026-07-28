"""Quais bancos do Glue Catalog pertencem à conta analisada.

Numa conta Consumer do Data Mesh o Data Catalog enxerga bancos compartilhados
por outras contas. Percorrê-los custa uma chamada `get_tables` por banco e
produz tabelas sobre as quais esta conta não pode fazer nada: não gera, não
desliga, não redimensiona. O escopo existe para que a coleta pare antes dessa
chamada, e não depois.

A convenção do ambiente é que o banco compartilhado termina com o nome da conta
(`dbcompartilhado_consumer-avi`). A comparação normaliza os dois lados, então
`-`, `_` e maiúsculas não decidem nada. Um banco compartilhado por outra conta
termina com o nome *dela* e cai fora pela mesma regra — não é preciso uma
segunda verificação.

Escopo não informado mantém o comportamento antigo (todos os bancos) e diz isso
na saúde da coleta. Varrer o mesh inteiro é uma escolha legítima em um ambiente
sem a convenção; o que não pode é acontecer sem ninguém saber.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def normalize(value: str) -> str:
    """`dbCompartilhado_Consumer-AVI` → `dbcompartilhadoconsumeravi`."""
    return _NOT_ALPHANUMERIC.sub("", str(value).lower())


@dataclass(frozen=True)
class CatalogScope:
    """O recorte do catálogo, resolvido uma vez e repassado para baixo."""

    account_name: str = ""
    #: Lista explícita: quando informada, substitui a regra de sufixo.
    databases: tuple[str, ...] = ()

    @property
    def declared(self) -> bool:
        return bool(self.databases or self.account_name)

    @property
    def rule(self) -> str:
        """Como o escopo foi decidido, em uma linha, para a saúde da coleta."""
        if self.databases:
            return f"lista explícita: {', '.join(self.databases)}"
        if self.account_name:
            return f"bancos terminados em '{self.account_name}'"
        return "sem escopo declarado: todos os bancos do catálogo"

    def select(self, names: Sequence[str]) -> list[str]:
        """Os bancos que ficam, preservando a ordem em que o catálogo os deu."""
        if self.databases:
            wanted = {normalize(name) for name in self.databases}
            return [name for name in names if normalize(name) in wanted]
        if self.account_name:
            suffix = normalize(self.account_name)
            return [name for name in names if normalize(name).endswith(suffix)]
        return list(names)
