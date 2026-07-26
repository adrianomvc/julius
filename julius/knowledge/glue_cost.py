"""Taxonomia de cobrança do Glue.

    O Cost Explorer entrega `USAGE_TYPE`; esta é a tradução para buckets com
    significado. Conhecimento de domínio, consumido pela coleta — que o recebe
    como parâmetro em vez de importar daqui."""

from __future__ import annotations

# Classificação versionada de `USAGE_TYPE` do Cost Explorer em buckets de
# cobrança Glue. A ordem importa: o primeiro marcador encontrado no usage type
# normalizado (minúsculo, sem prefixo de região) define o bucket.
GLUE_USAGE_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("databrew", "databrew"),
    ("dataquality", "data_quality"),
    ("data-quality", "data_quality"),
    ("crawler", "crawler"),
    ("session", "interactive_session"),
    ("notebook", "interactive_session"),
    ("flex", "flex"),
    ("objectstorage", "catalog"),
    ("request", "catalog"),
    ("catalog", "catalog"),
    ("dpu-hour", "etl_job"),
    ("dpu_hour", "etl_job"),
    ("etl", "etl_job"),
)

# Buckets rateados a ativos coletados e buckets que permanecem sem atribuição.
ALLOCATED_GLUE_BUCKETS: frozenset[str] = frozenset(
    {"etl_job", "flex", "crawler", "interactive_session", "databrew"}
)
UNATTRIBUTED_GLUE_BUCKETS: frozenset[str] = frozenset(
    {"catalog", "data_quality", "other"}
)
