"""Quais bancos do Glue Catalog pertencem à conta analisada.

Numa conta Consumer do Data Mesh o Data Catalog enxerga bancos compartilhados
por outras contas. Percorrê-los custa uma chamada `get_tables` por banco e
produz tabelas sobre as quais esta conta não pode fazer nada: não gera, não
desliga, não redimensiona. O escopo existe para que a coleta pare antes dessa
chamada, e não depois.

São **três** os bancos da conta, e eles não seguem a mesma forma:

- `database_db_compartilhado_consumer_<nome da conta>` — o compartilhado, único
  que carrega o nome da conta;
- `workspace_db` e `sagemaker_featurestore` — nomes fixos, iguais em toda conta.

Por isso a regra não é "termina com o nome da conta": isso deixaria os dois
últimos de fora. É uma lista de nomes locais mais o compartilhado da conta.

A comparação normaliza os dois lados, mas **preserva os separadores**. Colapsar
`-` e `_` a nada tornaria `..._consumer_navi` um casamento válido para a conta
`avi`, porque a cadeia terminaria em `avi` de qualquer jeito. O separador é o
que marca onde o nome da conta começa.

Escopo não informado mantém o comportamento antigo (todos os bancos) e diz isso
na saúde da coleta. Varrer o mesh inteiro é uma escolha legítima num ambiente
sem essa convenção; o que não pode é acontecer sem ninguém saber.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

_SEPARATORS = re.compile(r"[^a-z0-9]+")
_CATALOG_ENVIRONMENT_SUFFIXES: tuple[str, ...] = ("_prod", "_pro")

#: Prefixo do banco compartilhado da conta. O nome da conta vem depois dele.
SHARED_DATABASE_PREFIX = "database_db_compartilhado_consumer_"

#: Bancos da conta com nome fixo — não carregam o nome da conta e por isso
#: nenhuma regra de sufixo os alcança.
LOCAL_DATABASES: tuple[str, ...] = ("workspace_db", "sagemaker_featurestore")


def normalize(value: str) -> str:
    """`dbCompartilhado_Consumer-AVI` → `dbcompartilhado_consumer_avi`.

    Minúsculas e separador único. O separador **fica**: é ele que impede o banco
    de uma conta vizinha de passar pelo desta.
    """
    return _SEPARATORS.sub("_", str(value).lower()).strip("_")


@dataclass(frozen=True)
class CatalogScope:
    """O recorte do catálogo, resolvido uma vez e repassado para baixo."""

    account_name: str = ""
    #: De onde `account_name` veio: `explicit`, `registry`, `aws` ou `profile`.
    #:
    #: Não é metadado decorativo. `profile` significa que o nome é o apelido que
    #: alguém deu ao perfil em `aws configure sso` — uma escolha local, sem
    #: relação com a conta —, e é o único caso em que a coleta pode substituí-lo
    #: pelo nome que a própria AWS informa. Também é o que faz a saúde da coleta
    #: distinguir "nenhum banco casou porque o cadastro está errado" de "nenhum
    #: banco casou porque o nome era um apelido".
    name_source: str = ""
    #: Lista explícita: quando informada, substitui a regra de nome inteira.
    databases: tuple[str, ...] = ()
    #: Os bancos de nome fixo. Campo, e não constante embutida, para um ambiente
    #: com outra convenção não precisar de mudança de código.
    local_databases: tuple[str, ...] = field(default=LOCAL_DATABASES)

    @property
    def declared(self) -> bool:
        return bool(self.databases or self.account_name)

    @property
    def shared_database(self) -> str:
        """O compartilhado desta conta, com o nome já resolvido."""
        if not self.account_name:
            return ""
        # O cadastro às vezes chama a conta de `consumer-avi` e o banco termina
        # em `consumer_avi`: repetir o token produziria
        # `..._consumer_consumer_avi` e não casaria com nada.
        token = normalize(self.account_name)
        if token.startswith("consumer_"):
            token = token.removeprefix("consumer_")
        elif token.startswith("consumer"):
            # Alguns nomes cadastrais juntam o tipo e o nome da conta:
            # `consumeratendimentodataservice-pro`.
            token = token.removeprefix("consumer")
        # O cadastro identifica a conta/ambiente (`consumer-avi-prod`), mas o
        # banco compartilhado identifica somente a conta (`consumer_avi`).
        # Remove apenas sufixo completo: `produto` e `produtos` continuam
        # intactos.
        for suffix in _CATALOG_ENVIRONMENT_SUFFIXES:
            if token.endswith(suffix):
                token = token.removesuffix(suffix)
                break
        return normalize(SHARED_DATABASE_PREFIX) + "_" + token

    @property
    def rule(self) -> str:
        """Como o escopo foi decidido, em uma linha, para a saúde da coleta."""
        if self.databases:
            return f"lista explícita: {', '.join(self.databases)}"
        if self.account_name:
            nomes = [self.shared_database, *self.local_databases]
            return "bancos da conta: " + ", ".join(nomes)
        return "sem escopo declarado: todos os bancos do catálogo"

    def select(self, names: Sequence[str]) -> list[str]:
        """Os bancos que ficam, preservando a ordem em que o catálogo os deu."""
        if self.databases:
            wanted = {normalize(name) for name in self.databases}
            return [name for name in names if normalize(name) in wanted]
        if not self.account_name:
            return list(names)
        wanted = {normalize(name) for name in self.local_databases}
        wanted.add(self.shared_database)
        return [name for name in names if normalize(name) in wanted]
