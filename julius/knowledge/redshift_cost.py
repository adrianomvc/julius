"""Taxonomia de cobrança do Redshift.

Mesma disciplina do Glue: o Cost Explorer entrega `USAGE_TYPE` e não expõe
dimensão de recurso, então a cobrança chega agregada e precisa ser traduzida
para buckets com significado antes de virar custo por cluster.

A separação que importa aqui é entre **compute** e o resto. Um cluster pausado
para de cobrar compute e continua cobrando armazenamento — é por isso que a
economia de um cluster ocioso é o compute inteiro, e não a linha toda da fatura.
Confundir os dois superestimaria a recomendação exatamente onde ela precisa ser
confiável.
"""

from __future__ import annotations

# A ordem importa: o primeiro marcador encontrado no usage type normalizado
# define o bucket.
REDSHIFT_USAGE_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("concurrencyscaling", "concurrency_scaling"),
    ("concurrency-scaling", "concurrency_scaling"),
    ("spectrum", "spectrum"),
    ("rms", "managed_storage"),
    ("managedstorage", "managed_storage"),
    ("storage", "managed_storage"),
    ("backup", "backup"),
    ("snapshot", "backup"),
    ("serverless", "rpu_hours"),
    ("rpu", "rpu_hours"),
    ("node", "node_hours"),
    ("nodes", "node_hours"),
)

#: Compute que para de ser cobrado quando o cluster pausa ou é removido. É o
#: único bucket cuja economia um achado de ociosidade pode reivindicar inteira.
REDSHIFT_COMPUTE_BUCKETS: frozenset[str] = frozenset({"node_hours", "rpu_hours"})

#: Cobrança que sobrevive à pausa, ou que não pertence a um cluster específico.
UNATTRIBUTED_REDSHIFT_BUCKETS: frozenset[str] = frozenset(
    {"managed_storage", "backup", "spectrum", "concurrency_scaling", "other"}
)
