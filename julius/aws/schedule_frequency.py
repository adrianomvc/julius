"""Normalização conservadora de expressões rate/cron para execuções mensais."""

from __future__ import annotations

import re

_MONTH_DAYS = 30.0


def expected_runs_per_month(expression: str) -> float | None:
    text = expression.strip()
    rate = re.fullmatch(
        r"rate\(\s*(\d+)\s+(minute|minutes|hour|hours|day|days)\s*\)",
        text,
        re.IGNORECASE,
    )
    if rate:
        value = max(1, int(rate.group(1)))
        unit = rate.group(2).lower()
        if unit.startswith("minute"):
            return round(_MONTH_DAYS * 24 * 60 / value, 2)
        if unit.startswith("hour"):
            return round(_MONTH_DAYS * 24 / value, 2)
        return round(_MONTH_DAYS / value, 2)

    cron = re.fullmatch(r"cron\((.+)\)", text, re.IGNORECASE)
    if not cron:
        return None
    fields = cron.group(1).split()
    if len(fields) != 6:
        return None
    minute, hour, day_of_month, _month, day_of_week, _year = fields
    minute_count = _field_count(minute, 60)
    hour_count = _field_count(hour, 24)
    if minute_count is None or hour_count is None:
        return None
    daily_runs = minute_count * hour_count
    if day_of_month not in {"*", "?"}:
        days = _value_count(day_of_month)
        return round(daily_runs * days, 2) if days is not None else None
    if day_of_week not in {"*", "?"}:
        days = _value_count(day_of_week)
        return round(daily_runs * days * 52 / 12, 2) if days is not None else None
    return round(daily_runs * _MONTH_DAYS, 2)


def _field_count(field: str, maximum: int) -> int | None:
    if field == "*":
        return maximum
    if field.startswith("*/"):
        try:
            return max(1, maximum // int(field[2:]))
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return _value_count(field)


def _value_count(field: str) -> int | None:
    parts = field.split(",")
    if all(re.fullmatch(r"[A-Za-z0-9]+", part) for part in parts):
        return len(parts)
    return None
