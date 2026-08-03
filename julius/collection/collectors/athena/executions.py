"""Listagem e leitura das execuções de query, por workgroup.

É a única parte que chama a API do Athena; o resto do pacote trabalha
sobre a evidência que sai daqui."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from julius.collection.collectors.athena.evidence import (
    AthenaExecutionEvidence,
    billable_bytes,
    fingerprints,
)
from julius.collection.collectors.s3_evidence import list_objects, parse_location
from julius.collection.models import AthenaCoverage
from julius.collection.session import ATHENA_QUERY_BATCH_WORKERS

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - instalação incompleta tem fallback seguro
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]

_GB = 1024**3
_NONDETERMINISTIC_FUNCTIONS = {
    "current_date",
    "current_time",
    "current_timestamp",
    "localtime",
    "localtimestamp",
    "now",
    "rand",
    "random",
    "shuffle",
    "uuid",
}


def workgroups(client, coverage: AthenaCoverage, telemetry) -> tuple[list[str], dict[str, dict]]:
    names: list[str] = []
    configs: dict[str, dict] = {}
    try:
        paginator = client.get_paginator("list_work_groups")
        for page in paginator.paginate():
            names.extend(item["Name"] for item in page.get("WorkGroups", []) if item.get("Name"))
    except Exception as exc:
        telemetry.failed("Athena API", exc, detail="list_work_groups")
        names = ["primary"]  # compatibilidade com mocks e permissões legadas
    names = list(dict.fromkeys(names))
    coverage.workgroups = names
    coverage.workgroups_total = len(names)
    for name in names:
        try:
            configs[name] = client.get_work_group(WorkGroup=name).get("WorkGroup", {})
            cfg = configs[name].get("Configuration", {})
            if not cfg.get("PublishCloudWatchMetricsEnabled", False):
                telemetry.unavailable("Athena CloudWatch", category="not_configured", detail=f"{name}: métricas desabilitadas no workgroup")
            # A mesma resposta traz onde os resultados de query se acumulam e se
            # existe teto de bytes por query. Ambos vinham sendo descartados.
            saida = str(
                (cfg.get("ResultConfiguration") or {}).get("OutputLocation") or ""
            )
            if saida:
                coverage.workgroup_output_locations[name] = saida
            coverage.workgroup_scan_cutoffs[name] = cfg.get(
                "BytesScannedCutoffPerQuery"
            )
        except Exception as exc:
            telemetry.failed("Athena API", exc, detail=f"{name}: get_work_group")
    return names, configs


def execution_ids(client, workgroup: str, *, max_ids: int | None, telemetry):
    ids: list[str] = []
    truncated = False
    try:
        paginator = client.get_paginator("list_query_executions")
        try:
            pages = paginator.paginate(WorkGroup=workgroup)
        except TypeError:
            pages = paginator.paginate()  # mocks/inventários anteriores a workgroups
        for page in pages:
            ids.extend(page.get("QueryExecutionIds", []))
            if max_ids is not None and len(ids) >= max_ids:
                truncated = True
                ids = ids[:max_ids]
                break
        return ids, truncated
    except Exception as exc:
        telemetry.failed("Athena API", exc, detail=f"{workgroup}: list_query_executions")
        return None, False


#: O Athena nomeia o resultado de cada query pelo próprio `QueryExecutionId`:
#: `<id>.csv` e `<id>.csv.metadata` para SELECT, `<id>.txt` para DDL, às vezes
#: sob subpasta de data. É por isso que os IDs sobrevivem à perda do
#: `ListQueryExecutions` — eles estão nos nomes dos objetos.
_RESULT_KEY = re.compile(
    r"(?:^|/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:\.[A-Za-z]+)*$"
)


def execution_ids_from_results(
    s3_client,
    location: str,
    *,
    modified_after=None,
    max_ids: int | None,
    telemetry,
) -> tuple[list[str] | None, bool]:
    """IDs recuperados do output location, quando a listagem é negada.

    `ListQueryExecutions` é permissão à parte de `BatchGetQueryExecution`, e
    negá-la zerava a fonte inteira: sem IDs não há o que buscar, e o Athena da
    conta aparecia como se ninguém o usasse. Mas o `ProcessedBytes` de cada query
    vem da resposta da própria API, não do CloudWatch — então basta recuperar os
    IDs por outro caminho para a medição voltar.

    O alcance é diferente e precisa ser dito: só aparece aqui a query que gravou
    resultado, o bucket pode ter lifecycle apagando resultado antigo, e a
    listagem custa requests de LIST. Por isso o resultado é marcado como origem
    própria em vez de se passar pela listagem oficial.
    """
    if s3_client is None or not location:
        return None, False
    alvo = parse_location(location)
    if alvo is None:
        return None, False
    bucket, prefix = alvo
    objetos, completa = list_objects(
        s3_client,
        bucket,
        prefix,
        modified_after=modified_after,
        max_objects=max_ids,
    )
    if not objetos and not completa:
        telemetry.unavailable(
            "Athena API",
            category="permission_denied",
            detail=f"{location}: listagem do output location não completou",
        )
        return None, False

    vistos: dict[str, None] = {}
    for item in objetos:
        encontrado = _RESULT_KEY.search(str(item.get("Key") or ""))
        if encontrado:
            vistos.setdefault(encontrado.group(1), None)
        if max_ids is not None and len(vistos) >= max_ids:
            return list(vistos)[:max_ids], True
    # Listagem cortada significa "pode haver mais execuções", não "não havia".
    return list(vistos), not completa


def query_executions(
    client,
    ids: list[str],
    telemetry,
    *,
    workers: int = ATHENA_QUERY_BATCH_WORKERS,
):
    """Resolve lotes de 50 em paralelo, preservando a ordem dos IDs.

    `BatchGetQueryExecution` limita cada chamada a 50 IDs. Em contas com muito
    uso, pagar a latência desses lotes em série domina a fonte inteira. Os
    lotes são independentes e `pool.map` mantém a ordem de entrada; portanto o
    paralelismo não muda o dataset produzido.

    IDs que o batch devolve como não processados recebem uma tentativa isolada
    com `GetQueryExecution`. Isso recupera falhas transitórias sem refazer as 50
    execuções que já vieram corretamente.
    """
    chunks = [ids[index : index + 50] for index in range(0, len(ids), 50)]
    if not chunks:
        return
    if workers <= 1 or len(chunks) == 1:
        resolved = [_query_chunk(client, chunk) for chunk in chunks]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
            resolved = list(pool.map(lambda chunk: _query_chunk(client, chunk), chunks))

    for rows, unresolved, failures in resolved:
        yield from rows
        if unresolved:
            telemetry.unavailable(
                "Athena API",
                category="partial_data",
                detail=f"{unresolved} execuções não processadas",
            )
        for exc in failures:
            telemetry.failed("Athena API", exc, detail="get_query_execution")


def _query_chunk(client, ids: list[str]) -> tuple[list[dict], int, list[Exception]]:
    """Um lote, sem escrever telemetria compartilhada dentro da thread."""
    rows: list[dict] = []
    pending = list(ids)
    failures: list[Exception] = []
    try:
        response = client.batch_get_query_execution(QueryExecutionIds=ids)
        rows.extend(response.get("QueryExecutions", []))
        unprocessed = response.get("UnprocessedQueryExecutionIds", []) or []
        pending = [
            str(item.get("QueryExecutionId") or "")
            for item in unprocessed
            if isinstance(item, dict) and item.get("QueryExecutionId")
        ]
    except Exception:
        # Alguns ambientes autorizam Get mas não BatchGet.
        pass

    for query_id in pending:
        try:
            rows.append(
                client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
            )
        except Exception as exc:  # noqa: BLE001 - devolvida ao agregador
            failures.append(exc)

    by_id = {
        str(row.get("QueryExecutionId") or ""): row
        for row in rows
        if row.get("QueryExecutionId")
    }
    ordered = [by_id[query_id] for query_id in ids if query_id in by_id]
    return ordered, len(ids) - len(ordered), failures


def execution(qe: dict, workgroup: dict) -> AthenaExecutionEvidence | None:
    status = qe.get("Status") or {}
    submitted = status.get("SubmissionDateTime")
    if not isinstance(submitted, datetime):
        return None
    submitted = submitted.replace(tzinfo=submitted.tzinfo or timezone.utc).astimezone(timezone.utc)
    stats = qe.get("Statistics") or {}
    reuse = bool((stats.get("ResultReuseInformation") or {}).get("ReusedPreviousResult"))
    reuse_config = (
        (qe.get("ResultReuseConfiguration") or {}).get(
            "ResultReuseByAgeConfiguration"
        )
        or {}
    )
    state = str(status.get("State") or "UNKNOWN").upper()
    statement_type = str(qe.get("StatementType") or "").upper()
    scanned = int(stats.get("DataScannedInBytes") or 0)
    exact, structural, sanitized, parsed = fingerprints(str(qe.get("Query") or ""))
    modality = resolve_modality(qe, workgroup)
    sql = str(qe.get("Query") or "")
    reuse_eligible, reuse_reasons = result_reuse_eligibility(
        sql,
        statement_type,
        modality,
        managed_results=bool(
            (
                (workgroup.get("Configuration") or {}).get(
                    "ManagedQueryResultsConfiguration"
                )
                or {}
            ).get("Enabled")
        ),
    )
    return AthenaExecutionEvidence(
        query_execution_id=str(qe.get("QueryExecutionId") or ""),
        workgroup=str(qe.get("WorkGroup") or workgroup.get("Name") or "primary"),
        submitted_at=submitted,
        state=state,
        statement_type=statement_type,
        raw_sql=sql,
        exact_fingerprint=exact,
        structural_fingerprint=structural,
        sanitized_sql=sanitized,
        parse_succeeded=parsed,
        scanned_bytes=scanned,
        billed_bytes=billable_bytes(scanned, state=state, statement_type=statement_type, reused=reuse)
        if modality == "on_demand" else 0,
        duration_ms=int(stats.get("EngineExecutionTimeInMillis") or stats.get("TotalExecutionTimeInMillis") or 0),
        planning_ms=int(stats.get("QueryPlanningTimeInMillis") or 0),
        reused=reuse,
        reuse_configured=bool(reuse_config.get("Enabled")),
        reuse_max_age_minutes=(
            int(reuse_config["MaxAgeInMinutes"])
            if reuse_config.get("MaxAgeInMinutes") is not None
            else None
        ),
        reuse_eligible=reuse_eligible,
        reuse_ineligible_reasons=reuse_reasons,
        modality=modality,
        **ast_facts(sql),
    )


def resolve_modality(qe: dict, workgroup: dict) -> str:
    engine = str((qe.get("EngineVersion") or {}).get("SelectedEngineVersion") or "").lower()
    configuration = workgroup.get("Configuration", {})
    if "spark" in engine:
        return "spark"
    catalog = str((qe.get("QueryExecutionContext") or {}).get("Catalog") or "").lower()
    if (
        qe.get("SubstatementType") == "FEDERATED"
        or "federated" in engine
        or "lambda" in catalog
    ):
        return "federated"
    if any(
        configuration.get(key)
        for key in (
            "CapacityReservation",
            "CapacityReservationName",
            "CapacityReservationConfiguration",
        )
    ):
        return "provisioned"
    return "on_demand"


def ast_facts(sql: str) -> dict[str, Any]:
    if sqlglot is None:
        return {
            "reads_tables": [],
            "selects_star": bool(re.search(r"\bselect\s+\*", sql, re.I)),
            "has_where": bool(re.search(r"\bwhere\b", sql, re.I)),
            "filter_columns": [],
        }
    try:
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:
        return {
            "reads_tables": [],
            "selects_star": bool(re.search(r"\bselect\s+\*", sql, re.I)),
            "has_where": bool(re.search(r"\bwhere\b", sql, re.I)),
            "filter_columns": [],
        }
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = []
    for table in tree.find_all(exp.Table):
        if table.name.lower() not in ctes:
            full = ".".join(part for part in (table.catalog, table.db, table.name) if part)
            tables.append(full)
    filter_columns = sorted(
        {
            column.name
            for where in tree.find_all(exp.Where)
            for column in where.find_all(exp.Column)
            if column.name
        }
    )
    return {
        "reads_tables": list(dict.fromkeys(tables)),
        "selects_star": any(
            isinstance(projection, exp.Star)
            or (
                isinstance(projection, exp.Column)
                and isinstance(projection.this, exp.Star)
            )
            for select in tree.find_all(exp.Select)
            for projection in select.expressions
        ),
        "has_where": any(True for _ in tree.find_all(exp.Where)),
        "filter_columns": filter_columns,
    }


def result_reuse_eligible(sql: str, statement_type: str, modality: str) -> bool:
    """Gate conservador para não sugerir cache quando a AWS não o suporta."""
    return result_reuse_eligibility(sql, statement_type, modality)[0]


def result_reuse_eligibility(
    sql: str,
    statement_type: str,
    modality: str,
    *,
    managed_results: bool = False,
) -> tuple[bool, list[str]]:
    """Elegibilidade e motivo explícito para não recomendar cache indevido."""
    reasons: list[str] = []
    if sqlglot is None:
        return False, ["parser SQL indisponível"]
    if modality != "on_demand":
        reasons.append(f"modalidade {modality} não elegível")
    if statement_type != "DML":
        reasons.append(f"statement {statement_type or 'desconhecido'} não elegível")
    if managed_results:
        reasons.append("Athena managed query results não suporta result reuse")
    if reasons:
        return False, reasons
    try:
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:
        return False, ["SQL não pôde ser analisado"]
    if not any(True for _ in tree.find_all(exp.Select)):
        reasons.append("consulta não contém SELECT")
    tables = list(tree.find_all(exp.Table))
    if len(tables) > 20:
        reasons.append("consulta referencia mais de 20 tabelas")
    catalogs = {str(table.catalog).lower() for table in tables if table.catalog}
    if len(catalogs) > 1:
        reasons.append("consulta usa mais de um catálogo")
    for function in tree.find_all(exp.Func):
        name = str(function.sql_name() or function.name or "").lower()
        if name in _NONDETERMINISTIC_FUNCTIONS:
            reasons.append(f"função não determinística: {name}")
    return not reasons, sorted(set(reasons))
