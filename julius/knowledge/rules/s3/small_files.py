"""Arquivo pequeno nas tabelas da conta, vistas pelo S3 e não pelo Athena.

A análise de arquivo pequeno já existia, e chegava a uma tabela por um caminho
só: `ATHENA-SMALL-FILES`, cuja evidência vem de `enrich_catalog`, que percorre
`AthenaQuery.reads_tables`. **Tabela não consultada por Athena na janela nunca
era avaliada** — e no Data Mesh isso é a maioria: tabela escrita por Glue e lida
por Spark ou EMR não aparece em nenhum `GetQueryExecution`.

A evidência para avaliá-las já era coletada. `CatalogScope` seleciona os bancos
da conta (`database_db_compartilhado_consumer_<conta>`, `workspace_db`,
`sagemaker_featurestore`), `collect_tables` traz a `location` de cada tabela, e
`known_prefixes` transforma cada uma num prefixo de kind `table_location` que a
coleta de S3 lista. O que faltava era a regra.

**Compactar reduz request, não armazenamento.** Os bytes continuam lá; o que cai
é o número de chamadas — `S3_REQUEST_BUCKETS` na taxonomia de cobrança. Por isso
o baseline aqui é a fatia de request da fatura, e não o armazenamento que as
outras regras de S3 usam. Reivindicar armazenamento seria prometer uma economia
que a compactação não entrega.

O ganho é **estratégico**: compactar exige reescrever dado e validar consumidor,
e quanto se recupera depende de quantas leituras acontecem sobre esses objetos —
o que só o histórico de acesso diria. A regra dimensiona o problema e nomeia a
ação; não anuncia dinheiro no bolso.
"""

from __future__ import annotations

from julius.collection.models import Account, S3Prefix
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.knowledge.s3_cost import S3_REQUEST_BUCKETS

RULE_ID = "S3-SMALL-FILES"

_MB = 1024**2

_DOC_PERFORMANCE = (
    "https://docs.aws.amazon.com/athena/latest/ug/performance-tuning-data-optimization-techniques.html"
)
_DOC_COMPACTION = (
    "https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-etl-format.html"
)


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    ja_cobertas = _tabelas_cobertas_pelo_athena(account)
    custo_por_objeto = _request_cost_per_object(account)
    return [
        _achado(account, prefixo, config, scan_id, custo_por_objeto)
        for prefixo in getattr(account, "s3_prefixes", None) or ()
        if _e_tabela_com_arquivo_pequeno(prefixo, config)
        and _nome_da_tabela(prefixo) not in ja_cobertas
    ]


def _e_tabela_com_arquivo_pequeno(prefixo: S3Prefix, config: Config) -> bool:
    """Prefixo de tabela com muitos objetos e média abaixo do limiar.

    `average_object_bytes is None` significa prefixo não listado — e aí não há o
    que afirmar. A contagem mínima existe porque compactar dez arquivos não paga
    o trabalho de validar os consumidores da tabela.
    """
    if prefixo.kind != "table_location":
        return False
    if prefixo.average_object_bytes is None or prefixo.object_count is None:
        return False
    limiares = config.thresholds
    return (
        prefixo.object_count >= limiares.s3_small_files_min_count
        and prefixo.average_object_bytes < limiares.s3_small_file_max_bytes
    )


def _nome_da_tabela(prefixo: S3Prefix) -> str:
    """A tabela que originou o prefixo, em minúsculas — o catálogo ignora caixa."""
    return str(getattr(prefixo, "source_asset", "") or "").strip().lower()


def _tabelas_cobertas_pelo_athena(account: Account) -> frozenset[str]:
    """Tabelas que o `ATHENA-SMALL-FILES` já denuncia.

    Sem isto, a mesma tabela apareceria duas vezes no ranking com âncoras
    diferentes — uma no `query_id`, outra no prefixo — e o leitor não teria como
    saber que é o mesmo trabalho contado em dobro.
    """
    return frozenset(
        str(nome).strip().lower()
        for query in getattr(account, "athena_queries", None) or ()
        if query.small_files_confirmed
        for nome in query.reads_tables or ()
    )


def _request_cost_per_object(account: Account) -> float | None:
    """USD de request na janela por objeto listado, ou `None`.

    O rateio é proporcional à contagem de objetos, porque é ela que determina
    quantas chamadas uma leitura faz. Sem cobrança de request classificada não
    há tarifa implícita, e o achado sai sem cifra em vez de com uma inventada.
    """
    coverage = getattr(account, "s3_cost_coverage", None)
    if coverage is None:
        return None
    custo = coverage.cost_for(S3_REQUEST_BUCKETS)
    objetos = sum(
        prefixo.object_count or 0
        for prefixo in getattr(account, "s3_prefixes", None) or ()
    )
    if custo <= 0 or objetos <= 0:
        return None
    return custo / objetos


def _estimation(
    prefixo: S3Prefix, config: Config, custo_por_objeto: float | None
) -> Estimation:
    atual = prefixo.object_count or 0
    total = prefixo.total_bytes or 0
    alvo = max(1, -(-total // config.thresholds.s3_compaction_target_bytes))
    evitaveis = max(0, atual - alvo)

    if custo_por_objeto is None:
        return Estimation(
            method="s3_small_files_v1",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                "cobrança de request do S3 não classificada na janela",
                f"{atual} objetos poderiam virar ~{alvo} após compactação",
                "economia não quantificada sem cobrança rateada",
            ],
            saving_quality="unavailable",
            is_strategic=True,
        )

    baseline = custo_por_objeto * atual
    projetado = custo_por_objeto * alvo
    return Estimation(
        method="s3_small_files_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(projetado, 2),
        estimated_saving=round(baseline - projetado, 2),
        assumptions=[
            f"{atual} objetos compactados para ~{alvo} de "
            f"{config.thresholds.s3_compaction_target_bytes / _MB:.0f} MiB",
            "custo de request rateado por contagem de objetos, da fatura real",
            "reduz request; o armazenamento permanece e não entra na conta",
            f"{evitaveis} chamadas a menos por varredura completa da tabela",
        ],
        baseline_quality="allocated",
        # Quanto se recupera depende de quantas leituras acontecem sobre estes
        # objetos, e isso o inventário não mede. O ganho fica estratégico até
        # alguém comparar o antes e o depois.
        saving_quality="modeled_evidence",
        is_strategic=True,
    )


def _achado(
    account: Account,
    prefixo: S3Prefix,
    config: Config,
    scan_id: str,
    custo_por_objeto: float | None,
) -> Opportunity:
    media_mb = (prefixo.average_object_bytes or 0) / _MB
    alvo_mb = config.thresholds.s3_compaction_target_bytes / _MB
    parcial = not prefixo.listing_complete
    est = _estimation(prefixo, config, custo_por_objeto)
    tabela = getattr(prefixo, "source_asset", "") or prefixo.location

    opportunity = build(
        Finding(
            rule_id=RULE_ID,
            rule_version="1.0.0",
            asset_type="s3_prefix",
            asset_name=prefixo.location,
            title=f"Tabela '{tabela}' armazenada em arquivos pequenos",
            why=(
                f"{prefixo.object_count} objetos com média de {media_mb:.1f} MiB "
                f"sob '{prefixo.location}'. Cada leitura completa da tabela faz "
                "LIST e um GET por objeto, então o custo está em requests e em "
                "tempo de planejamento — não no volume armazenado."
            ),
            source_process=prefixo.source_asset or None,
        ),
        Recommendation(
            difficulty=3,
            action=f"Compactar os arquivos da tabela em blocos de ~{alvo_mb:.0f} MiB",
            how_to_apply=(
                "Reescrever a tabela compactada numa versão controlada e trocar "
                "depois de validar os consumidores. O Julius não reescreve dado "
                "— a ação é do time dono da tabela."
            ),
            how_to_validate=(
                "Comparar a linha de requests do S3 no Cost Explorer e o tempo "
                "de planejamento das consultas na janela seguinte."
            ),
            risks=[
                "compactação reescreve dado e exige aprovação separada",
                "consumidores que dependem do particionamento atual precisam ser validados",
                *(["listagem parcial: a contagem real pode ser maior"] if parcial else []),
            ],
            docs=[_DOC_PERFORMANCE, _DOC_COMPACTION],
            blocked=est.saving_quality == "unavailable",
        ),
        Evidence(
            items=[
                f"{prefixo.object_count} objetos no prefixo da tabela",
                f"tamanho médio {media_mb:.1f} MiB (limiar: "
                f"{config.thresholds.s3_small_file_max_bytes / _MB:.0f} MiB)",
                f"{(prefixo.total_bytes or 0) / 1024**3:.2f} GB no total",
                (
                    "listagem completa"
                    if prefixo.listing_complete
                    else "listagem truncada: evidência parcial"
                ),
            ],
            sources=["S3 ListObjectsV2", "Glue GetTables", "Cost Explorer"],
            observed_runs=1,
            coverage_days=config.thresholds.min_coverage_days,
            has_optional_metrics=custo_por_objeto is not None,
            owner_tag=prefixo.owner_tag,
        ),
        est,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    faltando = ["leituras desta tabela na janela: define quanto o request cai"]
    if parcial:
        faltando.append("listagem completa do prefixo: a contagem medida é piso")
    opportunity.missing_evidence = faltando
    return opportunity
