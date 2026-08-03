#!/usr/bin/env python3
"""Gera os artefatos de Skill a partir de `docs/ai/`, ou acusa divergência.

    python scripts/generate_skill_registry.py            # escreve
    python scripts/generate_skill_registry.py --check    # falha se divergir

O `--check` é o que roda nos testes. Sem ele, "o artefato é gerado" seria só uma
convenção — e convenção não sobrevive a alguém com pressa editando o arquivo
final porque era mais rápido.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from julius.analysis.skill_registry import RAIZ, check, write_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="não escreve nada; sai com erro se algum artefato estiver divergente",
    )
    args = parser.parse_args()

    if args.check:
        problemas = check()
        for problema in problemas:
            print(f"drift: {problema}", file=sys.stderr)
        if problemas:
            print(
                "\nRegenere com: python scripts/generate_skill_registry.py",
                file=sys.stderr,
            )
            return 1
        print("OK: artefatos de Skill em dia com docs/ai/.")
        return 0

    for caminho in write_all():
        print(f"escrito {caminho.relative_to(RAIZ).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
