"""Economia de requests S3 sustentada por fatura e access logs agregados."""

from __future__ import annotations

import math
from collections.abc import Iterable

from julius.collection.models import Account, S3Prefix
from julius.config import Config
from julius.findings.opportunity import Estimation
from julius.knowledge.s3_cost import S3_REQUEST_BUCKETS

_READ_REQUEST_BUCKETS = frozenset({"requests_read"})


def request_summary(account: Account) -> dict[str, float | None]:
    """Totais compatíveis por tipo; quantidades ausentes nunca viram zero."""
    coverage = account.s3_cost_coverage
    if coverage is None:
        return {
            "read_cost": 0.0,
            "read_quantity": None,
            "write_cost": 0.0,
            "write_quantity": None,
            "other_cost": 0.0,
            "other_quantity": None,
            "total_cost": 0.0,
            "total_quantity": None,
        }
    return {
        "read_cost": coverage.cost_for({"requests_read"}),
        "read_quantity": coverage.quantity_for({"requests_read"}),
        "write_cost": coverage.cost_for({"requests_write"}),
        "write_quantity": coverage.quantity_for({"requests_write"}),
        "other_cost": coverage.cost_for({"requests_other"}),
        "other_quantity": coverage.quantity_for({"requests_other"}),
        "total_cost": coverage.cost_for(S3_REQUEST_BUCKETS),
        "total_quantity": coverage.quantity_for(S3_REQUEST_BUCKETS),
    }


def request_estimation(
    account: Account,
    prefixes: Iterable[S3Prefix],
    config: Config,
    *,
    method: str,
) -> Estimation:
    """Estima somente GETs observados; não usa contagem de objetos como fatura."""
    coverage = account.s3_cost_coverage
    candidates = list(prefixes)
    unit_cost = (
        coverage.unit_cost_for(_READ_REQUEST_BUCKETS)
        if coverage is not None
        else None
    )
    unit_source = "custo unitário por request = custo / UsageQuantity de Requests-Tier2"
    baseline_quality = "allocated"
    dependencies: tuple[str, ...] = ()
    if unit_cost is None:
        # A fatura é a melhor âncora, mas não é a única. A tabela versionada já
        # traz a tarifa de GET e a regra de classe de armazenamento já a consome
        # por `s3_request_cost`; aqui ela ficava sem uso, e a falta do rateio
        # reconciliado bloqueava um achado que já é estratégico — ou seja, que
        # nem entra no portfólio. Trocar `unavailable` por um número modelado dá
        # grandeza ao time sem mexer em nenhum total.
        unit_cost = config.pricing.s3_request_cost("get", 1)
        if unit_cost is not None:
            unit_source = (
                f"tarifa versionada de GET · {config.pricing.provenance}"
            )
            baseline_quality = "modeled"
            dependencies = ("s3",)
    measured = [
        prefix
        for prefix in candidates
        if prefix.get_requests_window is not None
        and prefix.access_quality == "best_effort"
        and prefix.listing_complete
        and (prefix.object_count or 0) > 0
        and (prefix.total_bytes or 0) > 0
    ]
    if unit_cost is None or not measured:
        missing = []
        if unit_cost is None:
            missing.append(
                "sem custo por GET: Requests-Tier2 não reconciliado no Cost "
                "Explorer e tarifa de GET ausente na tabela versionada"
            )
        if not measured:
            missing.append(
                "GETs por prefixo sem access logs utilizáveis e listagem completa"
            )
        return Estimation(
            method=method,
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                *missing,
                "o armazenamento permanece; compactação não reduz os bytes armazenados",
            ],
            saving_quality="unavailable",
            is_strategic=True,
        )

    current_requests = 0
    projected_requests = 0
    object_reduction = 0
    target_objects_total = 0
    for prefix in measured:
        objects = prefix.object_count or 0
        target_objects = max(
            1,
            math.ceil(
                (prefix.total_bytes or 0)
                / config.thresholds.s3_compaction_target_bytes
            ),
        )
        requests = prefix.get_requests_window or 0
        current_requests += requests
        target_objects_total += target_objects
        projected_requests += math.ceil(
            requests * min(1.0, target_objects / objects)
        )
        object_reduction += max(0, objects - target_objects)

    baseline = current_requests * unit_cost
    projected = projected_requests * unit_cost
    saving = max(0.0, baseline - projected)
    return Estimation(
        method=method,
        baseline_cost=round(baseline, 2),
        projected_cost=round(projected, 2),
        estimated_saving=round(saving, 2),
        estimated_saving_low=0.0,
        estimated_saving_high=round(saving, 2),
        assumptions=[
            f"{current_requests} GETs observados nos prefixos da oportunidade",
            f"~{target_objects_total} objetos após compactação",
            f"~{projected_requests} GETs projetados por leitura equivalente",
            f"{object_reduction} objetos evitáveis por leitura equivalente",
            unit_source,
            (
                "atribuição por Server Access Logs é best-effort; o valor é "
                "potencial e exige validação na janela seguinte"
            ),
            "o armazenamento permanece; compactação não reduz os bytes armazenados",
        ],
        baseline_quality=baseline_quality,
        saving_quality="modeled_evidence",
        is_strategic=True,
        pricing_dependencies=dependencies,
    )


def request_evidence(account: Account, prefixes: Iterable[S3Prefix]) -> list[str]:
    coverage = account.s3_cost_coverage
    prefixes = list(prefixes)
    quantity = (
        coverage.quantity_for(_READ_REQUEST_BUCKETS)
        if coverage is not None
        else None
    )
    cost = (
        coverage.cost_for(_READ_REQUEST_BUCKETS)
        if coverage is not None
        else 0.0
    )
    observed_gets = sum(prefix.get_requests_window or 0 for prefix in prefixes)
    lines = [
        (
            f"Requests-Tier2: {quantity:.0f} requests por {cost:.6f} USD"
            if quantity is not None
            else "Requests-Tier2 sem UsageQuantity compatível"
        ),
        f"GETs observados nos prefixos={observed_gets}",
    ]
    qualities = sorted(
        {
            prefix.access_quality
            for prefix in prefixes
            if prefix.access_quality != "unavailable"
        }
    )
    if qualities:
        lines.append("qualidade dos access logs=" + ", ".join(qualities))
    return lines
