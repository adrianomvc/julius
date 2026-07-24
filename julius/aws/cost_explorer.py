"""Coletor do Cost Explorer: custo mensal por serviço (reconciliação)."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

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
    "AWS Glue": "Jobs, sessões, crawlers, catálogo e recursos associados",
    "Amazon Athena": "queries + workgroups",
    "Amazon S3": "armazenamento + requests",
    "Step Functions": "Standard / Express",
}


def collect_services(
    ce_client,
    *,
    months: int = 1,
    today: date | None = None,
    include_forecast: bool = False,
) -> list[ServiceCost]:
    """GetCostAndUsage agrupado por serviço → cobrança MTD em USD.

    Serviços fora do escopo do Julius são somados em "Outros".
    """
    today = today or date.today()
    # Janela: início do mês corrente até hoje (mês parcial) — simples e suficiente
    # para reconciliação; ajuste para meses fechados conforme a necessidade.
    month_start = today.replace(day=1)
    # O fim do Cost Explorer é exclusivo. No primeiro dia, avançar um dia evita
    # a janela inválida Start == End; nesse caso a cobrança inclui o dia atual.
    billing_end = today if today > month_start else today + timedelta(days=1)
    period = {"Start": month_start.isoformat(), "End": billing_end.isoformat()}

    resp = ce_client.get_cost_and_usage(
        TimePeriod=period,
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    totals: dict[str, float] = {}
    currencies: dict[str, str] = {}
    estimated = False
    for result in resp.get("ResultsByTime", []):
        estimated = estimated or bool(result.get("Estimated", True))
        for group in result.get("Groups", []):
            name = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            currency = str(group["Metrics"]["UnblendedCost"].get("Unit") or "USD")
            label = _SERVICE_LABEL.get(name, "Outros")
            totals[label] = totals.get(label, 0.0) + amount
            currencies[label] = currency

    forecasts = (
        _forecast_by_service(ce_client, billing_end, totals)
        if include_forecast
        else {}
    )
    services: list[ServiceCost] = []
    for label in ("AWS Glue", "Amazon Athena", "Amazon S3", "Step Functions", "Amazon SageMaker"):
        if label in totals:
            services.append(
                ServiceCost(
                    name=label,
                    monthly_cost=round(totals.pop(label), 2),
                    subtitle=_SUBTITLE.get(label, ""),
                    currency=currencies.get(label, "USD"),
                    period_start=period["Start"],
                    data_through=(billing_end - timedelta(days=1)).isoformat(),
                    estimated=estimated,
                    period_kind="month_to_date",
                    cost_basis="cost_explorer_unblended",
                    forecast_cost_eom=forecasts.get(label),
                )
            )
    if totals:
        services.append(
            ServiceCost(
                name="Outros",
                monthly_cost=round(sum(totals.values()), 2),
                subtitle="demais serviços",
                currency=next(iter(currencies.values()), "USD"),
                period_start=period["Start"],
                data_through=(billing_end - timedelta(days=1)).isoformat(),
                estimated=estimated,
                period_kind="month_to_date",
                cost_basis="cost_explorer_unblended",
            )
        )
    return services


def _forecast_by_service(
    ce_client, forecast_start: date, current_totals: dict[str, float]
) -> dict[str, float]:
    last_day = calendar.monthrange(forecast_start.year, forecast_start.month)[1]
    end = forecast_start.replace(day=last_day) + timedelta(days=1)
    if forecast_start >= end:
        return {}
    forecasts: dict[str, float] = {}
    for aws_name, label in _SERVICE_LABEL.items():
        if label not in current_totals:
            continue
        try:
            response = ce_client.get_cost_forecast(
                TimePeriod={"Start": forecast_start.isoformat(), "End": end.isoformat()},
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY",
                Filter={
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": [aws_name],
                    }
                },
            )
            remaining = float(
                (response.get("Total") or {}).get("Amount", 0) or 0
            )
        except Exception:
            continue
        forecasts[label] = round(current_totals[label] + remaining, 2)
    return forecasts
