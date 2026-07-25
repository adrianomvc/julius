"""Deriva arestas de linhagem a partir do inventário normalizado."""

from __future__ import annotations

import re

from julius.collection.models import Account
from julius.graph.assets import AssetKey
from julius.graph.edges import Edge, EdgeType


def build_lineage(account: Account) -> list[Edge]:
    edges: list[Edge] = []
    account_id = account.account_id

    for job in account.glue_jobs:
        job_key = AssetKey(account_id, "glue_job", job.name)
        for table_name in job.reads_tables:
            edges.append(
                Edge(
                    job_key,
                    AssetKey(account_id, "table", table_name),
                    EdgeType.JOB_READS_TABLE,
                    "configuração/script do Glue Job",
                    0.9,
                )
            )
        writes = set(job.writes_tables)
        writes.update(t.name for t in account.tables if t.written_by == job.name)
        for table_name in sorted(writes):
            edges.append(
                Edge(
                    job_key,
                    AssetKey(account_id, "table", table_name),
                    EdgeType.JOB_WRITES_TABLE,
                    "linhagem declarada ou written_by do catálogo",
                    0.95,
                )
            )

    for query in account.athena_queries:
        query_key = AssetKey(account_id, "athena_query", query.query_id)
        table_names = query.reads_tables or _tables_from_sql(query.statement)
        for table_name in table_names:
            edges.append(
                Edge(
                    query_key,
                    AssetKey(account_id, "table", table_name),
                    EdgeType.QUERY_READS_TABLE,
                    "tabelas extraídas da consulta Athena",
                    0.9,
                )
            )
    return edges


def _tables_from_sql(statement: str) -> list[str]:
    """Extrai referências simples de FROM/JOIN; parser completo entra se necessário."""
    matches = re.findall(
        r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.-]*)",
        statement or "",
        flags=re.IGNORECASE,
    )
    return list(dict.fromkeys(name.strip('"') for name in matches))
