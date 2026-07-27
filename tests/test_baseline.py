"""A saída do pipeline não muda sem alguém decidir que muda.

Os testes verificam o que alguém pensou em asseverar. Este pega o resto: roda o
pipeline inteiro sobre os datasets de exemplo e compara o `report.json` com a
referência congelada em `data/baseline/`. Qualquer diferença aparece — inclusive
a que ninguém previu, que é justamente a que escapa de teste unitário.

Ele nasceu manual, para a reestruturação que moveu ~14 mil linhas entre pacotes,
e ficou 27 commits sem ser regravado: acusava divergência em todos os datasets, o
vermelho virou paisagem e o sinal deixou de valer. Na suíte isso não acontece,
porque regravar passa a ser um ato deliberado — `write` muda arquivos
versionados, e a mudança aparece no diff junto com a razão dela.

Quando a divergência for intencional, regrave e explique no commit:

    python scripts/snapshot_baseline.py write
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "snapshot_baseline.py"


def _snapshot_module():
    """`scripts/` não é um pacote importável, e não deve virar um só por isto.

    O que interessa manter num lugar só é a definição do que é comparado — a
    data fixa, o `scan_id` fixo e as chaves voláteis. Duplicá-la aqui deixaria o
    teste e o `write` divergirem em silêncio, que é o defeito que este arquivo
    existe para não ter.
    """
    spec = importlib.util.spec_from_file_location("snapshot_baseline", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = _snapshot_module()


@pytest.mark.parametrize("sample", snapshot.SAMPLES, ids=lambda path: path.stem)
def test_report_json_matches_the_frozen_baseline(sample: pathlib.Path) -> None:
    reference = snapshot.BASELINE / f"{sample.stem}.json"
    assert reference.is_file(), (
        f"{reference.relative_to(ROOT).as_posix()} não existe; "
        "rode `python scripts/snapshot_baseline.py write`"
    )

    expected = json.loads(reference.read_text(encoding="utf-8"))
    actual = snapshot._payload(sample)

    # A comparação é por chave de primeiro nível: o `report.json` do maior
    # dataset tem 68 KB, e um assert do dicionário inteiro despeja tudo isso no
    # terminal sem dizer onde olhar.
    divergent = [
        key
        for key in sorted(set(expected) | set(actual))
        if expected.get(key) != actual.get(key)
    ]
    assert divergent == [], (
        f"a saída de {sample.name} mudou em {divergent}. "
        "Se a mudança é intencional, regrave a referência com "
        "`python scripts/snapshot_baseline.py write` e diga no commit o porquê"
    )


def test_every_sample_dataset_has_a_baseline() -> None:
    """Dataset novo sem referência não é comparado com nada, e ninguém percebe."""
    missing = [
        sample.name
        for sample in snapshot.SAMPLES
        if not (snapshot.BASELINE / f"{sample.stem}.json").is_file()
    ]
    assert missing == [], (
        "estes datasets de exemplo não têm referência congelada; "
        "rode `python scripts/snapshot_baseline.py write`"
    )
