"""Coletor de toques via Athena (tabela oficial de acessos).

Consulta a tabela oficial de toques para obter, por tabela, o nº de acessos e
quantas contas/comunidades consomem — o que alimenta os detectores de base
sem/pouco uso. Nomes de tabela e colunas são CONFIGURÁVEIS (o schema real está
na máquina do trabalho); os padrões são um ponto de partida.

Degradação graciosa: se a tabela não estiver disponível/configurada, o coletor
devolve {} e os detectores de dados simplesmente não disparam.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from julius.collection.collectors.athena.query import run_query, validate_identifier
from julius.collection.window import AnalysisWindow


@dataclass
class TouchColumns:
    table: str = "tabela"
    account: str = "conta"
    community: str = "comunidade"
    date: str = "data_acesso"


@dataclass
class TouchStats:
    touches: int = 0
    accounts: int = 0
    communities: int = 0
    account_ids: list[str] = field(default_factory=list)
    primary_community: str | None = None
    #: Data do acesso mais recente na janela. É a melhor evidência de leitura
    #: que o Julius consegue sem configuração no bucket — o S3 não expõe último
    #: acesso por objeto, e `LastModified` é a última escrita.
    last_access: str = ""


def collect_touches(
    athena_client,
    *,
    touches_table: str,
    workgroup: str = "julius",
    output_location: str | None = None,
    window: AnalysisWindow,
    columns: TouchColumns | None = None,
) -> dict[str, TouchStats]:
    if not touches_table:
        return {}
    # O nome vem de `--touches-table` e entra no SQL por interpolação.
    touches_table = validate_identifier(touches_table)
    cols = columns or TouchColumns()
    sql = (
        "WITH base AS ("
        f"SELECT {cols.table} AS tabela, cast({cols.account} AS varchar) AS conta, "
        f"cast({cols.community} AS varchar) AS comunidade, "
        f"{cols.date} AS acesso FROM {touches_table} "
        f"WHERE {cols.date} >= date_add('day', -{int(window.days)}, current_date)"
        "), totals AS ("
        "SELECT tabela, count(*) AS toques, count(distinct conta) AS contas, "
        "count(distinct comunidade) AS comunidades, "
        # O acesso mais recente é o que decide se o dado pode ir para classe
        # fria. A contagem sozinha não distingue "lido ontem" de "lido uma vez,
        # há noventa dias".
        "cast(max(acesso) AS varchar) AS ultimo_acesso, "
        "array_join(array_agg(distinct conta), ',') AS contas_lista "
        "FROM base GROUP BY tabela"
        "), ranked AS ("
        "SELECT tabela, comunidade, row_number() OVER "
        "(PARTITION BY tabela ORDER BY count(*) DESC, comunidade) AS posicao "
        "FROM base GROUP BY tabela, comunidade"
        ") SELECT t.*, r.comunidade AS comunidade_principal "
        "FROM totals t LEFT JOIN ranked r ON t.tabela = r.tabela AND r.posicao = 1"
    )
    rows = run_query(
        athena_client, sql, workgroup=workgroup, output_location=output_location
    )
    stats: dict[str, TouchStats] = {}
    for r in rows:
        name = r.get("tabela")
        if not name:
            continue
        stats[name] = TouchStats(
            touches=_int(r.get("toques")),
            accounts=_int(r.get("contas")),
            communities=_int(r.get("comunidades")),
            account_ids=[
                value.strip()
                for value in str(r.get("contas_lista") or "").split(",")
                if value.strip()
            ],
            primary_community=r.get("comunidade_principal") or None,
            last_access=str(r.get("ultimo_acesso") or ""),
        )
    return stats


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
