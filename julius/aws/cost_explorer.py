"""Coletor do Cost Explorer: custo mensal por serviço (reconciliação)."""

from __future__ import annotations

from datetime import date

from julius.inventory.model import ServiceCost

# Serviços que o Julius monitora (nomes do Cost Explorer → rótulo do relatório).
_SERVICE_LABEL = {
    "AWS Glue": "AWS Glue",
    "Amazon Athena": "Amazon Athena",
    "Amazon Simple Storage Service": "Amazon S3",
    "AWS Step Functions": "Step Functions",
    "Amazon SageMaker": "Amazon SageMaker",
}
_SUBTITLE = {
    "AWS Glue": "Jobs + Interactive Sessions",
    "Amazon Athena": "queries + workgroups",
    "Amazon S3": "armazenamento + requests",
    "Step Functions": "Standard / Express",
}


def collect_services(ce_client, *, months: int = 1, today: date | None = None) -> list[ServiceCost]:
    """GetCostAndUsage MONTHLY agrupado por SERVICE → custo mensal médio por serviço.

    Serviços fora do escopo do Julius são somados em "Outros".
    """
    today = today or date.today()
    # Janela: início do mês corrente até hoje (mês parcial) — simples e suficiente
    # para reconciliação; ajuste para meses fechados conforme a necessidade.
    period = {"Start": today.replace(day=1).isoformat(), "End": today.isoformat()}

    resp = ce_client.get_cost_and_usage(
        TimePeriod=period,
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    totals: dict[str, float] = {}
    currency = "BRL"
    for result in resp.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            name = group["Keys"][0]
            metric = group["Metrics"]["UnblendedCost"]
            amount = float(metric["Amount"])
            currency = metric.get("Unit") or currency
            label = _SERVICE_LABEL.get(name, "Outros")
            totals[label] = totals.get(label, 0.0) + amount

    services: list[ServiceCost] = []
    for label in ("AWS Glue", "Amazon Athena", "Amazon S3", "Step Functions", "Amazon SageMaker"):
        if label in totals:
            services.append(
                ServiceCost(
                    name=label,
                    monthly_cost=round(totals.pop(label), 2),
                    subtitle=_SUBTITLE.get(label, ""),
                    currency=currency,
                )
            )
    if totals:
        services.append(
            ServiceCost(
                name="Outros",
                monthly_cost=round(sum(totals.values()), 2),
                subtitle="demais serviços",
                currency=currency,
            )
        )
    return services
