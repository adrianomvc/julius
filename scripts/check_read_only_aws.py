"""Falha o CI quando código de coleta introduz verbos mutáveis da AWS."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = ROOT / "julius" / "collection"
MUTATING = re.compile(
    r"\.(?:create|delete|put|update|start|stop|terminate|modify|tag|untag|"
    r"copy_object|abort_multipart_upload)_[a-z0-9_]*\s*\("
)
ALLOWLIST = {
    (
        "julius/collection/collectors/athena/query.py",
        "start_query_execution",
    ),
}


def main() -> None:
    violations: list[str] = []
    for path in COLLECTION.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if MUTATING.search(line):
                if any(
                    allowed_path == relative and operation in line
                    for allowed_path, operation in ALLOWLIST
                ):
                    continue
                violations.append(f"{relative}:{number}: {line.strip()}")
    if violations:
        raise SystemExit(
            "Operações AWS potencialmente mutáveis encontradas:\n"
            + "\n".join(violations)
        )


if __name__ == "__main__":
    main()
