"""Detectores conservadores para AWS Glue Crawlers."""

from __future__ import annotations

from julius.config import Config
from julius.inventory.model import Account, GlueCrawler
from julius.opportunities.base import Estimation, Opportunity
from julius.opportunities.detectors._build import build

_DOC = "https://docs.aws.amazon.com/glue/latest/dg/aws-glue-api-crawler-crawling.html"
_DOC_INCREMENTAL = "https://docs.aws.amazon.com/glue/latest/dg/incremental-crawls.html"


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    out: list[Opportunity] = []
    for crawler in account.glue_crawlers:
        baseline = crawler.dpu_hours_window * config.pricing.glue_dpu_hour
        if crawler.failures_in_window > 0 and crawler.runs_in_window > 0:
            ratio = crawler.failures_in_window / crawler.runs_in_window
            out.append(
                _opportunity(
                    account,
                    crawler,
                    config,
                    scan_id,
                    "GLUE-CRAWLER-FAILING",
                    "Crawler falha no mês atual",
                    "Corrigir a causa das falhas antes da próxima execução",
                    baseline,
                    baseline * ratio,
                    [
                        f"{crawler.failures_in_window}/{crawler.runs_in_window} crawls falharam",
                        f"{crawler.dpu_hours_window:.2f} DPU-h no mês",
                    ],
                )
            )
        changes = (
            crawler.tables_created
            + crawler.tables_updated
            + crawler.tables_deleted
        )
        if crawler.runs_in_window >= 4 and changes == 0 and baseline > 0:
            out.append(
                _opportunity(
                    account,
                    crawler,
                    config,
                    scan_id,
                    "GLUE-CRAWLER-NO-CHANGES",
                    "Crawler recorrente sem alterações no catálogo",
                    "Revisar a frequência do crawler",
                    baseline,
                    baseline * 0.5,
                    [
                        f"{crawler.runs_in_window} crawls no mês",
                        "0 tabelas criadas/atualizadas/excluídas",
                    ],
                )
            )
        if (
            crawler.runs_in_window >= 4
            and crawler.recrawl_behavior == "CRAWL_EVERYTHING"
        ):
            out.append(
                _opportunity(
                    account,
                    crawler,
                    config,
                    scan_id,
                    "GLUE-CRAWLER-FULL-RECRAWL",
                    "Crawler recorrente relê toda a fonte",
                    (
                        "Validar estabilidade do schema e avaliar crawl incremental "
                        "ou orientado por eventos"
                    ),
                    baseline,
                    0.0,
                    [
                        "RecrawlBehavior=CRAWL_EVERYTHING",
                        f"{crawler.runs_in_window} crawls no mês",
                        (
                            f"{changes} alterações de catálogo observadas"
                        ),
                    ],
                    blocked=True,
                    doc_link=_DOC_INCREMENTAL,
                )
            )
        if (
            crawler.schedule_expression
            and crawler.schedule_state == "NOT_SCHEDULED"
        ):
            out.append(
                _opportunity(
                    account,
                    crawler,
                    config,
                    scan_id,
                    "GLUE-CRAWLER-SCHEDULE-DISABLED",
                    "Crawler possui cron, mas o schedule está desabilitado",
                    "Confirmar se o processo foi descontinuado ou reativar após aprovação",
                    0.0,
                    0.0,
                    [
                        f"schedule={crawler.schedule_expression}",
                        "schedule_state=NOT_SCHEDULED",
                    ],
                    blocked=True,
                )
            )
    return out


def _opportunity(
    account: Account,
    crawler: GlueCrawler,
    config: Config,
    scan_id: str,
    rule_id: str,
    finding: str,
    action: str,
    baseline: float,
    saving: float,
    evidence: list[str],
    *,
    blocked: bool = False,
    doc_link: str = _DOC,
) -> Opportunity:
    estimation = Estimation(
        method=rule_id.lower().replace("-", "_") + "_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(max(0.0, baseline - saving), 2),
        estimated_saving=round(min(baseline, saving), 2),
        assumptions=["somente consumo e alterações observados no mês atual"],
        pricing_region=config.pricing.region,
        estimation_version=config.pricing.version,
    )
    return build(
        account=account.account_id,
        asset_type="glue_crawler",
        asset_name=crawler.name,
        rule_id=rule_id,
        rule_version="1.0.0",
        difficulty=2,
        estimation=estimation,
        finding=finding,
        why=finding,
        recommended_action=action,
        how_to_apply="Revisar configuração e histórico; qualquer alteração exige aprovação humana.",
        how_to_validate="Comparar status, alterações do catálogo e DPU-h no mês seguinte.",
        evidence=evidence,
        risks=["mudança de frequência pode atrasar descoberta de schema"],
        doc_links=[doc_link],
        data_sources=["Glue GetCrawlers", "GetCrawlerMetrics", "ListCrawls"],
        observed_runs=crawler.runs_in_window,
        coverage_days=crawler.window_days,
        has_optional_metrics=crawler.dpu_hours_window > 0,
        owner_tag=crawler.owner_tag,
        config=config,
        scan_id=scan_id,
        blocked=blocked,
    )
