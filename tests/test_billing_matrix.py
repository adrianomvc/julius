"""Uma consulta CE compartilhada substitui quatro leituras equivalentes."""

from __future__ import annotations

from datetime import datetime, timezone

from julius.collection.collectors import (
    billing_matrix,
    redshift_cost,
    s3_cost,
    sagemaker_cost,
)
from julius.collection.collectors.glue import cost as glue_cost
from julius.collection.window import AnalysisWindow

WINDOW = AnalysisWindow(
    start=datetime(2026, 7, 1, tzinfo=timezone.utc),
    end=datetime(2026, 7, 31, tzinfo=timezone.utc),
    days=30,
)


def _group(service: str, usage: str, amount: str) -> dict:
    return {
        "Keys": [service, usage],
        "Metrics": {
            "NetUnblendedCost": {"Amount": amount, "Unit": "USD"},
            "UnblendedCost": {"Amount": amount, "Unit": "USD"},
            "UsageQuantity": {"Amount": "10", "Unit": "GB-Mo"},
        },
    }


class _CE:
    def __init__(self):
        self.calls = 0

    def get_cost_and_usage(self, **request):
        self.calls += 1
        assert len(request["GroupBy"]) == 2
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        _group("AWS Glue", "GlueETL", "1"),
                        _group("Amazon Simple Storage Service", "TimedStorage", "2"),
                        _group("Amazon SageMaker", "Training", "3"),
                        _group("Amazon Redshift", "Node", "4"),
                    ]
                }
            ]
        }


class _NoCECalls:
    def get_cost_and_usage(self, **_request):
        raise AssertionError("o rateio deveria reutilizar a matriz")


def test_four_cost_collectors_reuse_one_cost_explorer_call():
    ce = _CE()
    matrix = billing_matrix.collect(ce, window=WINDOW)

    glue = glue_cost.collect_glue_costs(
        _NoCECalls(),
        window=WINDOW,
        markers=(("glueetl", "etl_job"),),
        matrix=matrix,
    )
    s3 = s3_cost.collect_s3_costs(
        _NoCECalls(),
        window=WINDOW,
        markers=(("timedstorage", "storage"),),
        matrix=matrix,
    )
    sagemaker = sagemaker_cost.collect_sagemaker_costs(
        _NoCECalls(),
        window=WINDOW,
        markers=(("training", "training"),),
        matrix=matrix,
    )
    redshift = redshift_cost.collect_redshift_costs(
        _NoCECalls(),
        window=WINDOW,
        markers=(("node", "compute"),),
        matrix=matrix,
    )

    assert ce.calls == 1
    assert glue.net_cost == 1
    assert s3.net_cost == 2
    assert sagemaker.net_cost == 3
    assert redshift.net_cost == 4


def test_matrix_falls_back_to_unblended_in_one_shared_retry():
    class WithoutNet(_CE):
        def get_cost_and_usage(self, **request):
            self.calls += 1
            if "NetUnblendedCost" in request["Metrics"]:
                raise RuntimeError("metric unavailable")
            response = super().get_cost_and_usage(**request)
            self.calls -= 1  # o super representa a mesma chamada, não outra
            return response

    ce = WithoutNet()
    matrix = billing_matrix.collect(ce, window=WINDOW)

    assert ce.calls == 2
    assert matrix.metrics == frozenset({"UnblendedCost", "UsageQuantity"})


def test_matrix_preserves_pagination_before_splitting_services():
    class Paginated:
        calls = 0

        def get_cost_and_usage(self, **request):
            self.calls += 1
            second = bool(request.get("NextPageToken"))
            return {
                "ResultsByTime": [
                    {
                        "Groups": [
                            _group("AWS Glue", "GlueETL", "2" if second else "1")
                        ]
                    }
                ],
                **({} if second else {"NextPageToken": "next"}),
            }

    ce = Paginated()
    matrix = billing_matrix.collect(ce, window=WINDOW)
    coverage = glue_cost.collect_glue_costs(
        _NoCECalls(),
        window=WINDOW,
        markers=(("glueetl", "etl_job"),),
        matrix=matrix,
    )

    assert ce.calls == 2
    assert coverage.net_cost == 3
