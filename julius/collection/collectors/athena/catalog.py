"""Enriquecimento por Glue Catalog e S3: particionamento, formato,
compressão, arquivos pequenos e tabelas wide."""

from __future__ import annotations

from typing import Any

from julius.collection.collectors.athena.evidence import AthenaExecutionEvidence

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - instalação incompleta tem fallback seguro
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]

from julius.collection.collectors.s3_evidence import object_evidence

_COLUMNAR = {"PARQUET", "ORC"}
_ROW_FORMATS = {"CSV", "JSON", "TSV", "TEXT"}
_WIDE_TABLE_COLUMNS = 50
_PARTITION_PROJECTION_MIN_PARTITIONS = 1000
#: Teto de páginas na contagem de partições, no mesmo espírito de
#: `MAX_LIST_PAGES`: cinco páginas de mil passam de qualquer limiar que dependa
#: desse número, e param antes de varrer uma tabela inteira.
MAX_PARTITION_PAGES = 5
_PARTITION_PAGE_SIZE = 1000


def enrich_catalog(items: list[AthenaExecutionEvidence], glue, s3, telemetry) -> None:
    if glue is None:
        return
    telemetry.used("Athena Glue Catalog")
    if s3 is not None:
        telemetry.used("Athena S3")
    cache: dict[str, dict[str, Any]] = {}
    for item in items:
        for name in item.reads_tables:
            if name not in cache:
                parts = name.split(".")
                if len(parts) < 2:
                    continue
                try:
                    table = glue.get_table(DatabaseName=parts[-2], Name=parts[-1])["Table"]
                    keys = [key["Name"] for key in table.get("PartitionKeys", [])]
                    descriptor = table.get("StorageDescriptor") or {}
                    parameters = table.get("Parameters") or {}
                    fmt = storage_format(descriptor, parameters)
                    objects = object_evidence(s3, descriptor.get("Location"))
                    if not objects["complete"] and objects["count"]:
                        telemetry.partial(
                            "Athena S3",
                            category="bounded_or_incomplete",
                            detail=f"listagem limitada em {name}",
                        )
                    projection = str(parameters.get("projection.enabled") or "").lower() == "true"
                    partition_count, partitions_complete = (
                        (0, True)
                        if projection
                        else count_partitions(glue, parts[-2], parts[-1])
                    )
                    if not partitions_complete:
                        telemetry.partial(
                            "Athena Glue Catalog",
                            category="bounded_or_incomplete",
                            detail=f"contagem de partições limitada em {name}",
                        )
                    codecs, explicitly_uncompressed = compression_evidence(
                        descriptor, parameters
                    )
                    cache[name] = {
                        "keys": keys,
                        "format": fmt,
                        "objects": objects,
                        "columns": len(descriptor.get("Columns") or []),
                        "projection": projection,
                        "partition_count": partition_count,
                        "codecs": codecs,
                        "columnar_uncompressed": (
                            fmt in _COLUMNAR and explicitly_uncompressed
                        ),
                        "row_uncompressed": (
                            fmt in _ROW_FORMATS
                            and objects["count"] > 0
                            and objects["compressed_count"] == 0
                        ),
                        "lake_formation_governed": bool(
                            table.get("IsRegisteredWithLakeFormation")
                            or str(table.get("TableType") or "").upper()
                            == "GOVERNED"
                        ),
                    }
                except Exception as exc:
                    telemetry.failed("Athena Glue Catalog", exc, detail=name)
                    continue
            evidence = cache[name]
            if evidence["lake_formation_governed"]:
                item.reuse_eligible = False
                reason = "tabela governada/registrada no Lake Formation"
                if reason not in item.reuse_ineligible_reasons:
                    item.reuse_ineligible_reasons.append(reason)
            keys = evidence["keys"]
            fmt = evidence["format"]
            objects = evidence["objects"]
            item.partition_keys.extend(key for key in keys if key not in item.partition_keys)
            if fmt and fmt not in item.storage_formats:
                item.storage_formats.append(fmt)
            if objects["small_files"]:
                item.small_files_confirmed = True
                item.small_file_count = max(item.small_file_count, objects["count"])
                item.average_file_bytes = objects["average"]
            item.total_table_bytes += objects["total"]
            item.max_table_columns = max(item.max_table_columns, evidence["columns"])
            if evidence["columns"] >= _WIDE_TABLE_COLUMNS:
                item.wide_tables.append(name)
            if not keys:
                item.unpartitioned_tables.append(name)
            if evidence["row_uncompressed"]:
                item.row_format_uncompressed.append(name)
            if evidence["columnar_uncompressed"]:
                item.columnar_uncompressed.append(name)
            item.compression_codecs.extend(evidence["codecs"])
            item.partition_projection_enabled = (
                item.partition_projection_enabled or evidence["projection"]
            )
            item.partition_count = max(item.partition_count, evidence["partition_count"])
            if (
                keys
                and not evidence["projection"]
                and evidence["partition_count"] >= _PARTITION_PROJECTION_MIN_PARTITIONS
            ):
                item.partition_projection_candidates.append(name)
            for key in keys:
                if not has_partition_predicate(item.raw_sql, name, key):
                    item.missing_partition_filters.append(key)
        item.missing_partition_filters = sorted(set(item.missing_partition_filters))
        item.wide_tables = sorted(set(item.wide_tables))
        item.unpartitioned_tables = sorted(set(item.unpartitioned_tables))
        item.row_format_uncompressed = sorted(set(item.row_format_uncompressed))
        item.columnar_uncompressed = sorted(set(item.columnar_uncompressed))
        item.compression_codecs = sorted(set(item.compression_codecs))
        item.partition_projection_candidates = sorted(
            set(item.partition_projection_candidates)
        )


def has_partition_predicate(sql: str, table_name: str, key: str) -> bool:
    """Confirma o predicado no alias da tabela, inclusive dentro de CTEs."""
    if sqlglot is None:
        return False
    try:
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:
        return False
    target = table_name.split(".")[-1].lower()
    physical_tables = list(tree.find_all(exp.Table))
    aliases = {
        table.alias_or_name.lower()
        for table in physical_tables
        if table.name.lower() == target
    }
    for where in tree.find_all(exp.Where):
        for column in where.find_all(exp.Column):
            if column.name.lower() != key.lower():
                continue
            qualifier = (column.table or "").lower()
            if qualifier in aliases:
                return True
            if not qualifier and len(physical_tables) == 1:
                return True
    return False


def storage_format(descriptor: dict, parameters: dict | None = None) -> str:
    value = " ".join(
        str(descriptor.get(key) or "") for key in ("InputFormat", "OutputFormat")
    )
    serde = descriptor.get("SerdeInfo") or {}
    value += " " + str(serde.get("SerializationLibrary") or "")
    classification = str((parameters or {}).get("classification") or "")
    value = (value + " " + classification).upper()
    aliases = {
        "PARQUET": ("PARQUET",),
        "ORC": ("ORC",),
        "JSON": ("JSON",),
        "CSV": ("CSV", "OPENCSV"),
        "TSV": ("TSV",),
        "TEXT": ("TEXTINPUT", "LAZYSIMPLE"),
    }
    return next(
        (fmt for fmt, markers in aliases.items() if any(marker in value for marker in markers)),
        "",
    )


def small_file_evidence(s3, location: str | None) -> tuple[int, int, bool]:
    evidence = object_evidence(s3, location)
    return evidence["count"], evidence["average"], evidence["small_files"]


def compression_evidence(
    descriptor: dict, parameters: dict
) -> tuple[list[str], bool]:
    serde_parameters = (descriptor.get("SerdeInfo") or {}).get("Parameters") or {}
    combined = {**parameters, **serde_parameters}
    values = [
        str(value).upper()
        for key, value in combined.items()
        if "compress" in str(key).lower()
    ]
    codecs = sorted(
        {
            codec
            for codec in ("GZIP", "SNAPPY", "ZSTD", "ZLIB", "BZIP2", "LZ4")
            if any(codec in value for value in values)
        }
    )
    explicitly_uncompressed = (
        descriptor.get("Compressed") is False
        and any(value in {"NONE", "UNCOMPRESSED"} for value in values)
    )
    return codecs, explicitly_uncompressed


def count_partitions(
    glue, database: str, table: str, *, max_pages: int = MAX_PARTITION_PAGES
) -> tuple[int, bool]:
    """Quantas partições a tabela tem, e se a contagem foi até o fim.

    Contar todas as partições de uma tabela de data lake pode custar centenas de
    chamadas, e isso rodava por tabela lida. O teto não muda o veredito que
    depende deste número: a recomendação de partition projection começa em
    `_PARTITION_PROJECTION_MIN_PARTITIONS` (mil) e o teto são cinco páginas de
    mil — quem trunca já passou do limiar com folga.
    """
    try:
        paginator = glue.get_paginator("get_partitions")
        pages = paginator.paginate(
            DatabaseName=database,
            TableName=table,
            ExcludeColumnSchema=True,
            PaginationConfig={"PageSize": _PARTITION_PAGE_SIZE},
        )
        total = 0
        for index, page in enumerate(pages, start=1):
            total += len(page.get("Partitions", []))
            # Cortar depois de somar evita pedir a página seguinte só para
            # descobrir que ela existe.
            if index >= max_pages:
                return total, False
        return total, True
    except Exception:
        return 0, True
