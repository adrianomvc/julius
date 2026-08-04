"""Quais hipóteses o motor já sabe calcular, e quais ainda não.

Setenta regras emitem sinal; nove tinham cálculo. As sessenta e uma restantes não
estavam sem fórmula por decisão — estavam sem ninguém ter perguntado se alguma das
fórmulas existentes servia. `glue_shuffle_reduction_v1` responde a três regras da
família `shuffle_partitioning` e estava ligada a uma.

Este módulo faz essa pergunta de forma sistemática, e a resposta tem três estados:

- **`calculated`** — existe método (a análise escolhe o cenário e o motor executa)
  ou faixa contextual autorizada para este `rule_id`;
- **`candidate`** — uma irmã da mesma família já tem cálculo. É o achado acionável:
  a fórmula existe, falta declarar que ela serve aqui também;
- **`uncovered`** — nenhuma regra da família tem cálculo. Ou a fórmula precisa ser
  escrita, ou a família não admite cifra e isso deveria estar dito em algum lugar.

O terceiro estado é o que impede este módulo de virar um relatório que sempre diz
"tudo bem". Uma família inteira sem cálculo é informação, não silêncio.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from julius.findings.signal import Signal
from julius.knowledge.contextual_estimation import allowed_methods
from julius.knowledge.generative_estimation import eligible_rule_ids
from julius.knowledge.remediation import CATALOG, FAMILIES


@dataclass(frozen=True)
class SignalCoverage:
    """O que o motor consegue dizer sobre um `rule_id` que emite sinal."""

    rule_id: str
    family: str
    #: Método determinístico autorizado para este `rule_id`, quando existe.
    method: str
    #: A faixa contextual é permitida para este `rule_id`.
    generative: bool
    #: Método de outra regra da **mesma família**. É o que torna esta linha
    #: acionável: a conta já existe e alguém precisa declarar que ela serve aqui.
    sibling_method: str
    #: Irmã da mesma família que já aceita faixa contextual.
    sibling_generative: str
    resolved_by: str
    effort: int

    @property
    def status(self) -> str:
        if self.method or self.generative:
            return "calculated"
        if self.sibling_method or self.sibling_generative:
            return "candidate"
        return "uncovered"

    @property
    def reason(self) -> str:
        """Uma frase dizendo o que falta, para quem for agir sobre a linha."""
        if self.method:
            return f"método {self.method}"
        if self.generative:
            return "faixa contextual autorizada"
        if self.sibling_method:
            return (
                f"{self.sibling_method} já responde a mesma família; "
                "falta autorizá-lo para este rule_id"
            )
        if self.sibling_generative:
            return (
                f"{self.sibling_generative} já usa faixa contextual na mesma "
                "família; falta declarar mecanismo e baseline deste rule_id"
            )
        if not self.family:
            return "sem família de remediação: classificar antes de estimar"
        return (
            f"nenhuma regra de {self.family} em "
            f"{service_of(self.rule_id)} tem cálculo"
        )


def service_of(rule_id: str) -> str:
    """O serviço, pelo prefixo do identificador.

    Todos os 135 `rule_id` do produto começam pelo serviço — `GLUE-`, `SM-`, `SFN-`,
    `ATHENA-`, `S3-`, `REDSHIFT-` —, e `tests/test_remediation_catalog.py` é o que
    impede um identificador de nascer fora dessa forma.
    """
    return rule_id.split("-", 1)[0]


def _precedentes(mapa: dict[str, str] | tuple[str, ...]) -> dict[tuple[str, str], str]:
    """(família, serviço) → um `rule_id` que já tem cálculo, para citar.

    **O serviço entra na chave, e essa é a correção que importa.** Só a família
    fazia `glue_interactive_capacity_reduction_v1` aparecer como precedente para
    `REDSHIFT-RESIZE-TARGET`: as duas ajustam capacidade provisionada, e nenhuma
    linha da fórmula de sessão Glue sabe o que é um nó de Redshift.

    A família diz que a alavanca é a mesma; a fórmula está presa a um serviço, a um
    tipo de ativo e ao inventário que os descreve. Sugerir uma para o outro produz
    exatamente o tipo de reaproveitamento que parece economia de esforço e é erro
    de cálculo.
    """
    saida: dict[tuple[str, str], str] = {}
    for rule_id in mapa:
        familia = CATALOG.get(rule_id, "")
        chave = (familia, service_of(rule_id))
        if familia and chave not in saida:
            # De `_ALLOWED` guarda o **método**, que é o que alguém precisa
            # autorizar; da lista generativa guarda o `rule_id`, porque lá o método
            # é derivado do mecanismo e citar o precedente é o que orienta.
            saida[chave] = mapa[rule_id] if isinstance(mapa, dict) else rule_id
    return saida


def coverage_for(rule_ids: Iterable[str]) -> list[SignalCoverage]:
    """A cobertura de cada `rule_id`, ordenada do mais acionável para o menos."""
    metodos = allowed_methods()
    generativas = eligible_rule_ids()
    metodo_precedente = _precedentes(metodos)
    generativa_precedente = _precedentes(generativas)

    linhas = []
    for rule_id in sorted(set(rule_ids)):
        familia = CATALOG.get(rule_id, "")
        chave = (familia, service_of(rule_id))
        proprio_metodo = metodos.get(rule_id, "")
        irma_metodo = metodo_precedente.get(chave, "")
        irma_generativa = generativa_precedente.get(chave, "")
        linhas.append(
            SignalCoverage(
                rule_id=rule_id,
                family=familia,
                method=proprio_metodo,
                generative=rule_id in generativas,
                sibling_method="" if proprio_metodo else irma_metodo,
                sibling_generative=(
                    "" if rule_id in generativas else irma_generativa
                ),
                resolved_by=(
                    FAMILIES[familia].resolved_by if familia in FAMILIES else "time"
                ),
                effort=FAMILIES[familia].effort if familia in FAMILIES else 5,
            )
        )
    ordem = {"candidate": 0, "uncovered": 1, "calculated": 2}
    return sorted(linhas, key=lambda item: (ordem[item.status], item.family, item.rule_id))


def coverage_for_signals(signals: Iterable[Signal]) -> list[SignalCoverage]:
    return coverage_for(signal.rule_id for signal in signals)


def summary(linhas: Iterable[SignalCoverage]) -> dict[str, int]:
    contagem = {"calculated": 0, "candidate": 0, "uncovered": 0}
    for linha in linhas:
        contagem[linha.status] += 1
    return contagem


__all__ = [
    "SignalCoverage",
    "coverage_for",
    "coverage_for_signals",
    "summary",
]
