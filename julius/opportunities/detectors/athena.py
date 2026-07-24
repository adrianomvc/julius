"""Detectores de Athena: (4) leitura excessiva, (5) sem filtro de partição."""

from __future__ import annotations

from julius.config import Config
from julius.estimation import athena as athena_est
from julius.inventory.model import Account, AthenaQuery
from julius.opportunities.base import Estimation, Opportunity
from julius.opportunities.detectors._build import build

_DOC_PARTITION = "https://docs.aws.amazon.com/athena/latest/ug/partitions.html"
_DOC_REUSE = "https://docs.aws.amazon.com/athena/latest/ug/reusing-query-results.html"
_DOC_PERFORMANCE = "https://docs.aws.amazon.com/athena/latest/ug/performance-tuning.html"

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
        # Regra 5 — sem filtro de partição (tem precedência sobre leitura excessiva).
        legacy = not q.structural_fingerprint
        if q.table_is_partitioned and (
            (q.parse_succeeded and q.missing_partition_filters)
            or (legacy and not q.has_partition_filter)
        ):
            out.append(_no_partition(account, q, config, scan_id))
            continue
        # Regra 4 — leitura excessiva (SELECT * / scan alto).
        if q.data_scanned_bytes >= th.athena_high_scan_bytes or (
            q.parse_succeeded and q.selects_star
            and bool(set(q.storage_formats) & {"PARQUET", "ORC"})
        ):
            out.append(_excessive_scan(account, q, config, scan_id))
            continue
        # Regra 6 — query recorrente sem result reuse (query "saudável", mas repetida).
        if (
            (q.recurring and q.exact_fingerprint and q.reused_runs == 0)
            or (legacy and not q.result_reuse_enabled and q.executions_per_month >= 8)
        ):
            out.append(_result_reuse(account, q, config, scan_id))
    return out


def _result_reuse(account: Account, q: AthenaQuery, config: Config, scan_id: str) -> Opportunity:
    est = athena_est.result_reuse_saving(q, config)
    return build(
        account=account.account_id, asset_type="athena_query", asset_name=q.query_id,
        rule_id="ATHENA-RESULT-REUSE", rule_version="1.0.0", difficulty=1, estimation=est,
        finding="Query recorrente sem query result reuse",
        why=f"Query idêntica roda {q.executions_per_month}×/mês sem reutilização confirmada.",
        recommended_action="Habilitar query result reuse no workgroup",
        how_to_apply="Ativar result reuse no workgroup (ou por query) com janela de reuso adequada.",
        how_to_validate="Comparar bytes escaneados e cache hits no workgroup.",
        evidence=q.evidence + ["nenhuma reutilização confirmada"],
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
    est = athena_est.projection_saving(q, config)
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
        estimation=Estimation(
            method="athena_reliability_investigation_v1",
            baseline_cost=0,
            projected_cost=0,
            estimated_saving=0,
            assumptions=["falhas não recebem economia estimada"],
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
        estimation=Estimation(
            method="athena_small_files_evidence_v1",
            baseline_cost=0,
            projected_cost=0,
            estimated_saving=0,
            assumptions=["sem economia financeira sem benchmark posterior"],
        ),
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
