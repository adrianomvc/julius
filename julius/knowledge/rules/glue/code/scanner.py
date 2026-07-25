"""Scanner estático conservador para padrões de custo em scripts AWS Glue."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CodeFinding:
    rule_id: str
    signal: str
    lines: tuple[int, ...]


_SPARK_MARKERS = (
    r"\bpyspark\b",
    r"\bSparkContext\b",
    r"\bSparkSession\b",
    r"\bGlueContext\b",
    r"\bDynamicFrame\b",
    r"\bspark\.(?:read|sql|createDataFrame)\b",
    r"\bcreate_dynamic_frame\b",
    r"\bwrite_dynamic_frame\b",
)
_DISTRIBUTED_TRANSFORMS = (
    "join",
    "groupBy",
    "groupByKey",
    "reduceByKey",
    "aggregateByKey",
    "distinct",
    "orderBy",
    "sort",
    "repartition",
    "coalesce",
    "map",
    "flatMap",
    "mapPartitions",
)
_ACTIONS = {"count", "collect", "show", "take", "first", "toPandas"}


def scan_glue_script(source: str) -> list[CodeFinding]:
    """Retorna sinais estáticos; não interpreta ausência de sinal como segurança."""
    findings: dict[str, CodeFinding] = {}
    lines = source.splitlines()
    tree = _parse(source)
    spark_lines = _matching_lines(lines, "|".join(_SPARK_MARKERS))
    is_spark = bool(spark_lines)

    def add(rule_id: str, signal: str, line_numbers: list[int] | tuple[int, ...]) -> None:
        clean = tuple(sorted({line for line in line_numbers if line > 0}))
        current = findings.get(rule_id)
        if current:
            clean = tuple(sorted(set(current.lines) | set(clean)))
        findings[rule_id] = CodeFinding(rule_id, signal, clean)

    if is_spark and _has_catalog_read(source) and not re.search(
        r"\b(?:push_down_predicate|catalogPartitionPredicate)\b", source
    ):
        add(
            "GLUE-CODE-PUSHDOWN",
            "leitura do Glue Catalog sem predicate pushdown explícito",
            _matching_lines(lines, r"\b(?:from_catalog|create_dynamic_frame)\b"),
        )

    if (
        is_spark
        and re.search(r"\bspark\.read\.(?:parquet|orc|json|csv)\s*\(\s*[\"']s3://", source)
        and _call_lines(tree, {"filter", "where"})
    ):
        add(
            "GLUE-CODE-S3-FULL-SCAN",
            "leitura direta de prefixo S3 seguida de filtro no Spark",
            _matching_lines(
                lines,
                r"\bspark\.read\.(?:parquet|orc|json|csv)\s*\(",
            ),
        )

    if is_spark and _has_unpartitioned_jdbc_read(source):
        add(
            "GLUE-CODE-JDBC-SINGLE-READER",
            "leitura JDBC sem opções explícitas de paralelismo",
            _matching_lines(lines, r"[\"']jdbc[\"']|\.jdbc\s*\("),
        )

    single_partition = _call_lines(tree, {"repartition", "coalesce"}, first_arg=1)
    if is_spark and single_partition:
        add(
            "GLUE-CODE-SINGLE-PARTITION",
            "reparticionamento para uma única partição",
            single_partition,
        )

    driver_lines = _call_lines(tree, {"collect", "toPandas", "toLocalIterator"})
    if is_spark and driver_lines:
        add(
            "GLUE-CODE-DRIVER-MATERIALIZATION",
            "materialização de dados distribuídos no driver",
            driver_lines,
        )

    udf_lines = _udf_lines(tree, lines)
    if is_spark and udf_lines:
        add(
            "GLUE-CODE-PYTHON-UDF",
            "Python UDF potencialmente substituível por função Spark nativa",
            udf_lines,
        )

    action_lines = _call_lines(tree, _ACTIONS)
    if is_spark and len(action_lines) >= 3:
        add(
            "GLUE-CODE-REPEATED-ACTIONS",
            "múltiplas ações Spark podem recomputar o mesmo plano",
            action_lines,
        )

    cache_lines = _call_lines(tree, {"cache", "persist"})
    if is_spark and cache_lines and not _call_lines(tree, {"unpersist"}):
        add(
            "GLUE-CODE-CACHE-LIFECYCLE",
            "cache/persist sem unpersist observável",
            cache_lines,
        )

    iterative = _calls_inside_loops(tree, {"withColumn", "union", "unionByName"})
    if is_spark and iterative:
        add(
            "GLUE-CODE-ITERATIVE-PLAN",
            "construção iterativa do plano Spark dentro de loop",
            iterative,
        )

    external_io = _external_io_in_row_functions(tree)
    if is_spark and external_io:
        add(
            "GLUE-CODE-ROW-EXTERNAL-IO",
            "chamada externa em função usada por UDF/map/foreach",
            external_io,
        )

    overwrite = _string_arg_call_lines(tree, "mode", "overwrite")
    if is_spark and overwrite and not re.search(
        r"partitionOverwriteMode|replaceWhere|overwritePartitions", source
    ):
        add(
            "GLUE-CODE-FULL-OVERWRITE",
            "gravação overwrite sem escopo incremental observável",
            overwrite,
        )

    excessive = _large_repartition_lines(tree)
    writes_in_loop = _calls_inside_loops(tree, {"save", "parquet", "json", "csv", "insertInto"})
    if is_spark and (excessive or writes_in_loop):
        add(
            "GLUE-CODE-SMALL-FILES",
            "particionamento excessivo ou escrita repetida dentro de loop",
            excessive + writes_in_loop,
        )

    shuffle = _call_lines(tree, set(_DISTRIBUTED_TRANSFORMS))
    if is_spark and shuffle:
        add(
            "GLUE-CODE-SHUFFLE",
            "transformações que podem gerar shuffle relevante",
            shuffle,
        )

    shuffle_config = _extreme_shuffle_partition_lines(tree)
    if is_spark and shuffle_config:
        add(
            "GLUE-CODE-SHUFFLE-PARTITIONS",
            "spark.sql.shuffle.partitions fixado em valor extremo",
            shuffle_config,
        )

    swallowed = _swallowed_exception_lines(tree)
    if swallowed:
        add(
            "GLUE-CODE-SWALLOWED-EXCEPTION",
            "exceção descartada sem falha ou registro observável",
            swallowed,
        )

    if is_spark and re.search(r"\bjob\.init\s*\(", source) and not re.search(
        r"\btransformation_ctx\s*=", source
    ):
        add(
            "GLUE-CODE-BOOKMARK-CONTEXT",
            "job inicializado sem transformation_ctx observável nas fontes Glue",
            _matching_lines(lines, r"\bjob\.init\s*\("),
        )

    if (
        is_spark
        and re.search(r"\bjob\.init\s*\(", source)
        and not re.search(r"\bjob\.commit\s*\(", source)
    ):
        add(
            "GLUE-CODE-BOOKMARK-COMMIT",
            "job.init sem job.commit observável",
            _matching_lines(lines, r"\bjob\.init\s*\("),
        )

    if (
        not spark_lines
        and isinstance(tree, ast.Module)
        and bool(tree.body)
        and len(source.strip()) >= 20
    ):
        add(
            "GLUE-SPARK-TO-PYTHON-SHELL",
            "script Python sem APIs Spark/Glue distribuídas detectadas",
            [1],
        )

    return sorted(findings.values(), key=lambda finding: finding.rule_id)


def _parse(source: str) -> ast.AST | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _matching_lines(lines: list[str], pattern: str) -> list[int]:
    regex = re.compile(pattern)
    return [index for index, line in enumerate(lines, start=1) if regex.search(line)]


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _call_lines(
    tree: ast.AST | None,
    names: set[str],
    *,
    first_arg: int | None = None,
) -> list[int]:
    if tree is None:
        return []
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in names:
            continue
        if first_arg is not None:
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            if node.args[0].value != first_arg:
                continue
        result.append(node.lineno)
    return sorted(set(result))


def _string_arg_call_lines(
    tree: ast.AST | None, name: str, value: str
) -> list[int]:
    if tree is None:
        return []
    result = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == name
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).lower() == value.lower()
        ):
            result.append(node.lineno)
    return sorted(set(result))


def _has_catalog_read(source: str) -> bool:
    return bool(
        re.search(r"\bfrom_catalog\s*\(", source)
        or re.search(r"\bcreate_dynamic_frame\.from_catalog\s*\(", source)
    )


def _has_unpartitioned_jdbc_read(source: str) -> bool:
    has_jdbc = bool(
        re.search(r"\.format\s*\(\s*[\"']jdbc[\"']\s*\)", source)
        or re.search(r"connection_type\s*=\s*[\"']jdbc[\"']", source)
        or re.search(r"\.jdbc\s*\(", source)
    )
    has_partitioning = bool(
        re.search(
            r"\b(?:partitionColumn|numPartitions|lowerBound|upperBound|"
            r"hashfield|hashexpression|hashpartitions)\b",
            source,
        )
    )
    return has_jdbc and not has_partitioning


def _udf_lines(tree: ast.AST | None, lines: list[str]) -> list[int]:
    result = _call_lines(tree, {"udf", "pandas_udf"})
    result += _matching_lines(lines, r"@\s*(?:pandas_)?udf\b")
    return sorted(set(result))


def _calls_inside_loops(tree: ast.AST | None, names: set[str]) -> list[int]:
    if tree is None:
        return []
    result = []
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for node in ast.walk(loop):
            if isinstance(node, ast.Call) and _call_name(node) in names:
                result.append(node.lineno)
    return sorted(set(result))


def _large_repartition_lines(tree: ast.AST | None) -> list[int]:
    if tree is None:
        return []
    result = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "repartition"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, int)
            and node.args[0].value >= 1000
        ):
            result.append(node.lineno)
    return sorted(set(result))


def _extreme_shuffle_partition_lines(tree: ast.AST | None) -> list[int]:
    if tree is None:
        return []
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        key, value = node.args[0], node.args[1]
        if (
            isinstance(key, ast.Constant)
            and key.value == "spark.sql.shuffle.partitions"
            and isinstance(value, ast.Constant)
        ):
            try:
                configured = int(str(value.value))
            except (TypeError, ValueError):
                continue
            if configured <= 1 or configured >= 1000:
                result.append(node.lineno)
    return sorted(set(result))


def _swallowed_exception_lines(tree: ast.AST | None) -> list[int]:
    if tree is None:
        return []
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        meaningful = [
            item
            for item in node.body
            if not (
                isinstance(item, ast.Expr)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            )
        ]
        if not meaningful or all(
            isinstance(item, (ast.Pass, ast.Continue)) for item in meaningful
        ):
            result.append(node.lineno)
    return sorted(set(result))


def _external_io_in_row_functions(tree: ast.AST | None) -> list[int]:
    if tree is None:
        return []
    functions: dict[str, tuple[ast.AST, int]] = {}
    row_function_names: set[str] = set()
    external_clients: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = (node, node.lineno)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and _dotted_name(value.func) in {"boto3.client", "boto3.resource"}
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                external_clients.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
        if isinstance(node, ast.Call) and _call_name(node) in {
            "udf",
            "pandas_udf",
            "map",
            "flatMap",
            "foreach",
        }:
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    row_function_names.add(arg.id)
    result = []
    for name in row_function_names:
        definition = functions.get(name)
        if not definition:
            continue
        node, _ = definition
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            dotted = _dotted_name(call.func)
            root = dotted.split(".", 1)[0]
            if (
                dotted.startswith(("requests.", "httpx.", "urllib3."))
                or dotted in {"boto3.client", "boto3.resource"}
                or root in external_clients
                or dotted.endswith((".execute", ".executemany"))
            ):
                result.append(call.lineno)
    return sorted(set(result))


def _dotted_name(node: ast.AST) -> str:
    parts = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))
