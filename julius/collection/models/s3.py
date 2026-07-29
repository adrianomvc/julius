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

#: Faixas de idade desde a última escrita, em dias. Espelham as faixas que o
#: próprio Storage Class Analysis usa para agrupar objetos, e os mínimos de
#: retenção das classes: 30 dias para IA, 90 para Glacier Flexible, 180 para
#: Deep Archive. Objeto mais novo que o mínimo da classe alvo **encarece** ao
#: ser movido, porque a AWS cobra o período inteiro mesmo assim.
AGE_BUCKETS = ((0, 30), (30, 90), (90, 180), (180, 365), (365, None))

#: Distribuição estável de tamanho usada para calcular a cobrança mínima das
#: classes frias sem persistir nenhuma chave de objeto.
SIZE_BUCKETS = (
    (0, 0, "zero"),
    (1, 128 * 1024, "1-128kb"),
    (128 * 1024, 1024 * 1024, "128kb-1mb"),
    (1024 * 1024, 64 * 1024**2, "1-64mb"),
    (64 * 1024**2, 128 * 1024**2, "64-128mb"),
    (128 * 1024**2, None, "128mb+"),
)


def age_bucket(days: int) -> str:
    """A faixa de `AGE_BUCKETS` em que uma idade cai, como rótulo estável."""
    for inicio, fim in AGE_BUCKETS:
        if fim is None:
            return f"{inicio}+"
        if days < fim:
            return f"{inicio}-{fim}"
    return f"{AGE_BUCKETS[-1][0]}+"


def size_bucket(size: int) -> str:
    """Faixa de tamanho de um objeto, com limites inclusivo/exclusivo."""
    if size <= 0:
        return "zero"
    for inicio, fim, rotulo in SIZE_BUCKETS[1:]:
        if size >= inicio and (fim is None or size < fim):
            return rotulo
    return "128mb+"


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
    #: Bytes por `StorageClass` do próprio objeto, agregado da listagem. É mais
    #: fino que o `bytes_by_class` do bucket, que vem do CloudWatch e não sabe
    #: separar prefixo — e é no prefixo que a recomendação de transição age.
    bytes_by_class: dict[str, float] = field(default_factory=dict)
    object_count_by_class: dict[str, int] = field(default_factory=dict)
    #: Matrizes agregadas que permitem calcular o tamanho faturável em uma
    #: classe alvo. Continuam sem revelar nomes/chaves de objetos.
    bytes_by_class_size: dict[str, dict[str, float]] = field(default_factory=dict)
    object_count_by_class_size: dict[str, dict[str, int]] = field(
        default_factory=dict
    )
    #: Bytes por faixa de idade desde a última **escrita** (`LastModified`), nas
    #: faixas de `AGE_BUCKETS`. Não é idade desde a última leitura: o S3 não
    #: expõe isso. Serve para dimensionar o candidato, nunca para concluir
    #: sozinho que o dado é frio — ver `S3BucketConfig.last_access_source`.
    bytes_by_age: dict[str, float] = field(default_factory=dict)
    object_count_by_age: dict[str, int] = field(default_factory=dict)
    bytes_by_size: dict[str, float] = field(default_factory=dict)
    object_count_by_size: dict[str, int] = field(default_factory=dict)
    #: Marcadores de diretório com zero byte não entram no tamanho médio.
    nonzero_object_count: int | None = None
    #: Tamanho médio do objeto. Abaixo de 128 KB a cobrança mínima de IA e
    #: Glacier torna a transição mais cara que o Standard, e a regra precisa
    #: saber disso antes de recomendar.
    average_object_bytes: int | None = None
    #: Evidência normalizada de leitura. `None` nos contadores significa fonte
    #: não consultada; zero significa consultada e nenhuma leitura observada.
    last_read_at: str = ""
    read_requests_window: int | None = None
    #: Subconjunto de `read_requests_window` que é GET de objeto. HEAD e
    #: Select permanecem separados porque não têm necessariamente a mesma
    #: cobrança no Cost Explorer.
    get_requests_window: int | None = None
    head_requests_window: int | None = None
    select_requests_window: int | None = None
    bytes_read_window: int | None = None
    read_coverage_days: int = 0
    access_source: str = ""
    access_quality: str = "unavailable"
    inventory_data_through: str = ""
    #: Requests de `ListObjectsV2` gastos para montar este agregado.
    list_requests: int = 0

    @property
    def location(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


#: Fontes de último acesso, da mais precisa para a menos — e todas exigem
#: configuração prévia no bucket. **O S3 não tem last access time nativo por
#: objeto**: `LastModified` é a última *escrita*. Sem uma destas ligada, não há
#: como saber se um arquivo é lido, só quando ele foi gravado.
LAST_ACCESS_SOURCES = (
    "server_access_logs",   # GET por objeto; entrega grátis, best-effort
    "storage_lens",         # requests por prefixo; advanced tier, pago
    "storage_class_analysis",  # razão de acesso por faixa de idade; só Standard→IA
    "intelligent_tiering",  # a AWS mede e move sozinha; taxa por objeto
)


@dataclass
class S3BucketConfig:
    """O que está ligado no bucket, e o que isso permite afirmar.

    Existe para responder uma pergunta que o relatório não conseguia fazer:
    *dá para saber se estes arquivos são lidos?* Sem isso, uma recomendação de
    mover dado para classe fria se apoiaria em `LastModified` — a data da última
    escrita — e trocaria a classe de um arquivo que é lido todo dia.

    `None` = não consultado (a chamada foi negada ou nem aconteceu). Lista vazia
    = consultado e não há nada configurado. A diferença decide se a regra pode
    afirmar que o bucket não tem lifecycle ou apenas que não olhou.
    """

    bucket: str
    access_logging_enabled: bool | None = None
    access_log_target_bucket: str = ""
    access_log_target_prefix: str = ""
    storage_class_analysis_ids: list[str] | None = None
    intelligent_tiering_ids: list[str] | None = None
    #: Regras de lifecycle declaradas. `NoSuchLifecycleConfiguration` é resposta,
    #: não erro: significa `[]`, e é o que autoriza dizer "não há lifecycle".
    lifecycle_rules: list[dict] | None = None
    metadata_table_enabled: bool | None = None
    storage_lens_enabled: bool | None = None

    @property
    def last_access_source(self) -> str:
        """Qual evidência de último acesso este bucket permite, ou `"none"`."""
        if self.access_logging_enabled:
            return "server_access_logs"
        if self.storage_lens_enabled:
            return "storage_lens"
        if self.storage_class_analysis_ids:
            return "storage_class_analysis"
        if self.intelligent_tiering_ids:
            return "intelligent_tiering"
        return "none"

    @property
    def transitions_automatically(self) -> bool:
        """Já há automação movendo objeto de classe neste bucket?

        Recomendar transição onde o Intelligent-Tiering ou uma regra de
        lifecycle já age é recomendar trabalho que a AWS faz sozinha — e pior,
        cobrar por ele duas vezes na conta da economia.
        """
        if self.intelligent_tiering_ids:
            return True
        return any(
            rule.get("Transitions") or rule.get("NoncurrentVersionTransitions")
            for rule in self.lifecycle_rules or ()
        )

    def transitions_prefix(self, prefix: str) -> bool:
        """Se uma automação de transição alcança este prefixo específico."""
        if self.intelligent_tiering_ids:
            return True
        return any(
            (rule.get("Transitions") or rule.get("NoncurrentVersionTransitions"))
            and _lifecycle_matches(rule, prefix)
            for rule in self.lifecycle_rules or ()
        )

    def expiration_days_for_prefix(self, prefix: str) -> int | None:
        """Menor expiração aplicável; evita recomendar transição sem payback."""
        values: list[int] = []
        for rule in self.lifecycle_rules or ():
            if not _lifecycle_matches(rule, prefix):
                continue
            expiration = rule.get("Expiration") or {}
            if isinstance(expiration, dict) and expiration.get("Days") is not None:
                values.append(int(expiration["Days"]))
        return min(values) if values else None


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
class S3CostLine:
    """Custo e quantidade preservados por usage type e unidade."""

    usage_type: str
    bucket: str
    cost: float = 0.0
    usage_quantity: float | None = None
    usage_unit: str = ""


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
    lines: list[S3CostLine] = field(default_factory=list)
    unknown_usage_types: list[str] = field(default_factory=list)
    cost_quality: str = "unavailable"
    allocation_version: str = ""
    gaps: list[str] = field(default_factory=list)

    def cost_for(self, names: frozenset[str] | set[str]) -> float:
        return round(
            sum(value for name, value in self.buckets.items() if name in names), 6
        )

    def quantity_for(self, names: frozenset[str] | set[str]) -> float | None:
        """Soma quantidades somente quando todas são contagens de requests."""
        relevant = [line for line in self.lines if line.bucket in names]
        if not relevant:
            return None
        if any(
            line.usage_quantity is None
            or line.usage_unit.strip().lower() not in {"request", "requests"}
            for line in relevant
        ):
            return None
        return round(sum(line.usage_quantity or 0.0 for line in relevant), 6)

    def unit_cost_for(
        self, names: frozenset[str] | set[str]
    ) -> float | None:
        """Custo faturado por request, sem misturar unidades incompatíveis."""
        quantity = self.quantity_for(names)
        cost = self.cost_for(names)
        if quantity is None or quantity <= 0 or cost <= 0:
            return None
        return cost / quantity


def _lifecycle_matches(rule: dict, prefix: str) -> bool:
    """Filtro simples de prefixo; filtros por tag ficam conservadoramente fora."""
    filtro = rule.get("Filter") or {}
    declared = rule.get("Prefix")
    if isinstance(filtro, dict):
        declared = filtro.get("Prefix", declared)
        if filtro.get("Tag") or filtro.get("And"):
            return False
    if declared in (None, ""):
        return True
    normalized = str(prefix or "").lstrip("/")
    return normalized.startswith(str(declared).lstrip("/"))
