"""Detectores de Athena: (4) leitura excessiva, (5) sem filtro de partição."""

from __future__ import annotations

from julius.config import Config
from julius.estimation import athena as athena_est
from julius.inventory.model import Account, AthenaQuery
from julius.opportunities.base import Opportunity
from julius.opportunities.detectors._build import build

_DOC_PARTITION = "https://docs.aws.amazon.com/athena/latest/ug/partitions.html"
_DOC_REUSE = "https://docs.aws.amazon.com/athena/latest/ug/reusing-query-results.html"
_DOC_PERFORMANCE = "https://docs.aws.amazon.com/athena/latest/ug/performance-tuning.html"
_DOC_QUERY_OPT = "https://docs.aws.amazon.com/athena/latest/ug/performance-tuning-query-optimization-techniques.html"
_DOC_COLUMNAR = "https://docs.aws.amazon.com/athena/latest/ug/columnar-storage.html"
_DOC_COMPRESSION = "https://docs.aws.amazon.com/athena/latest/ug/compression-formats.html"
_DOC_PROJECTION = "https://docs.aws.amazon.com/athena/latest/ug/partition-projection.html"

_GB = 1024**3


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    th = config.thresholds
    for q in account.athena_queries:
        if q.modality not in {"on_demand", ""}:
            continue
        if q.failed_runs + q.cancelled_runs >= 3:
            out.append(_failures(account, q, config, scan_id))
        if q.small_files_confirmed:
            out.append(_small_files(account, q, config, scan_id))
        if q.selects_star and q.wide_tables:
            out.append(_select_star_wide(account, q, config, scan_id))
        if q.full_scan_confirmed:
            out.append(_full_table_scan(account, q, config, scan_id))
        if (
            q.unpartitioned_tables
            and q.total_table_bytes >= _GB
            and (q.recurring or q.data_scanned_bytes >= th.athena_high_scan_bytes)
        ):
            out.append(_table_not_partitioned(account, q, config, scan_id))
        if q.row_format_uncompressed:
            out.append(_uncompressed_row_format(account, q, config, scan_id))
        if q.columnar_uncompressed:
            out.append(_uncompressed_columnar(account, q, config, scan_id))
        if q.partition_projection_candidates and (
            q.recurring or q.p95_planning_ms >= 1000
        ):
            out.append(_partition_projection(account, q, config, scan_id))
        # Regra 5 — sem filtro de partição (tem precedência sobre leitura excessiva).
        legacy = not q.structural_fingerprint
        if q.table_is_partitioned and (
            (q.parse_succeeded and q.missing_partition_filters)
            or (legacy and not q.has_partition_filter)
        ):
            out.append(_no_partition(account, q, config, scan_id))
        # Regra 4 — leitura excessiva (SELECT * / scan alto).
        if q.data_scanned_bytes >= th.athena_high_scan_bytes or (
            q.parse_succeeded and q.selects_star
            and bool(set(q.storage_formats) & {"PARQUET", "ORC"})
        ):
            out.append(_excessive_scan(account, q, config, scan_id))
        # Regra 6 — query recorrente sem result reuse (query "saudável", mas repetida).
        if (
            (q.reuse_eligible_runs > 0 and q.reuse_avoidable_billed_bytes > 0)
            or (legacy and not q.result_reuse_enabled and q.executions_per_month >= 8)
        ):
            out.append(_result_reuse(account, q, config, scan_id))
    return out


def _select_star_wide(
    account: Account, q: AthenaQuery, config: Config, scan_id: str
) -> Opportunity:
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-SELECT-STAR-WIDE",
        rule_version="1.0.0",
        difficulty=2,
        estimation=athena_est.projection_saving(q, config, "select_star"),
        finding="SELECT * em tabela wide",
        why=(
            f"O padrão usa SELECT * em tabela com até {q.max_table_columns} colunas; "
            f"{q.observed_runs} execuções por {q.actor_count} atores."
        ),
        recommended_action="Substituir SELECT * pelas colunas realmente utilizadas",
        how_to_apply="Revisar consumidores do resultado e explicitar a projeção de colunas.",
        how_to_validate="Comparar bytes, duração e resultado funcional na mesma janela.",
        evidence=q.evidence + [
            f"tabelas wide: {', '.join(q.wide_tables)}",
            f"{q.max_table_columns} colunas no maior schema",
        ],
        risks=["remover coluna ainda consumida pode quebrar integrações"],
        doc_links=[_DOC_QUERY_OPT],
        data_sources=["Athena GetQueryExecution", "Glue GetTable"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=q.parse_succeeded,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
    )


def _full_table_scan(
    account: Account, q: AthenaQuery, config: Config, scan_id: str
) -> Opportunity:
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-FULL-TABLE-SCAN",
        rule_version="1.0.0",
        difficulty=2,
        estimation=athena_est.projection_saving(q, config, "full_scan"),
        finding="Full table scan recorrente ou relevante",
        why=(
            f"O AST não comprova restrição do conjunto lido e o padrão varre "
            f"{q.data_scanned_bytes / _GB:.1f} GB por execução."
        ),
        recommended_action="Restringir o conjunto lido antes de projetar ou agregar dados",
        how_to_apply="Adicionar predicados seletivos e, quando aplicável, filtros de partição.",
        how_to_validate="Comparar DataScannedInBytes e cardinalidade do resultado.",
        evidence=q.evidence + ["full scan confirmado por AST e volume escaneado"],
        risks=["predicado incorreto pode excluir dados necessários"],
        doc_links=[_DOC_PERFORMANCE],
        data_sources=["Athena GetQueryExecution", "sqlglot AST"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=q.parse_succeeded,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
    )


def _table_not_partitioned(
    account: Account, q: AthenaQuery, config: Config, scan_id: str
) -> Opportunity:
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-TABLE-NOT-PARTITIONED",
        rule_version="1.0.0",
        difficulty=3,
        estimation=athena_est.partition_pruning_saving(
            q, config, "table_not_partitioned"
        ),
        finding="Tabela relevante sem particionamento",
        why=(
            f"Tabela sem partition keys soma {q.total_table_bytes / _GB:.1f} GB "
            f"e participa de padrão {'recorrente' if q.recurring else 'de alto scan'}."
        ),
        recommended_action="Projetar uma estratégia de particionamento alinhada aos filtros reais",
        how_to_apply="Escolher chave de baixa/moderada cardinalidade e criar uma versão particionada controlada.",
        how_to_validate="Comparar bytes e resultados antes de migrar consumidores.",
        evidence=q.evidence + [
            "sem partition keys: " + ", ".join(q.unpartitioned_tables)
        ],
        risks=["particionamento excessivo cria muitos arquivos e piora planejamento"],
        doc_links=[_DOC_PARTITION],
        data_sources=["Glue GetTable", "S3 ListObjectsV2", "Athena history"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=q.total_table_bytes > 0,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
    )


def _uncompressed_row_format(
    account: Account, q: AthenaQuery, config: Config, scan_id: str
) -> Opportunity:
    return _non_financial(
        account,
        q,
        config,
        scan_id,
        rule_id="ATHENA-UNCOMPRESSED-ROW-FORMAT",
        finding="CSV/JSON sem compressão comprovada",
        why="Todos os objetos inspecionados usam extensão sem codec reconhecido.",
        action="Avaliar compressão GZIP/ZSTD ou conversão para Parquet/ORC",
        evidence=["tabelas: " + ", ".join(q.row_format_uncompressed)],
        docs=[_DOC_COMPRESSION, _DOC_COLUMNAR],
    )


def _uncompressed_columnar(
    account: Account, q: AthenaQuery, config: Config, scan_id: str
) -> Opportunity:
    return _non_financial(
        account,
        q,
        config,
        scan_id,
        rule_id="ATHENA-COLUMNAR-COMPRESSION",
        finding="Parquet/ORC explicitamente sem compressão",
        why="As propriedades da tabela declaram NONE/UNCOMPRESSED.",
        action="Testar Snappy ou ZSTD em uma nova versão dos dados",
        evidence=["tabelas: " + ", ".join(q.columnar_uncompressed)],
        docs=[_DOC_COMPRESSION, _DOC_COLUMNAR],
    )


def _partition_projection(
    account: Account, q: AthenaQuery, config: Config, scan_id: str
) -> Opportunity:
    return _non_financial(
        account,
        q,
        config,
        scan_id,
        rule_id="ATHENA-PARTITION-PROJECTION",
        finding="Tabela altamente particionada sem partition projection",
        why=(
            f"Há pelo menos {q.partition_count} partições registradas e projection não está ativo; "
            f"p95 de planejamento {q.p95_planning_ms} ms."
        ),
        action="Avaliar partition projection conforme o padrão real das partições",
        evidence=["tabelas: " + ", ".join(q.partition_projection_candidates)],
        docs=[_DOC_PROJECTION],
    )


def _non_financial(
    account: Account,
    q: AthenaQuery,
    config: Config,
    scan_id: str,
    *,
    rule_id: str,
    finding: str,
    why: str,
    action: str,
    evidence: list[str],
    docs: list[str],
) -> Opportunity:
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id=rule_id,
        rule_version="1.0.0",
        difficulty=3,
        estimation=athena_est.modeled_saving(
            q,
            config,
            {
                "ATHENA-UNCOMPRESSED-ROW-FORMAT": "row_format_compression",
                "ATHENA-COLUMNAR-COMPRESSION": "columnar_compression",
                "ATHENA-PARTITION-PROJECTION": "partition_projection",
            }[rule_id],
        ),
        finding=finding,
        why=why,
        recommended_action=action,
        how_to_apply="Criar uma versão controlada; não alterar dados existentes automaticamente.",
        how_to_validate="Comparar bytes, duração, custo e equivalência do resultado.",
        evidence=q.evidence + evidence,
        risks=["mudança de layout exige validação dos consumidores"],
        doc_links=docs,
        data_sources=["Glue GetTable", "S3 ListObjectsV2", "Athena history"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=True,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
        is_strategic=False,
    )


def _result_reuse(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    est = athena_est.result_reuse_saving(q, config)
    return build(
        account=account.account_id, asset_type="athena_query", asset_name=q.query_id,
        rule_id="ATHENA-RESULT-REUSE", rule_version="1.0.0", difficulty=1, estimation=est,
        finding="Repetições exatas elegíveis sem query result reuse",
        why=(
            f"{q.reuse_eligible_runs} repetições exatas em até 60 minutos processaram "
            f"{q.reuse_avoidable_billed_bytes / _GB:.2f} GB faturáveis novamente."
            if q.structural_fingerprint
            else f"Query roda {q.executions_per_month}×/mês sem reutilização confirmada."
        ),
        recommended_action="Configurar result reuse no cliente que submete a query",
        how_to_apply="Definir ResultReuseByAgeConfiguration com janela compatível com a atualização da origem.",
        how_to_validate="Comparar bytes faturáveis, custo líquido alocado e ReusedPreviousResult.",
        evidence=q.evidence + [
            f"custo evitável reconciliado: {q.reuse_avoidable_cost:.6f} {q.currency}"
            if q.reuse_avoidable_cost is not None
            else "custo evitável indisponível sem reconciliação completa"
        ],
        risks=["resultado defasado se a janela de reuso for longa demais"],
        doc_links=[_DOC_REUSE], data_sources=["Athena workgroup history"],
        observed_runs=q.observed_runs, coverage_days=q.coverage_days,
        has_optional_metrics=q.observed_runs >= config.thresholds.min_runs,
        owner_tag=q.owner_tag, config=config, scan_id=scan_id,
    )


def _no_partition(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    est = athena_est.partition_pruning_saving(q, config)
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-NO-PARTITION-FILTER",
        rule_version="1.0.0",
        difficulty=2,
        estimation=est,
        finding="Query sem filtro de partição varre a tabela inteira",
        why=(
            f"Query recorrente varre {q.data_scanned_bytes / (1024**4):.2f} TB/execução "
            f"sem predicado de partição; {q.executions_per_month} execuções/mês."
        ),
        recommended_action="Adicionar filtro de partição (WHERE dt=…)",
        how_to_apply="Adicionar filtro de partição e projetar só as colunas usadas.",
        how_to_validate="Comparar DataScannedInBytes antes × depois na mesma janela.",
        evidence=q.evidence + [
            f"DataScannedInBytes {q.data_scanned_bytes / (1024**3):.0f} GB por execução",
            "sem filtro de partição: "
            + (", ".join(q.missing_partition_filters) or "evidência do inventário legado"),
            f"{q.executions_per_month} execuções/mês no histórico",
        ],
        risks=["resultado incompleto se a partição errada for filtrada"],
        doc_links=[_DOC_PARTITION],
        data_sources=["Athena get_query_execution", "workgroup history"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=True,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
    )


def _excessive_scan(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    profile = (
        "select_star"
        if q.selects_star and bool(set(q.storage_formats) & {"PARQUET", "ORC"})
        else "full_scan"
    )
    est = athena_est.projection_saving(q, config, profile)
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-EXCESSIVE-SCAN",
        rule_version="1.0.0",
        difficulty=2,
        estimation=est,
        finding="Query com leitura excessiva (SELECT * / sem result reuse)",
        why=(
            f"Varre {q.data_scanned_bytes / _GB:.0f} GB/execução; "
            f"{'SELECT * ' if q.selects_star else ''}"
            f"{'sem result reuse no workgroup' if not q.result_reuse_enabled else ''}."
        ),
        recommended_action="Projetar colunas e habilitar result reuse",
        how_to_apply="Selecionar só as colunas necessárias e ativar query result reuse no workgroup.",
        how_to_validate="Comparar bytes escaneados e cache hits no workgroup.",
        evidence=q.evidence + [
            f"{q.data_scanned_bytes / _GB:.0f} GB escaneados por execução",
            "SELECT * detectado" if q.selects_star else "leitura ampla de colunas",
            "workgroup sem result reuse" if not q.result_reuse_enabled else "result reuse ativo",
        ],
        risks=["quebra de compatibilidade se colunas removidas forem usadas"],
        doc_links=[_DOC_REUSE],
        data_sources=["Athena workgroup history"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=q.observed_runs >= config.thresholds.min_runs,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
    )


def _failures(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-RECURRENT-FAILURES",
        rule_version="1.0.0",
        difficulty=2,
        estimation=athena_est.modeled_saving(
            q, config, "recurrent_failures"
        ),
        finding="Falhas ou cancelamentos recorrentes no Athena",
        why=f"{q.failed_runs} falhas e {q.cancelled_runs} cancelamentos no período.",
        recommended_action="Investigar causa das falhas e cancelamentos recorrentes",
        how_to_apply="Revisar códigos de erro, limites e dependências do padrão sanitizado.",
        how_to_validate="Acompanhar taxa de sucesso e duração na próxima janela mensal.",
        evidence=q.evidence + [f"{q.failed_runs} falhas", f"{q.cancelled_runs} cancelamentos"],
        risks=["não alterar a query sem validar a semântica esperada"],
        doc_links=[_DOC_PERFORMANCE],
        data_sources=["Athena GetQueryExecution"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=q.parse_succeeded,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
        is_strategic=True,
        blocked=not q.parse_succeeded,
    )


def _small_files(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    return build(
        account=account.account_id,
        asset_type="athena_query",
        asset_name=q.query_id,
        rule_id="ATHENA-SMALL-FILES",
        rule_version="1.0.0",
        difficulty=3,
        estimation=athena_est.modeled_saving(q, config, "small_files"),
        finding="Muitos arquivos pequenos nas tabelas consultadas",
        why=(
            f"Evidência S3 confirma {q.small_file_count} objetos com média de "
            f"{q.average_file_bytes / 1024**2:.1f} MiB."
        ),
        recommended_action="Planejar compactação dos arquivos da tabela",
        how_to_apply="Compactar em uma nova versão controlada e validar consumidores antes da troca.",
        how_to_validate="Comparar tempo, bytes e custo por execução na janela seguinte.",
        evidence=q.evidence + [
            f"{q.small_file_count} objetos S3",
            f"tamanho médio {q.average_file_bytes / 1024**2:.1f} MiB",
        ],
        risks=["compactação exige escrita de dados e aprovação humana separada"],
        doc_links=[_DOC_PERFORMANCE],
        data_sources=["S3 ListObjectsV2", "Glue GetTable"],
        observed_runs=q.observed_runs,
        coverage_days=q.coverage_days,
        has_optional_metrics=True,
        owner_tag=q.owner_tag,
        config=config,
        scan_id=scan_id,
        is_strategic=True,
    )
