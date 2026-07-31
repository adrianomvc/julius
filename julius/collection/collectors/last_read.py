"""Quando cada tabela — e cada prefixo S3 — foi lida pela última vez.

**O S3 não tem last access time nativo por objeto.** `LastModified` é a data da
última escrita, e um arquivo gravado uma vez e lido todo dia tem `LastModified`
de um ano atrás. Recomendar Glacier a partir dele trocaria a classe de dado
quente e faria o time dono pagar retrieval para reverter.

As fontes que medem leitura de verdade exigem configuração prévia no bucket
(`collectors/s3_config.py`). Este módulo cobre o caso em que nenhuma delas está
ligada, usando o que o Julius **já** coleta e que não pede permissão nova:

- o histórico de execuções do Athena, que diz quais tabelas cada query leu e
  quando rodou pela última vez (`AthenaQuery.reads_tables` + `last_execution_at`);
- a tabela oficial de toques, quando `--touches-table` está configurada.

O vínculo com o S3 é a `location` da tabela no catálogo — a mesma que já define
o escopo da coleta de S3. Não é evidência por objeto: é evidência de que **o
prefixo inteiro** foi lido, o que é suficiente para *não* recomendar transição,
e insuficiente para afirmar que nada ali é lido. Por isso o resultado limita a
confiança da regra em vez de fabricar uma certeza.
"""

from __future__ import annotations

from typing import Any


def apply_last_read(account: Any) -> int:
    """Preenche `Table.last_read_at` a partir do histórico de queries.

    Devolve quantas tabelas ficaram com data. Só avança a data: a tabela de
    toques, quando existe, já escreveu a dela, e ela é a fonte melhor.
    """
    if not getattr(account, "tables", None):
        return 0
    ultima = _ultima_leitura_por_tabela(account)
    if not ultima:
        medidas = sum(1 for table in account.tables if table.last_read_at)
        _apply_prefix_read_evidence(account)
        return medidas
    medidas = 0
    for table in account.tables:
        candidata, origem = ultima.get(_normalizado(table.name), ("", ""))
        if candidata > table.last_read_at:
            table.last_read_at = candidata
            table.last_read_source = origem
        if table.last_read_at:
            medidas += 1
    _apply_prefix_read_evidence(account)
    return medidas


def _apply_prefix_read_evidence(account: Any) -> None:
    """Publica no contrato S3 a evidência que já existe no catálogo.

    É uma projeção agregada: não afirma quantos GETs ocorreram e não inventa
    cobertura por objeto. Datasets antigos continuam válidos porque os campos
    de `S3Prefix` têm defaults explícitos.
    """
    leituras = last_read_by_prefix(account)
    origens = _origem_por_prefixo(account)
    for prefixo in getattr(account, "s3_prefixes", None) or ():
        quando = last_read_for(prefixo, leituras)
        if not quando or quando <= prefixo.last_read_at:
            continue
        origem = origens.get(quando, "catalog_read_history")
        prefixo.last_read_at = quando
        prefixo.access_source = origem
        # Qualidades diferentes porque as afirmações são diferentes: query
        # observada diz **quando** o prefixo foi lido; execução de job diz que
        # ele **é** consumido, sem garantir que a leitura foi da parte inteira.
        prefixo.access_quality = (
            "process_inferred" if origem == "process_lineage" else "prefix_inferred"
        )
        prefixo.read_coverage_days = int(getattr(account, "window_days", 0) or 0)


def _origem_por_prefixo(account: Any) -> dict[str, str]:
    """A origem de cada data de leitura, indexada pela própria data."""
    return {
        str(getattr(table, "last_read_at", "") or ""): str(
            getattr(table, "last_read_source", "") or ""
        )
        for table in getattr(account, "tables", None) or ()
        if getattr(table, "last_read_at", "")
    }


def _ultima_leitura_por_tabela(account: Any) -> dict[str, tuple[str, str]]:
    """`(quando, origem)` por tabela, da fonte mais forte que tiver data.

    Duas origens, e elas afirmam coisas diferentes. O histórico de query é
    **leitura observada**: alguém consultou a tabela e há registro disso. A
    linhagem é **inferência**: um job que declara ler a tabela rodou naquele
    instante, então o dado é consumido — mas o job pode ler só a partição do
    dia, e a data não é de leitura da tabela inteira.

    A data mais recente vence, porque uma leitura posterior é uma leitura
    posterior venha de onde vier. O que a origem decide é o que a regra pode
    afirmar com ela.
    """
    ultima: dict[str, tuple[str, str]] = {}

    def registrar(nome: Any, quando: str, origem: str) -> None:
        chave = _normalizado(nome)
        if not chave or not quando:
            return
        anterior = ultima.get(chave)
        if anterior is None or quando > anterior[0]:
            ultima[chave] = (quando, origem)

    for query in getattr(account, "athena_queries", None) or ():
        quando = str(getattr(query, "last_execution_at", "") or "")
        for nome in getattr(query, "reads_tables", None) or ():
            registrar(nome, quando, "catalog_read_history")

    for job in getattr(account, "glue_jobs", None) or ():
        quando = str(getattr(job, "last_run_at", "") or "")
        for nome in getattr(job, "reads_tables", None) or ():
            registrar(nome, quando, "process_lineage")

    return ultima


def _normalizado(nome: Any) -> str:
    """`db.tabela` em minúsculas — o catálogo Glue não diferencia caixa."""
    return str(nome or "").strip().lower()


def last_read_by_prefix(account: Any) -> dict[str, str]:
    """Última leitura conhecida de cada prefixo S3, via `location` da tabela.

    A chave é a `location` normalizada sem a barra final, para casar com
    `S3Prefix.location`. Prefixo que não é location de tabela nenhuma fica de
    fora — e ficar de fora significa "não medido", nunca "não lido".
    """
    out: dict[str, str] = {}
    for table in getattr(account, "tables", None) or ():
        quando = str(getattr(table, "last_read_at", "") or "")
        local = str(getattr(table, "location", "") or "").rstrip("/")
        if not quando or not local:
            continue
        if quando > out.get(local, ""):
            out[local] = quando
    return out


def last_read_for(prefix: Any, por_prefixo: dict[str, str]) -> str:
    """A última leitura que se conhece deste prefixo, ou vazio.

    Casa por caminho contido, não por igualdade: a location da tabela é
    `s3://lake/vendas/`, e o prefixo coletado pode ser uma partição dentro dela.
    Ler a tabela é ler a partição.
    """
    local = str(getattr(prefix, "location", "") or "").rstrip("/")
    if not local:
        return ""
    if local in por_prefixo:
        return por_prefixo[local]
    return max(
        (
            quando
            for raiz, quando in por_prefixo.items()
            if local.startswith(raiz + "/")
        ),
        default="",
    )
