"""Janelas de tempo da coleta — definidas uma vez, em UTC, para todo mundo.

Antes deste módulo cada coletor calculava a própria janela: uns com
`date.today()` (fuso local), outros com `datetime.now(timezone.utc)`, uns por
mês-calendário e outros por dias móveis. Três consequências: Athena e Glue
produziam baselines de períodos diferentes que acabavam somados no mesmo total;
a projeção para fim de mês extrapolava um MTD de poucos dias; e um scan rodado
à noite, no fim do mês, deslocava o corte de consumo em relação ao corte da
cobrança, derrubando o rateio sem que a causa aparecesse em lugar nenhum.

São dois períodos, com propósitos distintos, e eles nunca se misturam num mesmo
número:

- `AnalysisWindow` — N dias UTC **completos**. É o período de tudo que é
  comparado entre serviços: comportamento, custo, rateio, baseline de
  oportunidade. Nunca inclui o dia corrente, que está parcial.
- `BillingMonth` — mês-calendário até o último dia fechado. É a âncora com a
  fatura que a AWS emite e com o que o console mostra. Só o painel de cobrança
  usa este período.

Trinta dias não são um mês: o mês médio tem 30,44 dias. Um valor apurado na
janela de análise é rotulado pelo número de dias, nunca como "mensal".
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

DEFAULT_ANALYSIS_DAYS = 30


def utc_now(now: datetime | None = None) -> datetime:
    """Instante de referência em UTC, a única base de tempo da coleta."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


@dataclass(frozen=True)
class AnalysisWindow:
    """`days` dias UTC completos: `start` inclusivo, `end` exclusivo."""

    start: datetime
    end: datetime
    days: int = DEFAULT_ANALYSIS_DAYS

    @classmethod
    def trailing(
        cls, *, days: int = DEFAULT_ANALYSIS_DAYS, now: datetime | None = None
    ) -> AnalysisWindow:
        """Últimos `days` dias fechados, sem o dia corrente parcial."""
        reference = utc_now(now)
        end = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        return cls(start=end - timedelta(days=days), end=end, days=days)

    @classmethod
    def calendar_month(cls, period: str) -> AnalysisWindow:
        """Mês UTC completo em `YYYY-MM`, para o fechamento financeiro."""
        try:
            year_text, month_text = period.split("-", 1)
            year, month = int(year_text), int(month_text)
            start = datetime(year, month, 1, tzinfo=timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("period deve usar o formato YYYY-MM") from exc
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        return cls(start=start, end=end, days=(end - start).days)

    @classmethod
    def previous_calendar_month(
        cls, *, now: datetime | None = None
    ) -> AnalysisWindow:
        reference = utc_now(now)
        current = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_day = current - timedelta(days=1)
        return cls.calendar_month(previous_day.strftime("%Y-%m"))

    def capped(self, max_days: int | None) -> AnalysisWindow:
        """A mesma janela, encurtada ao que a fonte ainda consegue devolver.

        Encurta pelo início e preserva o fim: o que a AWS retém é a parte
        recente. Pedir mais dias do que a retenção do serviço **não** dá erro —
        devolve menos dado, e como `coverage_days` dos modelos é preenchido com
        `window.days` (a janela pedida, não a observada), o dataset passaria a
        afirmar uma cobertura que não tem. É esse silêncio que o teto evita.
        """
        if max_days is None or max_days >= self.days:
            return self
        return AnalysisWindow(
            start=self.end - timedelta(days=max_days), end=self.end, days=max_days
        )

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        """Fim exclusivo — o formato que o Cost Explorer espera em `End`."""
        return self.end.date()

    @property
    def data_through(self) -> date:
        """Último dia coberto pela janela; `end` é exclusivo."""
        return self.end_date - timedelta(days=1)

    @property
    def day_keys(self) -> list[str]:
        """Dias da janela em ISO, a chave usada no rateio diário de custo."""
        return [
            (self.start_date + timedelta(days=offset)).isoformat()
            for offset in range(self.days)
        ]

    @property
    def label(self) -> str:
        return f"{self.days} dias ({self.start_date.isoformat()} a {self.data_through.isoformat()})"

    def contains(self, moment: datetime | None) -> bool:
        if not isinstance(moment, datetime):
            return False
        normalized = utc_now(moment)
        return self.start <= normalized < self.end

    def overlap_seconds(self, started: datetime | None, completed: datetime | None) -> float:
        """Segundos de uma execução dentro da janela.

        Execução sem término conhecido é tratada como ainda em curso e cortada
        no fim da janela — nunca extrapolada além dela.
        """
        if not isinstance(started, datetime):
            return 0.0
        begin = max(utc_now(started), self.start)
        finish = min(
            utc_now(completed) if isinstance(completed, datetime) else self.end,
            self.end,
        )
        return max(0.0, (finish - begin).total_seconds())


@dataclass(frozen=True)
class BillingMonth:
    """Mês-calendário da cobrança, em UTC. Dois modos, e eles não se confundem.

    Existe só para o painel que reconcilia com a fatura. Nenhum baseline de
    oportunidade se apoia neste período.

    - `current()` — mês corrente até o último dia fechado. Parcial por definição,
      e é o único caso em que projetar o fechamento faz sentido.
    - `closed(period)` / `previous()` — um mês inteiro que já terminou. É o que a
      cadência mensal usa, e o que a fatura da AWS de fato cobra.

    A distinção não é cosmética: `GetCostForecast` exige data futura, então
    projetar um mês encerrado não é apenas inútil, é chamada que falha. Quem
    precisa decidir isso pergunta a `is_closed` em vez de reabrir o calendário.
    """

    month_start: date
    end_exclusive: date

    @classmethod
    def current(cls, *, now: datetime | None = None) -> BillingMonth:
        today = utc_now(now).date()
        month_start = today.replace(day=1)
        # O fim do Cost Explorer é exclusivo. No dia 1 avançar um dia evita a
        # janela inválida Start == End; nesse caso a cobrança inclui o dia atual.
        end_exclusive = today if today > month_start else today + timedelta(days=1)
        return cls(month_start=month_start, end_exclusive=end_exclusive)

    @classmethod
    def closed(cls, period: str) -> BillingMonth:
        """Mês `YYYY-MM` inteiro. Reusa a validação da janela de análise.

        Construir sobre `AnalysisWindow.calendar_month` em vez de repetir o
        `split` e o cuidado com dezembro é o que garante que os dois períodos
        cortem no mesmo instante — que é a coisa toda que este módulo existe
        para sustentar.
        """
        janela = AnalysisWindow.calendar_month(period)
        return cls(month_start=janela.start_date, end_exclusive=janela.end_date)

    @classmethod
    def previous(cls, *, now: datetime | None = None) -> BillingMonth:
        """O último mês encerrado — o padrão da cadência mensal."""
        reference = utc_now(now).date().replace(day=1)
        return cls.closed((reference - timedelta(days=1)).strftime("%Y-%m"))

    @property
    def is_closed(self) -> bool:
        """O período cobre um mês inteiro? Decide projeção e rótulo, num lugar só.

        Deliberadamente sem consultar o relógio. Um mês-calendário completo é
        encerrado por definição, e `current()` nunca produz um — nem no último
        dia do mês, porque o fim é exclusivo e para no dia corrente. Derivar da
        própria janela mantém a resposta determinística no teste e dispensa
        passar `now` para uma propriedade, que não teria onde recebê-lo.
        """
        return (self.end_exclusive - self.month_start).days == self.days_in_month

    @property
    def period(self) -> str:
        """`YYYY-MM` do mês coberto, para rótulo e para o dataset."""
        return self.month_start.strftime("%Y-%m")

    @property
    def data_through(self) -> date:
        return self.end_exclusive - timedelta(days=1)

    @property
    def observed_days(self) -> int:
        return max(1, (self.end_exclusive - self.month_start).days)

    @property
    def days_in_month(self) -> int:
        return calendar.monthrange(self.month_start.year, self.month_start.month)[1]

    def forecast_factor(self, *, minimum_days: int) -> float | None:
        """Fator MTD → fim de mês, ou `None` quando é cedo demais para projetar.

        Com poucos dias observados o fator explode (no dia 2 seria ×15) e a
        projeção deixa de ser informação. Abaixo do mínimo não se projeta: o
        MTD é reportado como MTD.
        """
        if self.observed_days < minimum_days:
            return None
        return self.days_in_month / self.observed_days
