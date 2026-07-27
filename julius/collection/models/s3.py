"""Inventário de S3: panorama por bucket e detalhe por prefixo conhecido.

Listar um data lake cobra por request, e a coleta não pode custar dinheiro para
descobrir custo. Por isso o modelo tem dois níveis diferentes de propósito:

`S3Bucket` vem do CloudWatch — `BucketSizeBytes` e `NumberOfObjects`, diários e
gratuitos, sem listar um objeto sequer. Serve para dizer o tamanho e a
distribuição por classe de armazenamento.

`S3Prefix` vem de listagem, e só nos caminhos que o inventário já conhece: a
saída de resultados do workgroup Athena, o prefixo de event log de cada job, a
location de cada tabela do catálogo. É onde as regras que recomendam apagar
arquivo atuam, e é bounded por construção.

Nada aqui guarda chave de objeto. O que sobe é agregado: quantos, quanto, quão
antigo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Como o prefixo foi descoberto — decide qual regra o avalia.
PREFIX_KINDS = (
    "athena_results",   # ResultConfiguration.OutputLocation do workgroup
    "spark_logs",       # job.spark_event_logs_path
    "table_location",   # StorageDescriptor.Location do catálogo
    "staging",          # _temporary/, .spark-staging/, _$folder$
)


@dataclass
class S3Bucket:
    """Tamanho e composição de um bucket, do CloudWatch."""

    name: str
    #: Bytes por `StorageType` do CloudWatch (`StandardStorage`,
    #: `StandardIAStorage`, `GlacierInstantRetrievalStorage`, …). Vazio quando a
    #: métrica não foi consultada — e aí nenhuma regra afirma sobre tamanho.
    bytes_by_class: dict[str, float] = field(default_factory=dict)
    #: `None` = métrica não consultada; `0` = consultada e o bucket está vazio.
    object_count: int | None = None
    versioning_enabled: bool | None = None
    observed_days: int = 0
    coverage_days: int = 0
    owner_tag: str | None = None
    #: Storage rateado da cobrança real, quando ela existe.
    allocated_storage_cost: float | None = None
    cost_quality: str = "unavailable"

    @property
    def total_bytes(self) -> float:
        return sum(self.bytes_by_class.values())


@dataclass
class S3Prefix:
    """Agregado de um prefixo listado, sem nenhuma chave de objeto."""

    bucket: str
    prefix: str
    kind: str = "table_location"
    #: `None` = não listado. Zero objetos é resultado de listagem, não ausência
    #: dela — a diferença decide se a regra pode afirmar.
    object_count: int | None = None
    total_bytes: int | None = None
    oldest_object_age_days: int | None = None
    #: Objetos além do limiar de idade da regra que avalia este prefixo.
    stale_object_count: int | None = None
    stale_bytes: int | None = None
    #: Falso quando a paginação foi cortada: a evidência é parcial e o achado
    #: precisa dizer isso em vez de afirmar sobre o que não viu.
    listing_complete: bool = True
    #: Ativo de origem — job, tabela ou workgroup — para religar ao dono.
    source_asset: str = ""
    owner_tag: str | None = None

    @property
    def location(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


@dataclass
class S3MultipartUpload:
    """Upload iniciado e nunca concluído.

    Cobra armazenamento pelas partes já enviadas e **não aparece na listagem de
    objetos** — é desperdício que nenhuma inspeção pelo console encontra.
    """

    bucket: str
    #: Agregado por bucket: quantos uploads e há quanto tempo o mais antigo.
    upload_count: int = 0
    total_bytes: int | None = None
    oldest_age_days: int | None = None
    listing_complete: bool = True


@dataclass
class S3CostCoverage:
    """Cobrança S3 da janela, separada entre armazenamento e requests.

    A separação importa porque as recomendações agem em lados diferentes:
    apagar objeto reduz armazenamento; compactar arquivo pequeno reduz request.
    Somar os dois numa linha só esconderia qual ação rende o quê.
    """

    period_start: str = ""
    data_through: str = ""
    cost_metric: str = ""
    currency: str = "USD"
    net_cost: float | None = None
    buckets: dict[str, float] = field(default_factory=dict)
    unknown_usage_types: list[str] = field(default_factory=list)
    cost_quality: str = "unavailable"
    allocation_version: str = ""
    gaps: list[str] = field(default_factory=list)

    def cost_for(self, names: frozenset[str] | set[str]) -> float:
        return round(
            sum(value for name, value in self.buckets.items() if name in names), 6
        )
