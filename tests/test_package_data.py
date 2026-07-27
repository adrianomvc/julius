"""Arquivo que não é `.py` precisa estar declarado, ou some do pacote instalado.

Esta é a classe de erro que a suíte não pega por construção: os testes rodam do
repositório, onde todo arquivo está no disco. No pacote instalado só entra o que
`package-data` declara — e o que faltar quebra em produção, não aqui.

Aconteceu de verdade nesta reestruturação. As tabelas de preço nasceram na fase 3
como `.toml` sob `knowledge/pricing/` e ninguém as declarou; um `pip install`
teria produzido um Julius que levanta `UnknownPricingRegionError` ao montar a
configuração padrão.
"""

from __future__ import annotations

import fnmatch
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "julius"

# Extensões que são código-fonte ou artefato de execução, não dado do pacote.
IGNORED_SUFFIXES = {".py", ".pyc", ".pyo"}
IGNORED_PARTS = {"__pycache__"}


def _declared() -> dict[str, list[str]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]


def _data_files() -> list[pathlib.Path]:
    return [
        path
        for path in sorted(PACKAGE.rglob("*"))
        if path.is_file()
        and path.suffix not in IGNORED_SUFFIXES
        and not IGNORED_PARTS & set(path.parts)
    ]


def _is_covered(path: pathlib.Path, declared: dict[str, list[str]]) -> bool:
    relative = path.relative_to(PACKAGE)
    for package, patterns in declared.items():
        package_dir = PACKAGE / pathlib.Path(*package.split(".")[1:])
        if package_dir not in path.parents:
            continue
        inside = path.relative_to(package_dir).as_posix()
        if any(fnmatch.fnmatch(inside, pattern) for pattern in patterns):
            return True
    del relative
    return False


def test_every_non_python_file_is_declared_as_package_data():
    missing = [
        path.relative_to(ROOT).as_posix()
        for path in _data_files()
        if not _is_covered(path, _declared())
    ]
    assert missing == [], (
        "estes arquivos não entrariam no pacote instalado; "
        "declare-os em [tool.setuptools.package-data]"
    )


def test_every_declared_package_still_exists():
    """Declaração apontando para pacote renomeado para de valer em silêncio."""
    missing = [
        package
        for package in _declared()
        if not (PACKAGE / pathlib.Path(*package.split(".")[1:])).is_dir()
    ]
    assert missing == []


def test_the_things_the_report_and_the_pricing_need_are_there():
    """Os dois casos concretos, nomeados: sem eles o produto não roda."""
    declared = _declared()
    desenho = PACKAGE / "reporting" / "templates" / "design" / "Relatorio Julius.dc.html"
    email = PACKAGE / "reporting" / "templates" / "email.html.j2"
    table = PACKAGE / "knowledge" / "pricing" / "tables" / "sa-east-1.toml"

    assert desenho.is_file() and _is_covered(desenho, declared)
    assert email.is_file() and _is_covered(email, declared)
    assert table.is_file() and _is_covered(table, declared)
