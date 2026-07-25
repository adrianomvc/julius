"""USD é a única moeda aceita — e a AWS já reporta custo em USD.

Cost Explorer, Cost and Usage Report, Budgets e a Price List API reportam em
USD independentemente da moeda de pagamento configurada na conta. A preferência
de moeda do Billing afeta apenas a fatura final: a conversão acontece do lado
da AWS, na emissão, com o câmbio do dia.

Por isso o Julius não converte nada. Se uma resposta trouxer moeda diferente de
USD, o valor não é reinterpretado nem convertido por câmbio próprio: vira
lacuna registrada, porque um número convertido por taxa nossa não reproduz a
fatura e não tem procedência auditável.

Referência: <https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/manage-payment-method.html>
"""

from __future__ import annotations

USD = "USD"

# Unidades tratadas como USD: a AWS omite ou preenche a unidade de formas
# diferentes em grupos sem cobrança, e isso não é sinal de outra moeda.
_ACCEPTED = frozenset({"", USD, "N/A", "NONE"})


class UnsupportedCurrencyError(RuntimeError):
    """A AWS respondeu em uma moeda diferente de USD, contra o esperado."""

    def __init__(self, currency: str):
        self.currency = (currency or "?").upper()
        super().__init__(f"moeda diferente de USD: {self.currency}")


def usd_amount(amount: float | None, currency: str | None) -> float | None:
    """Valor em USD, ou `None` quando a AWS reportou outra moeda.

    Valor zerado é aceito em qualquer unidade: zero não tem moeda.
    """
    value = float(amount or 0.0)
    unit = str(currency or "").strip().upper()
    if unit in _ACCEPTED or value == 0.0:
        return value
    return None


def non_usd_gap(currency: str | None) -> str:
    """Texto estável de lacuna para uma resposta fora de USD."""
    return (
        f"Cost Explorer respondeu em {(currency or '?').upper()}; "
        "o Julius só aceita USD e não converte"
    )
