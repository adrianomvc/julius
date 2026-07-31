"""Campo que decide cifra precisa ter quem o escreva.

`redundant_read_bytes_window` e `incremental_source_evidence` ficaram declarados
no modelo e lidos pela regra de bookmark, sem nenhum coletor os preenchendo. O
efeito não falhava em teste nem em produção: a regra simplesmente nunca produzia
número, e um achado permanentemente sem economia é indistinguível de um em que a
economia é zero de verdade.

A técnica é a mesma de `scripts/check_read_only_aws.py`, que já varre o fonte
atrás de um padrão: aqui o padrão é "alguém atribui este campo".

**O que esta rede não pega**, dito para ninguém confiar nela além do que ela
cobre. A verificação é por *nome*, não por modelo: um campo homônimo escrito em
outro dataclass conta como escritor e produz um falso verde. E há um caminho de
escrita legítimo que só é visível porque os coletores declaram o alvo — o
`_enrich` de CloudWatch preenche por `setattr` com nome vindo de tabela, então o
nome aparece como chave de dicionário e não como atribuição.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
COLETA = RAIZ / "julius" / "collection"

#: Campos cuja ausência não deixa a regra falhar — deixa a cifra sumir. Cada
#: entrada é `(campo, para que serve)`, e a segunda parte vira a mensagem de erro
#: quando ninguém escreve o campo.
CAMPOS_QUE_DECIDEM_CIFRA = [
    ("redundant_read_bytes_window", "bytes relidos sem bookmark"),
    ("incremental_source_evidence", "fonte incremental, condição do bookmark"),
    ("bytes_read_window", "leitura da janela, base do reprocessamento"),
    ("date_partitioned", "partição temporal do prefixo de origem"),
    ("get_requests_window", "GETs por prefixo, base da compactação"),
    ("allocated_cost", "custo rateado do Cost Explorer"),
    ("modeled_cost", "custo modelado por tarifa"),
    ("dpu_seconds_window", "DPU-segundo faturado do Glue"),
    ("failure_categories", "categoria de falha, base do timeout"),
    ("last_run_at", "última execução, distingue job parado de job sem dado"),
]


def _fontes_da_coleta() -> list[tuple[Path, str]]:
    return [
        (caminho, caminho.read_text(encoding="utf-8"))
        for caminho in COLETA.rglob("*.py")
        if "__pycache__" not in caminho.parts
    ]


@pytest.mark.parametrize(
    ("campo", "proposito"),
    CAMPOS_QUE_DECIDEM_CIFRA,
    ids=[campo for campo, _ in CAMPOS_QUE_DECIDEM_CIFRA],
)
def test_a_field_that_decides_a_figure_has_someone_writing_it(campo, proposito):
    """Campo sem escritor produz cifra ausente em silêncio, não erro."""
    # Quatro formas de escrever, todas em uso no repositório: kwarg na
    # construção do dataclass, atribuição posterior, `setattr` com nome literal,
    # e declaração como chave de tabela — que é como o `_enrich` de CloudWatch
    # diz qual campo cada métrica preenche antes de aplicar por `setattr`.
    padrao = re.compile(
        rf"(?:\b{campo}\s*=|\.\s*{campo}\s*=|setattr\([^,]+,\s*[\"']{campo}[\"']"
        rf"|[\"']{campo}[\"']\s*:)"
    )
    escritores = sorted(
        caminho.relative_to(RAIZ).as_posix()
        for caminho, fonte in _fontes_da_coleta()
        # O próprio modelo declara o default; declarar não é escrever.
        if "models" not in caminho.parts and padrao.search(fonte)
    )

    assert escritores, (
        f"nenhum coletor escreve `{campo}` ({proposito}). "
        "A regra que o consome nunca vai produzir cifra, e nada mais vai avisar."
    )


def test_the_guard_would_catch_a_field_nobody_writes():
    """A rede só serve se pegar o caso que ela existe para pegar."""
    padrao = re.compile(r"\bcampo_que_ninguem_escreve\s*=")
    escritores = [
        caminho
        for caminho, fonte in _fontes_da_coleta()
        if padrao.search(fonte)
    ]

    assert escritores == []
