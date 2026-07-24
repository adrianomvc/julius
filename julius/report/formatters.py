"""Formatação (moeda, rótulos e cores dos chips) — sem regra de negócio."""

from __future__ import annotations

BUCKET_LABELS = {
    "fazer_agora": "Fazer agora",
    "planejar": "Planejar no trimestre",
    "preparar": "Preparar migração",
    "monitorar": "Monitorar",
    "investigar_primeiro": "Investigar primeiro",
}

# Cores semânticas (do design oficial). Cor nunca é o único sinal.
BUCKET_COLORS = {
    "fazer_agora": ("#e8730c", "#fdefdd"),
    "planejar": ("#0053b3", "#e6effa"),
    "preparar": ("#8a6a2a", "#faf0dd"),
    "monitorar": ("#5b6169", "#eef0ea"),
    "investigar_primeiro": ("#b26a12", "#faf0dd"),
}

CATEGORY_LABELS = {
    "glue_job": ("Glue", "#7a4bc4", "#f1ebfb"),
    "glue_session": ("Session", "#0053b3", "#e6effa"),
    "athena_query": ("Athena", "#e8730c", "#fdefdd"),
    "s3": ("S3", "#b26a12", "#faf0dd"),
    "gov": ("Governança", "#b23b3b", "#f7e4e4"),
}


def brl(value: float | None) -> str:
    return money(value, "BRL")


def money(value: float | None, currency: str = "BRL") -> str:
    if value is None:
        return "—"
    symbols = {"BRL": "R$", "USD": "US$", "EUR": "€", "GBP": "£"}
    prefix = symbols.get(currency.upper(), currency.upper())
    return f"{prefix} " + f"{value:,.0f}".replace(",", ".")


def difficulty_color(difficulty: int) -> tuple[str, str]:
    if difficulty <= 2:
        return ("#0053b3", "#e6effa")
    if difficulty == 3:
        return ("#b26a12", "#faf0dd")
    return ("#b23b3b", "#f7e4e4")


def confidence_color(label: str) -> tuple[str, str]:
    return {
        "Alta": ("#0053b3", "#e6effa"),
        "Média": ("#b26a12", "#faf0dd"),
        "Baixa": ("#8a5a5a", "#f0e6e6"),
    }.get(label, ("#b26a12", "#faf0dd"))


def exec_color(value: int) -> str:
    return "#e8730c" if value >= 75 else "#b26a12" if value >= 50 else "#8a9082"


def strat_color(value: int) -> str:
    return "#0053b3" if value >= 75 else "#4f7bc0" if value >= 50 else "#96a0b4"


# Cores das faixas da barra de Pareto (do design) + segmento restante.
PARETO_SEGMENT_COLORS = ["#e8730c", "#f0902f", "#f4a95c", "#f8c793"]
PARETO_REMAINDER_COLOR = "#e6dccf"
