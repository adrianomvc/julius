"""Profundidade da janela e o teto de retenção de cada família.

A primeira coleta pede uma janela profunda para não esperar meses de coletas
semanais antes de o portfólio ter número. O risco de pedir mais dias é
silencioso: a AWS não devolve erro quando o período excede a retenção — devolve
menos dado. E `coverage_days` dos modelos é preenchido com a janela **pedida**,
então sem teto o dataset afirmaria uma cobertura que não tem, inflando todos os
gates de regra que leem cobertura.
"""

from __future__ import annotations

from datetime import datetime, timezone

from julius.collection.bootstrap import resolve_depth
from julius.collection.health import CollectionRecorder
from julius.collection.models import Account, CollectionHealth
from julius.collection.settings import (
    ANALYSIS_WINDOW_DAYS,
    BOOTSTRAP_WINDOW_DAYS,
    RETENTION_CEILING_DAYS,
    retention_ceiling,
)
from julius.collection.sources import (
    SOURCES,
    CollectionContext,
    Source,
    run,
)
from julius.collection.window import AnalysisWindow, BillingMonth
from julius.config import DEFAULT_CONFIG

# Privados de propósito: são as duas funções que montam as linhas de janela do
# relatório, e não há entrada pública que as exercite sem montar o pipeline todo.
from julius.reporting.view_models import _collection_health, _lookback

AGORA = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Recorte da janela
# --------------------------------------------------------------------------


def test_capping_shortens_the_start_and_keeps_the_end():
    """O que a AWS retém é a parte recente: encurta pelo começo."""
    window = AnalysisWindow.trailing(days=90, now=AGORA)

    curta = window.capped(45)

    assert curta.days == 45
    assert curta.end == window.end
    assert curta.start > window.start
    assert (curta.end - curta.start).days == 45


def test_capping_above_the_window_is_a_no_op():
    """Teto maior que a janela não estica a janela."""
    window = AnalysisWindow.trailing(days=30, now=AGORA)

    assert window.capped(90) is window
    assert window.capped(30) is window
    assert window.capped(None) is window


def test_the_default_window_is_below_every_ceiling():
    """Por isso o recorte não muda o comportamento da coleta de sempre."""
    for familia, teto in RETENTION_CEILING_DAYS.items():
        assert teto >= ANALYSIS_WINDOW_DAYS, familia


# --------------------------------------------------------------------------
# Famílias
# --------------------------------------------------------------------------


def test_every_source_declares_a_family():
    """Fonte sem família herdaria a profundidade cheia em silêncio."""
    sem_familia = [source.name for source in SOURCES if not source.family]

    assert sem_familia == []


def test_a_cost_source_shares_the_window_of_the_inventory_it_reconciles():
    """O desalinhamento aqui quebra o rateio sem aparecer em lugar nenhum.

    Se `Glue Jobs` medisse 90 dias e `Glue Cost Explorer` 45, o delta de
    reconciliação explodiria e nada no relatório diria por quê.
    """
    por_nome = {source.name: source for source in SOURCES}
    pares = (
        ("Glue Jobs", "Glue Cost Explorer"),
        ("Amazon S3", "S3 Cost Explorer"),
        ("Amazon Redshift", "Redshift Cost Explorer"),
        ("SageMaker Studio", "SageMaker Cost Explorer"),
    )

    for inventario, custo in pares:
        assert por_nome[inventario].family == por_nome[custo].family, (
            f"{custo} mediria janela diferente de {inventario}"
        )


def test_families_with_no_documented_limit_use_the_bootstrap_depth():
    assert retention_ceiling("s3") == BOOTSTRAP_WINDOW_DAYS
    assert retention_ceiling("familia-inexistente") == BOOTSTRAP_WINDOW_DAYS
    # E as que têm limite documentado ficam nele.
    assert retention_ceiling("athena") == 45
    assert retention_ceiling("stepfunctions") == 30


# --------------------------------------------------------------------------
# O recorte chegando na fonte
# --------------------------------------------------------------------------


def _contexto(dias: int) -> CollectionContext:
    return CollectionContext(
        session=None,
        window=AnalysisWindow.trailing(days=dias, now=AGORA),
        billing=BillingMonth.current(now=AGORA),
        account=Account(account_id="123456789012"),
        config=DEFAULT_CONFIG,
    )


def _fonte(familia: str, visto: list[int]) -> Source:
    return Source(
        name="Fonte de teste",
        collect=lambda ctx: visto.append(ctx.window.days) or [],
        family=familia,
        impact="",
        next_action="",
    )


def test_a_source_receives_the_window_capped_to_its_family():
    """Athena retém 45 dias; pedir 90 devolveria menos dado sem avisar."""
    visto: list[int] = []
    ctx = _contexto(BOOTSTRAP_WINDOW_DAYS)

    run(_fonte("athena", visto), ctx, CollectionRecorder())

    assert visto == [45]
    # A janela da conta não é alterada pelo recorte de uma fonte.
    assert ctx.window.days == BOOTSTRAP_WINDOW_DAYS


def test_a_family_without_a_ceiling_sees_the_whole_window():
    visto: list[int] = []

    run(_fonte("s3", visto), _contexto(BOOTSTRAP_WINDOW_DAYS), CollectionRecorder())

    assert visto == [BOOTSTRAP_WINDOW_DAYS]


def test_the_health_entry_records_the_window_the_source_measured():
    """Ler cobertura sem saber a janela é comparar períodos diferentes."""
    recorder = CollectionRecorder()

    run(_fonte("athena", []), _contexto(BOOTSTRAP_WINDOW_DAYS), recorder)
    run(_fonte("s3", []), _contexto(BOOTSTRAP_WINDOW_DAYS), recorder)

    assert [entry.window_days for entry in recorder.entries] == [
        45,
        BOOTSTRAP_WINDOW_DAYS,
    ]


# --------------------------------------------------------------------------
# Decisão do bootstrap
# --------------------------------------------------------------------------


def test_the_first_collection_of_an_account_goes_deep(tmp_path):
    """Sem checkpoint, é a primeira coleta desta conta."""
    dias, bootstrap = resolve_depth(
        lookback_days=ANALYSIS_WINDOW_DAYS,
        cadence="weekly",
        checkpoint=tmp_path / "conta.json",
    )

    assert (dias, bootstrap) == (BOOTSTRAP_WINDOW_DAYS, True)


def test_a_later_collection_keeps_the_routine_window(tmp_path):
    """Bootstrap é caro uma vez; a rotina continua em 30 dias."""
    checkpoint = tmp_path / "conta.json"
    checkpoint.write_text("{}", encoding="utf-8")

    dias, bootstrap = resolve_depth(
        lookback_days=ANALYSIS_WINDOW_DAYS,
        cadence="weekly",
        checkpoint=checkpoint,
    )

    assert (dias, bootstrap) == (ANALYSIS_WINDOW_DAYS, False)


def test_the_flag_overrides_the_checkpoint_in_both_directions(tmp_path):
    """Repetir um bootstrap (escopo novo) e recusá-lo (custo do scan)."""
    existente = tmp_path / "conta.json"
    existente.write_text("{}", encoding="utf-8")

    assert resolve_depth(
        lookback_days=ANALYSIS_WINDOW_DAYS,
        cadence="weekly",
        checkpoint=existente,
        explicit=True,
    ) == (BOOTSTRAP_WINDOW_DAYS, True)

    assert resolve_depth(
        lookback_days=ANALYSIS_WINDOW_DAYS,
        cadence="weekly",
        checkpoint=tmp_path / "nova.json",
        explicit=False,
    ) == (ANALYSIS_WINDOW_DAYS, False)


def test_an_explicit_lookback_deeper_than_the_bootstrap_wins(tmp_path):
    """`max`, não substituição: quem pediu 120 dias não recebe 90."""
    dias, bootstrap = resolve_depth(
        lookback_days=120,
        cadence="weekly",
        checkpoint=tmp_path / "conta.json",
    )

    assert (dias, bootstrap) == (120, True)


def test_a_monthly_cadence_is_never_a_bootstrap(tmp_path):
    """Mês-calendário é fechamento de um período, não janela móvel."""
    dias, bootstrap = resolve_depth(
        lookback_days=ANALYSIS_WINDOW_DAYS,
        cadence="monthly",
        checkpoint=tmp_path / "conta.json",
        explicit=True,
    )

    assert (dias, bootstrap) == (ANALYSIS_WINDOW_DAYS, False)


# --------------------------------------------------------------------------
# O que o relatório mostra
#
# O teste de baseline não cobre isto: os datasets de `data/sample/` não trazem
# `collection_health`, então a tabela de saúde sai vazia lá e uma regressão
# nestas linhas passaria sem ninguém ver.
# --------------------------------------------------------------------------


def test_the_health_table_shows_the_window_of_each_source():
    account = Account(
        account_id="123456789012",
        collection_health=[
            CollectionHealth(source="Athena Queries", window_days=45),
            CollectionHealth(source="S3 Prefixes", window_days=90),
            CollectionHealth(source="Antiga, sem janela"),
        ],
    )

    *_, rows = _collection_health(account)

    assert [row["window"] for row in rows] == ["45 dias", "90 dias", "—"]


def test_the_lookback_line_says_when_the_window_is_a_bootstrap():
    """Cifra que apareceu no bootstrap não amadureceu em três coletas."""
    rotina = Account(account_id="1", lookback_days=30, bootstrap=False)
    primeira = Account(account_id="1", lookback_days=90, bootstrap=True)

    assert _lookback(rotina) == "30 dias"
    assert _lookback(primeira).startswith("90 dias (bootstrap")
    assert "retenção menor" in _lookback(primeira)


def test_the_scoped_context_still_shares_what_sources_hand_over():
    """O recorte monta um ctx novo; o que uma fonte escreve não pode se perder."""
    ctx = _contexto(BOOTSTRAP_WINDOW_DAYS)
    ctx.flags["antes"] = True

    def coletar(scoped: CollectionContext) -> list:
        assert scoped.flags["antes"] is True
        scoped.flags["depois"] = True
        scoped.account.region = "sa-east-1"
        return []

    run(
        Source(
            name="Fonte de teste",
            collect=coletar,
            family="athena",
            impact="",
            next_action="",
        ),
        ctx,
        CollectionRecorder(),
    )

    assert ctx.flags["depois"] is True
    assert ctx.account.region == "sa-east-1"
