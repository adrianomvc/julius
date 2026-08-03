"""Telemetria sanitizada da coleta AWS read-only."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import TypeVar

from julius.collection.currency import UnsupportedCurrencyError
from julius.collection.iam import gap_from_exception
from julius.collection.models import CollectionHealth

T = TypeVar("T")


class RequiredCollectionError(RuntimeError):
    """Uma fonte essencial falhou; não é seguro produzir o scan."""

    def __init__(self, source: str, category: str):
        self.source = source
        self.category = category
        self.collection_entries: list[CollectionHealth] = []
        super().__init__(f"coleta essencial falhou: {source} ({category})")


class CollectionRecorder:
    def __init__(self) -> None:
        self.entries: list[CollectionHealth] = []

    def capture(
        self,
        source: str,
        fn: Callable[[], T],
        default: T,
        *,
        required: bool = False,
        count: Callable[[T], int] | None = None,
        expected: int | Callable[[T], int | None] | None = None,
        data_through: Callable[[T], str] | None = None,
        impact: str = "",
        next_action: str = "",
    ) -> T:
        started = datetime.now(timezone.utc)
        timer = perf_counter()
        try:
            result = fn()
        except Exception as exc:
            category = error_category(exc)
            entry = _entry(
                source=source,
                status="error" if required else "unavailable",
                required=required,
                started=started,
                timer=timer,
                error_category=category,
                impact=impact,
                next_action=next_action,
            )
            iam_gap = gap_from_exception(exc) if category == "permission_denied" else None
            if iam_gap is not None:
                entry.iam_gaps = [iam_gap]
                entry.next_action = f"validar permissão read-only: {iam_gap.iam_action}"
            self.entries.append(entry)
            if required:
                raise RequiredCollectionError(source, category) from exc
            return default

        collected = max(0, int(count(result))) if count else 0
        expected_value = expected(result) if callable(expected) else expected
        expected_value = (
            max(0, int(expected_value)) if expected_value is not None else None
        )
        coverage = (
            min(1.0, collected / expected_value)
            if expected_value and expected_value > 0
            else (1.0 if expected_value == 0 else None)
        )
        status = "ok"
        category = ""
        if expected_value and collected == 0:
            status = "unavailable"
            category = "no_data"
        elif expected_value and collected < expected_value:
            status = "partial"
            category = "partial_data"
        self.entries.append(
            _entry(
                source=source,
                status=status,
                required=required,
                started=started,
                timer=timer,
                collected=collected,
                expected=expected_value,
                coverage=coverage,
                data_through=data_through(result) if data_through else "",
                error_category=category,
                impact=impact if status != "ok" else "",
                next_action=next_action if status != "ok" else "",
            )
        )
        return result

    def unavailable(
        self,
        source: str,
        *,
        category: str,
        impact: str,
        next_action: str,
        affects_status: bool = True,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.entries.append(
            CollectionHealth(
                source=source,
                status="unavailable",
                affects_status=affects_status,
                started_at=now.isoformat(),
                completed_at=now.isoformat(),
                error_category=category,
                impact=impact,
                next_action=next_action,
            )
        )

    def not_applicable(self, source: str, *, reason: str) -> None:
        """Registra fonte fora do perfil sem tratá-la como falha de cobertura."""
        now = datetime.now(timezone.utc).isoformat()
        self.entries.append(
            CollectionHealth(
                source=source,
                status="not_applicable",
                affects_status=False,
                started_at=now,
                completed_at=now,
                error_category="out_of_scope",
                impact=reason,
            )
        )


def error_category(exc: Exception) -> str:
    """Mapeia exceções para categorias estáveis sem persistir mensagens."""
    if isinstance(exc, UnsupportedCurrencyError):
        return "unsupported_currency"
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code") or "")
    normalized = code.lower()
    if any(token in normalized for token in ("accessdenied", "unauthorized", "forbidden")):
        return "permission_denied"
    if "throttl" in normalized or "limitexceeded" in normalized:
        return "throttled"
    if any(token in normalized for token in ("nosuch", "notfound", "resourcenotfound")):
        return "not_found"
    if "expired" in normalized or "invalidtoken" in normalized:
        return "credentials_expired"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_response"
    return "service_error"


def _entry(
    *,
    source: str,
    status: str,
    required: bool,
    started: datetime,
    timer: float,
    collected: int = 0,
    expected: int | None = None,
    coverage: float | None = None,
    data_through: str = "",
    error_category: str = "",
    impact: str = "",
    next_action: str = "",
) -> CollectionHealth:
    completed = datetime.now(timezone.utc)
    return CollectionHealth(
        source=source,
        status=status,
        required=required,
        started_at=started.isoformat(),
        completed_at=completed.isoformat(),
        duration_ms=max(0, round((perf_counter() - timer) * 1000)),
        collected=collected,
        expected=expected,
        coverage=round(coverage, 4) if coverage is not None else None,
        data_through=data_through,
        error_category=error_category,
        impact=impact,
        next_action=next_action,
    )
