"""Nenhuma leitura da coleta é ilimitada, e a que trunca diz que truncou.

Duas paginações rodavam sem teto — a listagem de objetos S3 de uma tabela e a
contagem de partições dela — e as duas rodam *por tabela lida por query*. Num
data lake isso é a diferença entre uma chamada e mil, para responder perguntas
que se decidem nas primeiras centenas de itens.

O teto só é aceitável porque nenhum veredito depende do que vem depois dele:
`SMALL_FILE_MIN_COUNT` são 100 objetos e a recomendação de partition projection
começa em 1.000 partições, contra um teto de cinco páginas de mil. O que muda é
o preço de chegar à mesma conclusão — e quem trunca registra evidência parcial
em vez de devolver um número que parece completo.
"""

from __future__ import annotations

from julius.collection.collectors.athena import catalog
from julius.collection.collectors.athena.telemetry import AthenaTelemetry
from julius.collection.collectors.s3_evidence import MAX_LIST_PAGES, object_evidence
from julius.collection.models import AthenaCoverage


class _Pages:
    """Paginador infinito: quem não parar sozinho não para."""

    def __init__(self, page, *, key):
        self.page = page
        self.key = key
        self.served = 0

    def paginate(self, **_kwargs):
        while True:
            self.served += 1
            yield {self.key: self.page}


class _S3:
    def __init__(self, page):
        self.pages = _Pages(page, key="Contents")

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self.pages


class _Glue:
    def __init__(self, page):
        self.pages = _Pages(page, key="Partitions")

    def get_paginator(self, name):
        assert name == "get_partitions"
        return self.pages


def test_listing_objects_stops_at_the_page_ceiling():
    s3 = _S3([{"Key": f"parte-{index}.parquet", "Size": 1024} for index in range(1000)])

    evidence = object_evidence(s3, "s3://lake/tabela/")

    assert s3.pages.served == MAX_LIST_PAGES
    assert evidence["complete"] is False
    assert evidence["count"] == MAX_LIST_PAGES * 1000


def test_a_truncated_listing_still_reaches_the_small_files_verdict():
    """O limiar são 100 objetos; o teto são 5.000. Truncar não muda a conclusão."""
    s3 = _S3([{"Key": f"parte-{index}.json", "Size": 1024} for index in range(1000)])

    evidence = object_evidence(s3, "s3://lake/tabela/")

    assert evidence["small_files"] is True


def test_a_listing_that_ends_on_its_own_is_complete():
    class _Finite:
        def get_paginator(self, _name):
            class _One:
                def paginate(self, **_kwargs):
                    yield {"Contents": [{"Key": "a.parquet", "Size": 4096}]}

            return _One()

    evidence = object_evidence(_Finite(), "s3://lake/tabela/")

    assert evidence["complete"] is True
    assert evidence["count"] == 1


def test_counting_partitions_stops_at_the_page_ceiling():
    glue = _Glue([{"Values": [str(index)]} for index in range(1000)])

    total, complete = catalog.count_partitions(glue, "db", "tabela")

    assert glue.pages.served == catalog.MAX_PARTITION_PAGES
    assert complete is False
    # Quem trunca já passou com folga do limiar que faz a recomendação disparar.
    assert total >= catalog._PARTITION_PROJECTION_MIN_PARTITIONS


def test_counting_partitions_of_a_small_table_is_complete():
    class _Finite:
        def get_paginator(self, _name):
            class _One:
                def paginate(self, **_kwargs):
                    yield {"Partitions": [{"Values": ["2026-07-27"]}]}

            return _One()

    assert catalog.count_partitions(_Finite(), "db", "tabela") == (1, True)


def test_truncated_evidence_is_partial_not_broken():
    """Fonte que respondeu até o teto não é fonte que falhou."""
    telemetry = AthenaTelemetry(AthenaCoverage())
    telemetry.used("Athena S3")
    telemetry.partial(
        "Athena S3", category="bounded_or_incomplete", detail="listagem limitada em t"
    )

    entry = next(item for item in telemetry.entries() if item.source == "Athena S3")
    assert entry.status == "partial"
    assert entry.error_category == "bounded_or_incomplete"
    assert telemetry.coverage.gaps == ["Athena S3: listagem limitada em t"]


def test_the_same_truncation_in_many_tables_is_reported_once():
    """Quinhentas tabelas truncadas não são quinhentas linhas de relatório."""
    telemetry = AthenaTelemetry(AthenaCoverage())
    for index in range(500):
        telemetry.partial(
            "Athena S3",
            category="bounded_or_incomplete",
            detail=f"listagem limitada em tabela-{index}",
        )

    assert len(telemetry.coverage.gaps) == 1


def test_a_failure_outranks_a_truncation_on_the_same_source():
    telemetry = AthenaTelemetry(AthenaCoverage())
    telemetry.partial("Athena S3", category="bounded_or_incomplete", detail="limitada")
    telemetry.failed("Athena S3", PermissionError("negado"))

    entry = next(item for item in telemetry.entries() if item.source == "Athena S3")
    assert entry.status == "unavailable"
    assert entry.error_category == "permission_denied"
