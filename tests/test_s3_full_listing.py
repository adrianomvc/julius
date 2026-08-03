"""Listar o data lake inteiro: o que isso compra, e o que custa.

A coleta de S3 nasceu limitada de propósito — cinco páginas por prefixo, sem
`ListBuckets` — porque listar cobra por request e o produto não pode custar
dinheiro para descobrir custo. Recomendar classe de armazenamento muda a conta:
dizer que um prefixo tem 8 TB frios a partir de cinco páginas de mil objetos
seria extrapolar, e a recomendação sairia com um número que ninguém mediu.

`--s3-full-listing` liga a listagem completa e paralela. Estes testes cobram as
três coisas que ela não pode quebrar: o agregado tem que ficar correto, a ordem
tem que ser determinística (senão dois scans do mesmo inventário produzem
datasets diferentes e o diff fica ilegível), e o custo tem que ser declarado.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from julius.collection.collectors import s3 as s3_collector
from julius.collection.collectors.s3_evidence import MAX_LIST_PAGES, list_objects
from julius.collection.models.s3 import age_bucket
from julius.collection.window import AnalysisWindow

AGORA = datetime(2026, 7, 29, tzinfo=timezone.utc)
JANELA = AnalysisWindow(start=AGORA - timedelta(days=30), end=AGORA, days=30)


class S3Falso:
    """Cliente que pagina de verdade, para o teto e o paralelismo importarem."""

    def __init__(
        self,
        objetos_por_prefixo: dict[str, list[dict]],
        por_pagina: int = 2,
        latencia: float = 0.0,
    ):
        self.objetos = objetos_por_prefixo
        self.por_pagina = por_pagina
        # Sem alguma latência a chamada é instantânea e duas threads nunca se
        # sobrepõem — o teste de paralelismo passaria com execução em série.
        self.latencia = latencia
        self.chamadas = 0
        self.simultaneas = 0
        self._pico = 0
        self._trava = threading.Lock()

    @property
    def pico_de_concorrencia(self) -> int:
        return self._pico

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        with self._trava:
            self.chamadas += 1
            self.simultaneas += 1
            self._pico = max(self._pico, self.simultaneas)
        try:
            if self.latencia:
                time.sleep(self.latencia)
            todos = self.objetos.get(Prefix, [])
            inicio = int(ContinuationToken or 0)
            fim = inicio + self.por_pagina
            pagina = todos[inicio:fim]
            truncado = fim < len(todos)
            resposta = {"Contents": pagina, "IsTruncated": truncado}
            if truncado:
                resposta["NextContinuationToken"] = str(fim)
            return resposta
        finally:
            with self._trava:
                self.simultaneas -= 1


def _objeto(dias: int, tamanho: int, classe: str | None = None) -> dict:
    item = {"Key": f"k{dias}", "Size": tamanho, "LastModified": AGORA - timedelta(days=dias)}
    if classe:
        item["StorageClass"] = classe
    return item


# ---------------------------------------------------------------------------
# O teto
# ---------------------------------------------------------------------------


def test_the_page_cap_still_truncates_and_says_so():
    """O default continua limitado: pagar pela resposta exata é escolha."""
    objetos = [_objeto(dia, 100) for dia in range(50)]
    client = S3Falso({"vendas/": objetos})

    lidos, completo = list_objects(client, "lake", "vendas/", max_pages=MAX_LIST_PAGES)

    assert len(lidos) == MAX_LIST_PAGES * 2
    assert completo is False


def test_without_a_cap_the_prefix_is_listed_to_the_end():
    objetos = [_objeto(dia, 100) for dia in range(50)]
    client = S3Falso({"vendas/": objetos})

    lidos, completo = list_objects(client, "lake", "vendas/", max_pages=None)

    assert len(lidos) == 50
    assert completo is True


def test_a_failure_midway_keeps_what_was_already_read():
    """Evidência truncada nunca vira zero — nem quando a falha é na página N."""

    class FalhaNaSegunda(S3Falso):
        def list_objects_v2(self, **kwargs):
            if kwargs.get("ContinuationToken"):
                raise RuntimeError("AccessDenied")
            return super().list_objects_v2(**kwargs)

    client = FalhaNaSegunda({"vendas/": [_objeto(dia, 100) for dia in range(10)]})

    lidos, completo = list_objects(client, "lake", "vendas/", max_pages=None)

    assert len(lidos) == 2
    assert completo is False


# ---------------------------------------------------------------------------
# O agregado
# ---------------------------------------------------------------------------


def _coletar(objetos: list[dict], **kwargs):
    client = S3Falso({"vendas/": objetos}, por_pagina=1000)
    prefixos = s3_collector.collect_prefixes(
        client,
        known=[("s3://lake/vendas/", "table_location", "db.vendas")],
        window=JANELA,
        stale_after_days=30,
        max_pages=None,
        **kwargs,
    )
    return prefixos[0], client


def test_storage_class_comes_from_the_listing_that_already_happened():
    """`StorageClass` vinha junto no `ListObjectsV2` e era descartado.

    O `bytes_by_class` do bucket vem do CloudWatch e não sabe separar prefixo —
    e é no prefixo que a transição age.
    """
    prefixo, _ = _coletar(
        [
            _objeto(400, 1000),                       # sem campo = STANDARD
            _objeto(300, 500, "STANDARD"),
            _objeto(200, 250, "GLACIER"),
        ]
    )

    assert prefixo.bytes_by_class == {"STANDARD": 1500.0, "GLACIER": 250.0}
    assert prefixo.object_count_by_class == {"STANDARD": 2, "GLACIER": 1}


def test_the_age_histogram_uses_the_retention_minimums_as_boundaries():
    """As faixas espelham os mínimos de retenção: 30 IA, 90 Glacier, 180 Deep."""
    prefixo, _ = _coletar(
        [_objeto(10, 100), _objeto(60, 200), _objeto(120, 400), _objeto(700, 800)]
    )

    assert prefixo.bytes_by_age == {
        "0-30": 100.0,
        "30-90": 200.0,
        "90-180": 400.0,
        "365+": 800.0,
    }


@pytest.mark.parametrize(
    ("dias", "faixa"),
    [(0, "0-30"), (29, "0-30"), (30, "30-90"), (89, "30-90"), (90, "90-180"),
     (180, "180-365"), (364, "180-365"), (365, "365+"), (5000, "365+")],
)
def test_every_age_lands_in_exactly_one_bucket(dias, faixa):
    assert age_bucket(dias) == faixa


def test_the_average_object_size_is_measured_because_small_files_get_worse():
    """Abaixo de 128 KB a cobrança mínima de IA/Glacier encarece a transição."""
    prefixo, _ = _coletar([_objeto(400, 1000), _objeto(300, 3000)])

    assert prefixo.average_object_bytes == 2000


def test_an_unlisted_prefix_aggregates_nothing():
    class Negado(S3Falso):
        def list_objects_v2(self, **_kwargs):
            raise RuntimeError("AccessDenied")

    client = Negado({})
    prefixos = s3_collector.collect_prefixes(
        client,
        known=[("s3://lake/vendas/", "table_location", "db.vendas")],
        window=JANELA,
        stale_after_days=30,
        max_pages=None,
    )

    prefixo = prefixos[0]
    assert prefixo.listing_complete is False
    assert prefixo.object_count is None
    assert prefixo.bytes_by_class == {}
    assert prefixo.average_object_bytes is None


# ---------------------------------------------------------------------------
# O paralelismo
# ---------------------------------------------------------------------------


def _muitos_prefixos(quantidade: int):
    objetos = {f"p{i}/": [_objeto(400, 100)] for i in range(quantidade)}
    known = [
        (f"s3://lake/p{i}/", "table_location", f"db.t{i}") for i in range(quantidade)
    ]
    return objetos, known


def test_parallel_listing_preserves_input_order():
    """Sem ordem estável, dois scans do mesmo inventário divergem no diff."""
    objetos, known = _muitos_prefixos(12)
    client = S3Falso(objetos, por_pagina=1000)

    prefixos = s3_collector.collect_prefixes(
        client, known=known, window=JANELA, stale_after_days=30,
        max_pages=None, workers=8,
    )

    assert [item.prefix for item in prefixos] == [f"p{i}/" for i in range(12)]


def test_parallel_listing_actually_overlaps_calls():
    """Um teste de paralelismo que roda em série não prova nada."""
    objetos, known = _muitos_prefixos(12)
    client = S3Falso(objetos, por_pagina=1, latencia=0.02)

    s3_collector.collect_prefixes(
        client, known=known, window=JANELA, stale_after_days=30,
        max_pages=None, workers=8,
    )

    assert client.pico_de_concorrencia > 1


def test_limited_listing_can_overlap_without_crossing_the_page_ceiling():
    objetos, known = _muitos_prefixos(12)
    client = S3Falso(objetos, por_pagina=1, latencia=0.02)

    prefixos = s3_collector.collect_prefixes(
        client, known=known, window=JANELA, stale_after_days=30,
        max_pages=MAX_LIST_PAGES, workers=8,
    )

    assert client.pico_de_concorrencia > 1
    assert all(item.list_requests <= MAX_LIST_PAGES for item in prefixos)


def test_one_worker_really_runs_in_series():
    """O contraste: sem ele, o teste acima passaria com qualquer número."""
    objetos, known = _muitos_prefixos(12)
    client = S3Falso(objetos, por_pagina=1, latencia=0.02)

    s3_collector.collect_prefixes(
        client, known=known, window=JANELA, stale_after_days=30,
        max_pages=None, workers=1,
    )

    assert client.pico_de_concorrencia == 1


def test_serial_and_parallel_produce_the_same_aggregate():
    objetos, known = _muitos_prefixos(6)

    serie = s3_collector.collect_prefixes(
        S3Falso(objetos, por_pagina=1000), known=known, window=JANELA,
        stale_after_days=30, max_pages=None, workers=1,
    )
    paralelo = s3_collector.collect_prefixes(
        S3Falso(objetos, por_pagina=1000), known=known, window=JANELA,
        stale_after_days=30, max_pages=None, workers=8,
    )

    assert serie == paralelo


def test_the_listing_cost_is_counted_so_it_can_be_declared():
    """O desenho evita `ListBuckets` para não gastar descobrindo custo.

    A listagem completa contraria isso a pedido — então declara o que gastou,
    em vez de a conta aparecer na fatura do mês seguinte sem explicação.
    """
    prefixo, client = _coletar([_objeto(dia, 100) for dia in range(2500)])

    # 2500 objetos são três requests de mil.
    assert prefixo.list_requests == 3
    assert client.chamadas >= 1
