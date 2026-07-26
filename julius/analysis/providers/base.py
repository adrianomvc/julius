"""O contrato que todo provedor de análise contextual honra.

É o primeiro ponto do projeto onde substituição importa de verdade: quem monta
o contexto e quem consome o resultado **não podem saber** qual provedor está em
uso. Trocar Devin por preenchimento manual não pode mudar o formato do
resultado, nem o tipo do erro, nem exigir preparo extra do chamador.

O contrato, explícito porque é o que os testes verificam:

1. `prepare` recebe uma análise e um diretório, escreve o que o provedor precisa
   e devolve os arquivos escritos — sempre incluindo o contexto e o schema.
2. `collect` lê o resultado e devolve uma `ContextualAnalysis` **validada**.
3. Falha sempre como `AgentOutputError`. Nenhuma exceção crua de parsing, de
   arquivo ou de rede escapa: quem chama trata um tipo só.
4. Nenhum provedor exige do chamador nada além do workspace.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from julius.analysis.response_validator import ContextualAnalysis


@dataclass(frozen=True)
class Workspace:
    """Onde o provedor deixa e busca arquivos, com os nomes canônicos."""

    directory: Path

    @classmethod
    def at(cls, path: str | Path) -> Workspace:
        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory=directory)

    @property
    def context(self) -> Path:
        return self.directory / "context.json"

    @property
    def schema(self) -> Path:
        return self.directory / "output-schema.json"

    @property
    def instructions(self) -> Path:
        return self.directory / "instructions.md"

    @property
    def result(self) -> Path:
        return self.directory / "result.json"


class AnalysisProvider(ABC):
    """Produz análise contextual a partir de um scan determinístico.

    O Julius decide sozinho o que é oportunidade, quanto vale e em que ordem
    executar. Um provedor só acrescenta contexto — diagnóstico, passos, riscos —
    e nunca altera número, prioridade ou conjunto de achados. É por isso que o
    resultado passa por validação antes de virar `ContextualAnalysis`.
    """

    #: Identificador estável, usado no relatório para dizer de onde veio.
    name: str = "base"

    @abstractmethod
    def prepare(self, analysis, workspace: Workspace, *, top: int = 10) -> list[Path]:
        """Escreve o que o provedor precisa e devolve os arquivos escritos."""

    @abstractmethod
    def collect(self, workspace: Workspace) -> ContextualAnalysis:
        """Lê e valida o resultado. Falha só como `AgentOutputError`."""
