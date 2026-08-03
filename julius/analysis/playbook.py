"""As perguntas por tipo de ativo, carregadas só quando há o ativo.

Eram uma tupla no `guardrails.py` e iam inteiras em todo pacote. Uma conta sem
Redshift recebia as duas perguntas de Redshift; uma sem SageMaker recebia as seis
de SageMaker. Isso não é só desperdício de contexto: instrução que não se aplica
a nada do pacote ensina o leitor a passar os olhos pelo bloco inteiro, e a
pergunta que importava viaja junto com nove que não importavam.

Aqui elas são markdown, agrupadas por serviço num arquivo e seccionadas por tipo
de ativo dentro dele. Os dois níveis servem a coisas diferentes: o arquivo é para
quem edita — ninguém quer sete arquivos de duas linhas —, e a seção é o que o
pacote carrega, que precisa ser preciso. Uma conta só com endpoint recebe as
perguntas de endpoint, não as de training job que moram no mesmo arquivo.

**Por que sob `julius/` e não em `docs/ai/`.** A fonte canônica da Skill é
`docs/ai/`, e `docs/` não entra no wheel — `packages.find` inclui só `julius*`.
Estas perguntas são injetadas no prompt em tempo de execução, então elas viajam
com o motor ou o Julius instalado monta pacote sem elas. É a mesma classe de erro
que `tests/test_package_data.py` existe para pegar, e por isso o diretório está
declarado em `package-data`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PLAYBOOKS = Path(__file__).resolve().parent / "playbooks"

#: Achado que não é de um ativo, e sim da relação entre dois. Não tem
#: `asset_type` próprio no inventário, então entra pela regra que o produz.
CROSS_SERVICE = "cross_service"
_PREFIXO_CROSS_SERVICE = "XSVC-"


@dataclass(frozen=True)
class Playbook:
    """Um arquivo de playbook e as seções por tipo de ativo dentro dele."""

    name: str
    path: Path
    #: `asset_type` → perguntas daquele ativo, na ordem em que foram escritas.
    sections: dict[str, tuple[str, ...]]


def _parse(path: Path) -> Playbook:
    texto = path.read_text(encoding="utf-8")
    corpo = re.sub(r"(?s)\A---\n.*?\n---\n", "", texto)
    sections: dict[str, tuple[str, ...]] = {}
    atual: str | None = None
    perguntas: list[str] = []
    for linha in corpo.splitlines():
        cabecalho = re.match(r"^##\s+([a-z_]+)\s*$", linha)
        if cabecalho:
            if atual is not None:
                sections[atual] = tuple(perguntas)
            atual = cabecalho.group(1)
            perguntas = []
            continue
        if linha.startswith("- ") and atual is not None:
            perguntas.append(linha[2:].strip())
    if atual is not None:
        sections[atual] = tuple(perguntas)
    return Playbook(name=path.stem, path=path, sections=sections)


def load_playbooks() -> list[Playbook]:
    return [_parse(path) for path in sorted(PLAYBOOKS.glob("*.md"))]


def known_asset_types() -> set[str]:
    return {
        asset for playbook in load_playbooks() for asset in playbook.sections
    }


def asset_types_in_context(
    opportunities: list[dict], signals: list[dict]
) -> set[str]:
    """O que o pacote realmente contém — a chave de tudo que carrega ou não.

    Oportunidade e sinal contam igual: as perguntas servem tanto para julgar a
    hipótese quanto para enriquecer o achado já calculado.
    """
    tipos = {str(item.get("asset_type") or "") for item in opportunities}
    tipos |= {str(item.get("asset_type") or "") for item in signals}
    regras = {str(item.get("rule_id") or "") for item in (*opportunities, *signals)}
    if any(regra.startswith(_PREFIXO_CROSS_SERVICE) for regra in regras):
        tipos.add(CROSS_SERVICE)
    return {tipo for tipo in tipos if tipo}


def select(asset_types: set[str] | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """As seções a carregar, na ordem dos arquivos. `None` carrega tudo.

    `None` não é o caso de uso normal: existe para quem monta briefing sem um
    pacote na mão — documentação, inspeção — e é o que preserva o comportamento
    antigo onde nenhum contexto foi informado.
    """
    escolhidas: list[tuple[str, tuple[str, ...]]] = []
    for playbook in load_playbooks():
        for asset, perguntas in playbook.sections.items():
            if asset_types is None or asset in asset_types:
                escolhidas.append((asset, perguntas))
    return escolhidas


def render(asset_types: set[str] | None = None) -> str:
    """O bloco que entra no briefing. Vazio quando nada do pacote casa."""
    blocos = [
        f"\n{asset}:\n" + "\n".join(f"- {pergunta}" for pergunta in perguntas)
        for asset, perguntas in select(asset_types)
        if perguntas
    ]
    return "\n".join(blocos)
