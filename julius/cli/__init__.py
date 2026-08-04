"""CLI do Julius, um módulo por família de comando.

Era um arquivo de 994 linhas com dezessete comandos. Importar os módulos aqui é
o que registra cada um no `app` — a ordem não importa, mas a importação sim: sem
ela o comando não existe.
"""

from julius.cli import (  # noqa: F401 - o import é o registro
    analysis,
    analyze,
    backlog,
    collect,
    notify,
    pricing,
    report,
    signals,
)
from julius.cli._shared import app

__all__ = ["app"]
