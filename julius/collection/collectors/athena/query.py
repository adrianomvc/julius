"""Executor de query Athena com guardrails (modo collection do plano).

**Esta é a única operação do Julius que age**, e por isso o arquivo inteiro
existe para limitá-la. Ele roda um SELECT num workgroup dedicado, espera
concluir e devolve as linhas. Não altera dado, mas custa bytes varridos e grava
o resultado em S3 — o suficiente para não ser leitura passiva.

A verificação era só de prefixo: bastava a string começar com `select` para
passar. Prefixo não é gramática. Agora, além do início, qualquer palavra-chave
que escreva, crie ou remova barra a execução, e o identificador da tabela é
validado antes de entrar no SQL — ele vem de `--touches-table`, e interpolar
texto de fora numa consulta sem verificar é como esse tipo de coisa começa.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}

#: Qualquer uma destas, em qualquer posição, impede a execução. A lista é de
#: negação de propósito: ela complementa a exigência de que a consulta comece
#: com SELECT/WITH, que já é a garantia principal.
_PALAVRAS_PROIBIDAS = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|merge|grant|revoke|"
    r"msck|repair|unload|vacuum|optimize)\b",
    re.IGNORECASE,
)

#: `catalogo.schema.tabela`, com aspas duplas opcionais. Nada além disso entra
#: numa consulta por interpolação.
_IDENTIFICADOR = re.compile(r'^"?[A-Za-z_][\w-]*"?(\."?[A-Za-z_][\w-]*"?){0,2}$')


class AthenaQueryError(RuntimeError):
    pass


def validate_identifier(nome: str) -> str:
    """Aceita só um nome de tabela qualificado, e devolve o mesmo nome.

    O valor vem da linha de comando e é interpolado no SQL. O Athena não
    executa múltiplas instruções, então o risco não é um DROP escondido — é uma
    consulta malformada custando uma varredura. Validar é mais barato que
    descobrir depois.
    """
    limpo = (nome or "").strip()
    if not limpo or not _IDENTIFICADOR.match(limpo):
        raise AthenaQueryError(
            f"identificador de tabela inválido: {nome!r}. "
            "Use catalogo.schema.tabela, sem espaços nem pontuação extra."
        )
    return limpo


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
    if (proibida := _PALAVRAS_PROIBIDAS.search(sql)) is not None:
        raise AthenaQueryError(
            f"palavra-chave de escrita na consulta: {proibida.group(0)!r}. "
            "O Julius analisa e recomenda; alterar dado é ação do time dono."
        )

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
        for r in result_rows:
            cells = [c.get("VarCharValue") for c in r.get("Data", [])]
            if header is None:
                header = cells
                continue
            # Linha mais curta que o cabeçalho é tolerada: o Athena omite a
            # célula quando o valor é nulo em alguns formatos de resultado.
            rows.append(dict(zip(header, cells, strict=False)))
        token = resp.get("NextToken")
        if not token:
            break
    return rows
