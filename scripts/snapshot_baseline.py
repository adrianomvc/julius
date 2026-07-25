"""Congela a saída do pipeline para comparar antes e depois de uma movimentação.

As fases de reestruturação só movem código: o `report.json` de um mesmo dataset
tem que sair idêntico, exceto o manifesto, que carrega o scan_id e a data. Este
script grava a referência e a compara.

    python scripts/snapshot_baseline.py write   # antes de mover
    python scripts/snapshot_baseline.py check   # depois de mover
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = sorted((ROOT / "data" / "sample").glob("*.json"))
BASELINE = ROOT / "data" / "baseline"

# `scan_id` e a data do scan variam por execução por desenho; fixá-los deixa
# qualquer outra diferença ser sinal de mudança de comportamento.
SCAN_ID = "baseline"
TODAY = date(2026, 7, 25)

# O manifesto carrega hash de configuração e horário de execução.
VOLATILE = {"manifest", "generated_at"}


def _payload(sample: Path) -> dict:
    from julius.pipeline import analyze
    from julius.report import renderer

    analysis = analyze(sample, today=TODAY, scan_id=SCAN_ID)
    payload = json.loads(renderer.render_json(analysis.vm, analysis.opportunities))
    return {key: value for key, value in payload.items() if key not in VOLATILE}


def write() -> int:
    BASELINE.mkdir(parents=True, exist_ok=True)
    for sample in SAMPLES:
        target = BASELINE / f"{sample.stem}.json"
        target.write_text(
            json.dumps(_payload(sample), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print(f"gravado  {target.relative_to(ROOT)}")
    return 0


def check() -> int:
    failures = 0
    for sample in SAMPLES:
        reference = BASELINE / f"{sample.stem}.json"
        if not reference.exists():
            print(f"AUSENTE  {reference.relative_to(ROOT)} — rode `write` primeiro")
            failures += 1
            continue
        expected = json.loads(reference.read_text(encoding="utf-8"))
        actual = _payload(sample)
        if actual == expected:
            print(f"idêntico {sample.name}")
            continue
        failures += 1
        print(f"DIVERGE  {sample.name}")
        for key in sorted(set(expected) | set(actual)):
            if expected.get(key) != actual.get(key):
                print(f"           chave divergente: {key}")
    return 1 if failures else 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command not in {"write", "check"}:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(write() if command == "write" else check())
