"""Coletor do Cost Explorer: custo mensal por serviço (reconciliação)."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from julius.collection.currency import UnsupportedCurrencyError, usd_amount
from julius.collection.models import ServiceCost
from julius.collection.settings import MIN_DAYS_FOR_FORECAST
from julius.collection.window import BillingMonth

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
    billing: BillingMonth,
    include_forecast: bool = False,
) -> list[ServiceCost]:
    """GetCostAndUsage agrupado por serviço → cobrança MTD em USD.

    Serviços fora do escopo do Julius são somados em "Outros".
    """
    month_start = billing.month_start
    billing_end = billing.end_exclusive
    period = {"Start": month_start.isoformat(), "End": billing_end.isoformat()}

    resp = ce_client.get_cost_and_usage(
        TimePeriod=period,
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    totals: dict[str, float] = {}
    estimated = False
    for result in resp.get("ResultsByTime", []):
        estimated = estimated or bool(result.get("Estimated", True))
        for group in result.get("Groups", []):
            name = group["Keys"][0]
            metric = group["Metrics"]["UnblendedCost"]
            currency = str(metric.get("Unit") or "")
            # A AWS reporta custo em USD mesmo quando a fatura é em outra
            # moeda; outra unidade aqui é anomalia, não caso de conversão.
            amount = usd_amount(metric["Amount"], currency)
            if amount is None:
                raise UnsupportedCurrencyError(currency)
            label = _SERVICE_LABEL.get(name, "Outros")
            totals[label] = totals.get(label, 0.0) + amount

    # Com poucos dias fechados a projeção da AWS ainda oscila demais para ser
    # exibida como número; nesse caso o painel mostra apenas o MTD.
    forecasts = (
        _forecast_by_service(ce_client, billing_end, totals)
        if include_forecast and billing.observed_days >= MIN_DAYS_FOR_FORECAST
        else {}
    )
    services: list[ServiceCost] = []
    for label in ("AWS Glue", "Amazon Athena", "Amazon S3", "Step Functions", "Amazon SageMaker"):
        if label in totals:
            services.append(
                _service(
                    label,
                    totals.pop(label),
                    _SUBTITLE.get(label, ""),
                    period,
                    billing_end,
                    estimated,
                    forecasts.get(label),
                )
            )
    if totals:
        services.append(
            _service(
                "Outros",
                sum(totals.values()),
                "demais serviços",
                period,
                billing_end,
                estimated,
                None,
            )
        )
    return services


def _service(
    label: str,
    amount: float,
    subtitle: str,
    period: dict,
    billing_end: date,
    estimated: bool,
    forecast: float | None,
) -> ServiceCost:
    return ServiceCost(
        name=label,
        monthly_cost=round(amount, 2),
        subtitle=subtitle,
        currency="USD",
        period_start=period["Start"],
        data_through=(billing_end - timedelta(days=1)).isoformat(),
        estimated=estimated,
        period_kind="month_to_date",
        cost_basis="cost_explorer_unblended",
        forecast_cost_eom=forecast,
    )


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
            total = response.get("Total") or {}
            remaining = usd_amount(total.get("Amount", 0), str(total.get("Unit") or ""))
            if remaining is None:
                raise UnsupportedCurrencyError(str(total.get("Unit") or ""))
        except UnsupportedCurrencyError:
            raise
        except Exception:
            continue
        forecasts[label] = round(current_totals[label] + remaining, 2)
    return forecasts
