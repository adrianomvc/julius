"""A cobrança olha o mesmo período que a análise.

O relatório apresentou julho fechado na análise ao lado de quatro dias de agosto
na fatura, e o percentual "economia sobre a conta" comparou os dois. A causa era
uma linha: `BillingMonth.current` fixo no orquestrador, indiferente à cadência.

O que impede a volta é a asserção de que os dois períodos são **o mesmo objeto de
tempo**, e não que cada um esteja certo por si — foi assim que passaram meses os
dois certos separadamente e errados juntos.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from julius.collection.window import AnalysisWindow, BillingMonth

AGORA = datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc)


# --- BillingMonth aprende o mês fechado ------------------------------------


def test_previous_is_the_month_that_just_ended():
    mes = BillingMonth.previous(now=AGORA)

    assert (mes.month_start.isoformat(), mes.end_exclusive.isoformat()) == (
        "2026-07-01",
        "2026-08-01",
    )


def test_on_the_first_day_previous_is_last_month_not_the_one_before():
    """O dia 1º é onde um `-1 mês` ingênuo erra por um mês inteiro."""
    mes = BillingMonth.previous(now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert mes.period == "2025-12"


def test_december_closes_into_the_next_year():
    assert BillingMonth.closed("2025-12").end_exclusive.isoformat() == "2026-01-01"


@pytest.mark.parametrize("dia", [1, 15, 31])
def test_the_current_month_is_never_closed(dia):
    """Nem no último dia: o fim é exclusivo e para no dia corrente.

    É o caso que faria `is_closed` responder sim sem ter mês inteiro, e com ele
    a projeção seria pedida sobre um período já vencido.
    """
    agora = datetime(2026, 8, dia, tzinfo=timezone.utc)

    assert BillingMonth.current(now=agora).is_closed is False


def test_a_whole_month_is_closed_without_asking_the_clock():
    """`is_closed` sai da própria janela — determinístico, e sem `now`."""
    assert BillingMonth.closed("2026-07").is_closed is True
    assert BillingMonth.closed("2026-02").is_closed is True  # fevereiro, 28 dias


# --- os dois períodos não divergem -----------------------------------------


def test_monthly_bills_exactly_the_month_it_analyses():
    """A asserção que teria pego o defeito original."""
    from julius.collection.orchestrator import _billing_for_window

    janela = AnalysisWindow.previous_calendar_month(now=AGORA)
    cobranca = _billing_for_window(janela, cadence="monthly", now=AGORA)

    assert (cobranca.month_start, cobranca.end_exclusive) == (
        janela.start_date,
        janela.end_date,
    )


def test_an_explicit_period_bills_that_period():
    from julius.collection.orchestrator import _billing_for_window

    janela = AnalysisWindow.calendar_month("2026-03")
    cobranca = _billing_for_window(janela, cadence="monthly", now=AGORA)

    assert cobranca.period == "2026-03"


def test_weekly_keeps_billing_the_current_month():
    """O modo antigo continua existindo e continua sendo MTD."""
    from julius.collection.orchestrator import _billing_for_window

    janela = AnalysisWindow.trailing(days=30, now=AGORA)
    cobranca = _billing_for_window(janela, cadence="weekly", now=AGORA)

    assert cobranca.is_closed is False
    assert cobranca.month_start.isoformat() == "2026-08-01"


# --- a projeção não acontece em mês fechado --------------------------------


class _CeFalso:
    """Cost Explorer mínimo que registra se a projeção foi pedida."""

    def __init__(self):
        self.forecast_chamado = False

    def get_cost_and_usage(self, **_):
        return {
            "ResultsByTime": [
                {
                    "Estimated": False,
                    "Groups": [
                        {
                            "Keys": ["AWS Glue"],
                            "Metrics": {"UnblendedCost": {"Amount": "100", "Unit": "USD"}},
                        }
                    ],
                }
            ]
        }

    def get_cost_forecast(self, **_):
        self.forecast_chamado = True
        return {"Total": {"Amount": "10", "Unit": "USD"}}


def test_a_closed_month_is_never_forecast():
    """Não é preferência de exibição: `GetCostForecast` exige data futura.

    Com julho fechado, `billing_end` é 1º de agosto — a projeção sairia sobre
    agosto, que não é o mês do painel, e partindo de uma data já vencida. O que
    seria informação a mais viraria erro de coleta.
    """
    from julius.collection.collectors import cost_explorer

    ce = _CeFalso()
    cost_explorer.collect_services(
        ce, billing=BillingMonth.closed("2026-07"), include_forecast=True
    )

    assert ce.forecast_chamado is False


def test_the_current_month_still_gets_its_forecast():
    """A guarda não pode ter desligado o caso legítimo.

    Dia 20 e não `AGORA`: em 4 de agosto só há três dias observados, e
    `MIN_DAYS_FOR_FORECAST` já barra a projeção por conta própria — o teste
    passaria sem provar nada sobre a guarda nova.
    """
    from julius.collection.collectors import cost_explorer

    ce = _CeFalso()
    meio_do_mes = datetime(2026, 8, 20, tzinfo=timezone.utc)
    cost_explorer.collect_services(
        ce, billing=BillingMonth.current(now=meio_do_mes), include_forecast=True
    )

    assert ce.forecast_chamado is True


def test_the_service_declares_which_period_it_measured():
    """O dado se descreve; o relatório não redescobre o calendário."""
    from julius.collection.collectors import cost_explorer

    fechado = cost_explorer.collect_services(
        _CeFalso(), billing=BillingMonth.closed("2026-07")
    )
    corrente = cost_explorer.collect_services(
        _CeFalso(), billing=BillingMonth.current(now=AGORA)
    )

    assert fechado[0].period_kind == "closed_month"
    assert corrente[0].period_kind == "month_to_date"


# --- o que o leitor vê ------------------------------------------------------


def test_the_label_names_the_month_when_it_is_closed():
    from julius.collection.models import Account, ServiceCost
    from julius.reporting.view_models import _billing_label

    conta = Account(
        account_id="1",
        services=[
            ServiceCost(
                name="AWS Glue",
                monthly_cost=1.0,
                period_start="2026-07-01",
                period_kind="closed_month",
            )
        ],
    )

    assert _billing_label(conta) == "Cobrança de julho/2026"


def test_the_label_stays_mtd_for_the_current_month():
    from julius.collection.models import Account, ServiceCost
    from julius.reporting.view_models import _billing_label

    conta = Account(
        account_id="1",
        services=[
            ServiceCost(name="AWS Glue", monthly_cost=1.0, period_start="2026-08-01")
        ],
    )

    assert _billing_label(conta) == "Cobrança do mês (MTD)"


def test_a_reread_dataset_keeps_saying_what_it_was():
    """Rótulo lido do dado, e não do calendário de hoje.

    Um dataset de julho aberto em dezembro precisa continuar dizendo julho —
    é a razão de `period_kind` ser gravado em vez de deduzido na apresentação.
    """
    from julius.collection.models import Account, ServiceCost
    from julius.reporting.view_models import _billing_label

    conta = Account(
        account_id="1",
        services=[
            ServiceCost(
                name="AWS Glue",
                monthly_cost=1.0,
                period_start="2026-02-01",
                period_kind="closed_month",
            )
        ],
    )

    assert _billing_label(conta) == "Cobrança de fevereiro/2026"


# --- o dataset não se reinterpreta sozinho ---------------------------------


def test_the_cadence_survives_the_round_trip():
    """Sem isto, o default de `Account` decidiria a cadência na releitura.

    Enquanto o default foi "weekly" ninguém notou. Trocá-lo para "monthly"
    passaria a rotular todo dataset antigo como mensal, em silêncio — que é a
    reinterpretação que `dataset_schema_version` existe para impedir.
    """
    from julius.collection.models import Account
    from julius.collection.normalizers.dump import account_to_dataset
    from julius.collection.normalizers.loader import account_from_dataset

    original = Account(account_id="1", cadence="weekly", financial_period="")
    voltou = account_from_dataset(account_to_dataset(original))

    assert voltou.cadence == "weekly"


def test_a_dataset_without_cadence_is_read_as_the_old_behaviour():
    """Dataset da versão 2 não existe mais, mas o default precisa ser honesto."""
    from julius.collection.normalizers.loader import account_from_dataset
    from julius.collection.settings import DATASET_SCHEMA_VERSION

    conta = account_from_dataset(
        {"dataset_schema_version": DATASET_SCHEMA_VERSION, "account": "1"}
    )

    assert conta.cadence == "weekly"
