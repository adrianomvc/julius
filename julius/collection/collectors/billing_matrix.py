"""Uma leitura Cost Explorer compartilhada pelos rateios de serviço."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from julius.collection.window import AnalysisWindow

SERVICES = (
    "AWS Glue",
    "Amazon Simple Storage Service",
    "Amazon SageMaker",
    "Amazon Redshift",
)
_METRIC_ATTEMPTS = (
    ("NetUnblendedCost", "UnblendedCost", "UsageQuantity"),
    ("UnblendedCost", "UsageQuantity"),
)


@dataclass(frozen=True)
class BillingMatrix:
    """Respostas por SERVICE + USAGE_TYPE, reutilizáveis sem nova chamada."""

    responses: tuple[dict[str, Any], ...]
    metrics: frozenset[str]

    def for_service(self, service: str, metric: str) -> list[dict] | None:
        if metric not in self.metrics:
            return None
        out: list[dict] = []
        for response in self.responses:
            periods = []
            for period in response.get("ResultsByTime", []) or []:
                groups = []
                for group in period.get("Groups", []) or []:
                    keys = list(group.get("Keys", []) or [])
                    if not keys or keys[0] != service:
                        continue
                    metrics = group.get("Metrics", {}) or {}
                    groups.append(
                        {
                            **group,
                            "Keys": keys[1:],
                            "Metrics": {
                                key: value
                                for key, value in metrics.items()
                                if key in {metric, "UsageQuantity"}
                            },
                        }
                    )
                periods.append({**period, "Groups": groups})
            out.append({**response, "ResultsByTime": periods, "NextPageToken": None})
        return out


def collect(ce_client, *, window: AnalysisWindow) -> BillingMatrix:
    """Busca quatro serviços juntos; fallback remove Net quando indisponível."""
    last_error: Exception | None = None
    for metrics in _METRIC_ATTEMPTS:
        try:
            responses = _pages(ce_client, window, metrics)
        except Exception as exc:  # noqa: BLE001 - fallback de métrica da AWS
            last_error = exc
            continue
        return BillingMatrix(tuple(responses), frozenset(metrics))
    assert last_error is not None
    raise last_error


def _pages(ce_client, window: AnalysisWindow, metrics: tuple[str, ...]) -> list[dict]:
    request: dict[str, Any] = {
        "TimePeriod": {
            "Start": window.start_date.isoformat(),
            "End": window.end_date.isoformat(),
        },
        "Granularity": "MONTHLY",
        "Metrics": list(metrics),
        "Filter": {"Dimensions": {"Key": "SERVICE", "Values": list(SERVICES)}},
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        ],
    }
    responses = []
    while True:
        response = ce_client.get_cost_and_usage(**request)
        responses.append(response)
        token = response.get("NextPageToken")
        if not token:
            return responses
        request["NextPageToken"] = token
