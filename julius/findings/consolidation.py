"""Onde a hipótese encontra a ação, e o dinheiro é contado uma vez só.

Um sinal e um achado podem descrever a **mesma** correção sobre o **mesmo** ativo.
Quando isso acontece, os dois derivam do mesmo custo — `code_pattern_saving` e
`potential()` chamam ambos `window_baseline(job)` — e mostrá-los lado a lado, cada
um com seu número, faz o leitor somar a mesma linha da fatura duas vezes.

Duas regras resolvem isso, e nenhuma delas apaga informação:

**R2 — a hipótese cede lugar à ação.** Se a família já tem achado com cifra, a
pergunta que o sinal fazia foi respondida: existe ação, existe número, existe dono.
A faixa sai; o sinal fica, porque as linhas do script e o hash do artefato continuam
sendo o que alguém precisa para implementar.

**R4 — a soma das faixas de um ativo cabe no que sobrou dele.** Depois de a economia
identificada tomar sua parte, o que resta é o teto de tudo que ainda é hipótese ali.
Sem isto, três sinais sobre um job de US$ 4.200 podiam somar US$ 6.300, porque cada
`potential()` limita a si mesmo a 50% do ativo e nenhum deles conhece os outros.

O módulo é puro e vive em `findings` de propósito: ele lê `remediation_family`, que
ambos os tipos já carregam, e não precisa do catálogo — que mora em `knowledge`, para
onde esta camada não enxerga.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from julius.findings.opportunity import Opportunity
from julius.findings.signal import PotentialRange, Signal

#: O que o sinal passa a dizer quando a ação já existe. Fica em `missing_evidence`
#: e não em `observation` porque o que mudou não foi o que se observou — foi o que
#: ainda falta, que agora é nada.
_JA_QUANTIFICADO = (
    "a economia desta correção já está no portfólio, no achado determinístico da "
    "mesma família; esta hipótese não acrescenta valor, só detalhe de implementação"
)


def _chave(item: Opportunity | Signal) -> tuple[str, str, str]:
    return (item.asset_type, item.asset_name, item.remediation_family)


def absorb_quantified(
    opportunities: list[Opportunity], signals: list[Signal]
) -> list[Signal]:
    """R2: sinal cuja família já tem cifra perde a faixa, e só a faixa.

    O filtro é por `include_in_portfolio`, e não por "existe achado": um achado
    bloqueado ou de economia zero não respondeu nada, e suprimir a faixa por causa
    dele esconderia a única estimativa disponível sobre aquele ativo.

    Sinal sem família não é absorvido por ninguém — `_chave` o isolaria junto de
    todos os outros sem família do mesmo ativo, e isso fundiria correções que
    ninguém declarou serem a mesma.
    """
    quantificadas = {
        _chave(item)
        for item in opportunities
        if item.remediation_family
        and item.include_in_portfolio
        and item.portfolio_gain.monthly_expected > 0
    }
    return [
        (
            replace(
                signal,
                potential_range=None,
                missing_evidence=[*signal.missing_evidence, _JA_QUANTIFICADO],
            )
            if signal.remediation_family
            and signal.potential_range is not None
            and _chave(signal) in quantificadas
            else signal
        )
        for signal in signals
    ]


def cap_ranges_by_asset(
    opportunities: list[Opportunity], signals: list[Signal]
) -> list[Signal]:
    """R4: as faixas de um ativo cabem no que sobrou do custo dele.

    O saldo é o custo do ativo menos o que a economia identificada já reivindicou.
    Quando ele não chega para todas as faixas, elas são reduzidas na proporção do
    que pediam — ninguém é zerado por chegar depois, porque a ordem entre hipóteses
    do mesmo ativo é arbitrária e não deveria decidir qual delas aparece.

    O custo vem de `PotentialRange.baseline`, que é o número de que a própria faixa
    saiu. Faixa sem baseline positivo não participa: sem saber de que custo ela veio,
    reduzi-la seria inventar um teto.
    """
    reivindicado: dict[tuple[str, str], float] = defaultdict(float)
    for item in opportunities:
        if item.include_in_portfolio:
            reivindicado[(item.asset_type, item.asset_name)] += (
                item.portfolio_gain.monthly_expected
            )

    # O par carrega a faixa já desembrulhada: guardar só o sinal obrigaria a
    # reabrir o `Optional` a cada uso, e o `assert`/`ignore` que isso pede é onde
    # um `None` real passaria despercebido no dia em que o filtro mudasse.
    por_ativo: dict[tuple[str, str], list[tuple[Signal, PotentialRange]]] = defaultdict(
        list
    )
    for signal in signals:
        faixa = signal.potential_range
        if faixa is not None and faixa.baseline > 0:
            por_ativo[(signal.asset_type, signal.asset_name)].append((signal, faixa))

    ajustados: dict[int, Signal] = {}
    for ativo, grupo in por_ativo.items():
        custo = max(faixa.baseline for _, faixa in grupo)
        saldo = max(0.0, custo - reivindicado.get(ativo, 0.0))
        pedido = sum(faixa.expected for _, faixa in grupo)
        if pedido <= saldo or pedido <= 0:
            continue
        fator = saldo / pedido
        for signal, faixa in grupo:
            ajustados[id(signal)] = replace(
                signal,
                potential_range=replace(
                    faixa,
                    low=round(faixa.low * fator, 2),
                    expected=round(faixa.expected * fator, 2),
                    high=round(faixa.high * fator, 2),
                    caveat=(
                        f"{faixa.caveat}; faixa reduzida a {fator:.0%} porque as "
                        "hipóteses deste ativo somavam mais do que sobrou do custo "
                        "dele depois da economia já identificada"
                    ),
                ),
            )
    return [ajustados.get(id(signal), signal) for signal in signals]


def deduplicate(
    opportunities: list[Opportunity], signals: list[Signal]
) -> list[Signal]:
    """R2 e depois R4, nesta ordem.

    A ordem importa: absorver primeiro tira da conta as faixas que a ação
    determinística já cobria, e o saldo que sobra para as demais é maior — e
    correto. Ao contrário, o teto seria repartido com hipóteses que nem deveriam
    estar disputando.
    """
    return cap_ranges_by_asset(opportunities, absorb_quantified(opportunities, signals))
