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
from julius.findings.opportunity import Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal
from julius.knowledge.rules.s3.request_cost import (
    request_estimation,
    request_evidence,
)

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
    return [
        _achado(account, prefixo, config, scan_id)
        for prefixo in getattr(account, "s3_prefixes", None) or ()
        if _e_tabela_com_arquivo_pequeno(prefixo, config)
        and _nome_da_tabela(prefixo) not in ja_cobertas
        and (
            getattr(account, "s3_mode", "proposal") != "evidence_only"
            or _processo_identificado(account, prefixo)
        )
    ]


def _processo_identificado(account: Account, prefixo: S3Prefix) -> bool:
    nome = _nome_da_tabela(prefixo)
    if not nome:
        return False
    return any(
        str(table.name).strip().lower() == nome
        and bool(table.written_by or table.used_by_accounts)
        for table in getattr(account, "tables", ()) or ()
    )


def signals(account: Account, config: Config) -> list[Signal]:
    """No Consumer, arquivo pequeno sem processo conhecido é apenas evidência."""
    if getattr(account, "s3_mode", "proposal") != "evidence_only":
        return []
    return [
        Signal(
            kind="inventory_integrity",
            rule_id=RULE_ID,
            asset_type="s3_prefix",
            asset_name=prefixo.location,
            observation=(
                f"{prefixo.object_count} objetos com média de "
                f"{(prefixo.average_object_bytes or 0) / _MB:.1f} MiB."
            ),
            question="Qual job ou consumidor deve compactar esses arquivos na origem?",
            missing_evidence=["processo produtor ou consumidor da tabela"],
            doc_links=[_DOC_PERFORMANCE, _DOC_COMPACTION],
        )
        for prefixo in getattr(account, "s3_prefixes", ()) or ()
        if _e_tabela_com_arquivo_pequeno(prefixo, config)
        and not _processo_identificado(account, prefixo)
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


def _achado(
    account: Account,
    prefixo: S3Prefix,
    config: Config,
    scan_id: str,
) -> Opportunity:
    media_mb = (prefixo.average_object_bytes or 0) / _MB
    alvo_mb = config.thresholds.s3_compaction_target_bytes / _MB
    parcial = not prefixo.listing_complete
    est = request_estimation(
        account,
        [prefixo],
        config,
        method="s3_small_files_requests_v2",
    )
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
                *request_evidence(account, [prefixo]),
            ],
            sources=["S3 ListObjectsV2", "Glue GetTables", "Cost Explorer"],
            observed_runs=1,
            coverage_days=config.thresholds.min_coverage_days,
            has_optional_metrics=est.saving_quality != "unavailable",
            owner_tag=prefixo.owner_tag,
        ),
        est,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    faltando = []
    if prefixo.get_requests_window is None:
        faltando.append(
            "GETs desta tabela na janela por Server Access Logs"
        )
    if est.saving_quality == "unavailable":
        faltando.append(
            "custo e UsageQuantity compatíveis de Requests-Tier2"
        )
    elif prefixo.access_quality == "best_effort":
        faltando.append(
            "Server Access Logs têm entrega best-effort; validar a redução na janela seguinte"
        )
    if parcial:
        faltando.append("listagem completa do prefixo: a contagem medida é piso")
    opportunity.missing_evidence = faltando
    return opportunity
