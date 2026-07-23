"""Executor de query Athena com guardrails (modo collection do plano).

Roda um SELECT num workgroup dedicado do Julius (que deve ter
BytesScannedCutoffPerQuery configurado), espera concluir e devolve as linhas.
Não emite CREATE/INSERT/CTAS/DROP — apenas SELECT.
"""

from __future__ import annotations

import time
from typing import Callable

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class AthenaQueryError(RuntimeError):
    pass


def run_query(
    athena_client,
    sql: str,
    *,
    workgroup: str = "julius",
    output_location: str | None = None,
    timeout_s: float = 60.0,
    poll_interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise AthenaQueryError("Somente SELECT é permitido no modo collection.")

    start_kwargs: dict = {"QueryString": sql, "WorkGroup": workgroup}
    if output_location:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": output_location}
    qid = athena_client.start_query_execution(**start_kwargs)["QueryExecutionId"]

    deadline = time.monotonic() + timeout_s
    while True:
        info = athena_client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = info["Status"]["State"]
        if state in _TERMINAL:
            break
        if time.monotonic() > deadline:
            raise AthenaQueryError(f"Timeout aguardando a query {qid}")
        sleep(poll_interval_s)

    if state != "SUCCEEDED":
        reason = info["Status"].get("StateChangeReason", "")
        raise AthenaQueryError(f"Query {qid} terminou em {state}: {reason}")

    return _rows(athena_client, qid)


def _rows(athena_client, qid: str) -> list[dict]:
    rows: list[dict] = []
    header: list[str] | None = None
    token: str | None = None
    while True:
        kwargs = {"QueryExecutionId": qid}
        if token:
            kwargs["NextToken"] = token
        resp = athena_client.get_query_results(**kwargs)
        result_rows = resp.get("ResultSet", {}).get("Rows", [])
        for i, r in enumerate(result_rows):
            cells = [c.get("VarCharValue") for c in r.get("Data", [])]
            if header is None:
                header = cells
                continue
            rows.append(dict(zip(header, cells)))
        token = resp.get("NextToken")
        if not token:
            break
    return rows
