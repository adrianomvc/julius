"""Reprocessamento medido de Glue Job com bookmark desligado.

`bookmark_saving` exige duas evidências para emitir cifra:
`incremental_source_evidence` e `redundant_read_bytes_window`. Os dois campos
existiam no modelo, eram lidos pela regra — e **nenhum coletor os escrevia**.
A regra nunca pôde produzir número, e nada acusava isso: o resultado era um
achado permanentemente sem economia, indistinguível de um em que a economia
fosse realmente zero.

Ambos saem de dado que já é coletado, cruzado aqui:

- a fonte é incremental quando o prefixo que o job lê é particionado por data
  (`S3Prefix.date_partitioned`, derivado das chaves dentro do coletor de S3);
- o redundante é o que foi lido além do tamanho da fonte
  (`GlueJob.bytes_read_window`, do CloudWatch de observabilidade, menos o
  `total_bytes` dos prefixos de origem).

O raciocínio do segundo: com bookmark ligado, uma passada lê a fonte uma vez e
depois só o que cresceu. Com ele desligado, cada execução relê tudo. Então ler
muito mais do que a fonte inteira mede, sozinho, quanto foi relido.

Três condições, todas necessárias, e nenhuma delas é conservadorismo decorativo:

1. **Toda** tabela lida precisa ter prefixo conhecido. `bytes_read_window` é a
   soma de tudo que o job leu; se uma das fontes estiver fora do inventário, a
   subtração atribui a leitura dela a reprocessamento que não houve.
2. Toda listagem precisa estar completa. Prefixo truncado dá tamanho que é piso,
   e um piso no subtraendo vira redundância superestimada.
3. A fonte precisa ser incremental. Reler uma tabela reescrita por inteiro a
   cada execução não é desperdício — é o único jeito de lê-la.
"""

from __future__ import annotations

from typing import Any


def apply_redundant_reads(account: Any) -> int:
    """Preenche a evidência de reprocessamento. Devolve quantos jobs mediu."""
    prefixos = _prefixos_de_tabela(account)
    medidos = 0
    for job in getattr(account, "glue_jobs", None) or ():
        fontes = _fontes_do_job(job, prefixos)
        if fontes is None:
            continue
        job.incremental_source_evidence = any(
            prefixo.date_partitioned for prefixo in fontes
        )
        if not job.incremental_source_evidence:
            continue
        if job.bytes_read_window is None or job.bytes_read_window <= 0:
            continue
        if not all(prefixo.listing_complete for prefixo in fontes):
            continue
        tamanho_fonte = sum(float(prefixo.total_bytes or 0) for prefixo in fontes)
        if tamanho_fonte <= 0:
            continue
        job.redundant_read_bytes_window = max(
            0.0, float(job.bytes_read_window) - tamanho_fonte
        )
        medidos += 1
    return medidos


def _prefixos_de_tabela(account: Any) -> dict[str, Any]:
    """O prefixo S3 de cada tabela, pela `location` que o catálogo já resolveu."""
    out: dict[str, Any] = {}
    for prefixo in getattr(account, "s3_prefixes", None) or ():
        if prefixo.kind == "table_location" and prefixo.source_asset:
            out.setdefault(prefixo.source_asset, prefixo)
    return out


def _fontes_do_job(job: Any, prefixos: dict[str, Any]) -> list[Any] | None:
    """Prefixos das tabelas lidas, ou `None` se alguma não for conhecida.

    Tudo ou nada de propósito: medir redundância com parte das fontes fora do
    inventário atribuiria a leitura delas a reprocessamento inexistente.
    """
    tabelas = list(getattr(job, "reads_tables", None) or ())
    if not tabelas:
        return None
    fontes = [prefixos[tabela] for tabela in tabelas if tabela in prefixos]
    if len(fontes) != len(tabelas):
        return None
    return fontes
