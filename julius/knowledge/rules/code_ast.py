"""Leitura de AST Python compartilhada pelos scanners de código.

Nada aqui sabe o que é Spark, Glue ou SageMaker. São as perguntas que qualquer
scanner de script faz — quais linhas chamam este nome, o que acontece dentro de
um laço, onde uma exceção é engolida — e elas não mudam com o serviço que roda
o script.

A separação nasceu quando o segundo scanner apareceu. Copiar os utilitários
teria funcionado, e a cópia teria divergido: um `map` reconhecido num arquivo e
não no outro é o tipo de diferença que ninguém encontra olhando.

Toda função aceita `tree=None` e devolve vazio. Script que não parseia é script
sem análise, não coleta interrompida: o `_parse` daqui já converte o erro de
sintaxe nesse `None`, e quem chama nunca precisa tratar a exceção.
"""

from __future__ import annotations

import ast
import re


def parse(source: str) -> ast.AST | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def matching_lines(lines: list[str], pattern: str) -> list[int]:
    regex = re.compile(pattern)
    return [index for index, line in enumerate(lines, start=1) if regex.search(line)]


def call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def dotted_name(node: ast.AST) -> str:
    parts = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def call_lines(
    tree: ast.AST | None,
    names: set[str],
    *,
    first_arg: int | None = None,
) -> list[int]:
    if tree is None:
        return []
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or call_name(node) not in names:
            continue
        if first_arg is not None:
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            if node.args[0].value != first_arg:
                continue
        result.append(node.lineno)
    return sorted(set(result))


def string_arg_call_lines(tree: ast.AST | None, name: str, value: str) -> list[int]:
    if tree is None:
        return []
    result = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and call_name(node) == name
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).lower() == value.lower()
        ):
            result.append(node.lineno)
    return sorted(set(result))


def calls_inside_loops(tree: ast.AST | None, names: set[str]) -> list[int]:
    if tree is None:
        return []
    result = []
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for node in ast.walk(loop):
            if isinstance(node, ast.Call) and call_name(node) in names:
                result.append(node.lineno)
    return sorted(set(result))


def swallowed_exception_lines(tree: ast.AST | None) -> list[int]:
    """`except` que não falha nem registra: a falha some e o trabalho se repete."""
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
        if not meaningful or all(isinstance(item, (ast.Pass, ast.Continue)) for item in meaningful):
            result.append(node.lineno)
    return sorted(set(result))


def external_io_in_row_functions(
    tree: ast.AST | None,
    *,
    appliers: set[str],
) -> list[int]:
    """Chamada externa dentro de função aplicada por registro.

    `appliers` é o que muda entre serviços: no Spark são `udf`/`map`/`foreach`,
    num script de treino é o que percorre o lote. O que não muda é o custo —
    latência de rede multiplicada pela cardinalidade dos dados.
    """
    if tree is None:
        return []
    functions: dict[str, ast.AST] = {}
    row_function_names: set[str] = set()
    external_clients: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Call) and dotted_name(value.func) in {
                "boto3.client",
                "boto3.resource",
            }:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                external_clients.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
        if isinstance(node, ast.Call) and call_name(node) in appliers:
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    row_function_names.add(arg.id)
    result = []
    for name in row_function_names:
        definition = functions.get(name)
        if definition is None:
            continue
        for call in ast.walk(definition):
            if not isinstance(call, ast.Call):
                continue
            dotted = dotted_name(call.func)
            root = dotted.split(".", 1)[0]
            if (
                dotted.startswith(("requests.", "httpx.", "urllib3."))
                or dotted in {"boto3.client", "boto3.resource"}
                or root in external_clients
                or dotted.endswith((".execute", ".executemany"))
            ):
                result.append(call.lineno)
    return sorted(set(result))
