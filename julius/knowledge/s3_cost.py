"""Taxonomia de cobrança do S3.

A separação que importa aqui é entre **armazenamento** e **request**, porque as
recomendações agem em lados diferentes da fatura: apagar objeto reduz
armazenamento e não toca em request; compactar arquivo pequeno reduz request e
quase não move armazenamento. Somar os dois numa linha só esconderia qual ação
rende o quê — e o Julius precisa dizer isso para a recomendação ser acionável.

Transferência de dados fica num bucket próprio e não é rateada a prefixo: ela
depende de quem lê de onde, não de onde o dado está.
"""

from __future__ import annotations

# A ordem importa: o primeiro marcador encontrado no usage type normalizado
# define o bucket. `timedstorage` precisa vir antes de `storage` genérico.
S3_USAGE_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("earlydelete", "early_delete"),
    ("timedstorage-glacier", "storage_archive"),
    ("timedstorage-deeparchive", "storage_archive"),
    ("timedstorage-ia", "storage_infrequent"),
    ("timedstorage-onezone", "storage_infrequent"),
    ("timedstorage-int", "storage_intelligent"),
    ("timedstorage", "storage_standard"),
    ("requests-tier1", "requests_write"),
    ("requests-tier2", "requests_read"),
    ("requests-tier", "requests_other"),
    ("requests", "requests_other"),
    ("bytes", "data_transfer"),
    ("datatransfer", "data_transfer"),
    ("select", "select_scan"),
    ("inventory", "inventory"),
    ("monitorandautomation", "intelligent_tiering_monitoring"),
)

#: Cobrança que cai quando um objeto deixa de existir. É o único conjunto cuja
#: economia uma recomendação de exclusão pode reivindicar.
S3_STORAGE_BUCKETS: frozenset[str] = frozenset(
    {
        "storage_standard",
        "storage_infrequent",
        "storage_intelligent",
        "storage_archive",
    }
)

#: Cobrança por chamada. Cai quando o número de objetos cai — é o que compactar
#: arquivo pequeno recupera, e o que apagar objeto frio não recupera.
S3_REQUEST_BUCKETS: frozenset[str] = frozenset(
    {"requests_read", "requests_write", "requests_other"}
)

#: Não pertence a nenhum prefixo em particular.
UNATTRIBUTED_S3_BUCKETS: frozenset[str] = frozenset(
    {
        "data_transfer",
        "select_scan",
        "inventory",
        "intelligent_tiering_monitoring",
        "early_delete",
        "other",
    }
)
