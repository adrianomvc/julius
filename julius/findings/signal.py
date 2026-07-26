"""O que uma regra viu mas não consegue concluir sozinha.

`Finding` é uma observação que fecha: config declarada mais métrica medida
levam a uma ação única. `Signal` é o caso em que o gatilho é fato mas a
conclusão não é — `collect()` sobre cem linhas é correto, sobre cem milhões é
desperdício, e nem o AST nem o limiar sabem qual dos dois está na frente.

Sinal não entra no backlog, não recebe economia e não disputa posição no
ranking. Ele vai para o pacote de análise contextual, onde a única camada capaz
de ler intenção — script inteiro, SQL inteiro, processo inteiro — confirma,
descarta ou pede a evidência que falta.

A alternativa que existia antes era pior: cada padrão virava uma
`Opportunity(blocked=True)` de economia zero, e o Top 10 enchia de hipótese
vestida de achado.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Signal:
    """Uma hipótese rastreável, com o que falta para ela virar conclusão."""

    #: `code` quando o gatilho veio do script; `config` quando veio de uma
    #: propriedade declarada do recurso.
    kind: str
    rule_id: str
    asset_type: str
    asset_name: str
    #: O que foi observado, em uma frase — sem afirmar que é desperdício.
    observation: str
    #: A pergunta que a análise contextual precisa responder sobre este sinal.
    question: str
    #: Evidência que faria o sinal virar achado determinístico.
    missing_evidence: list[str] = field(default_factory=list)
    #: Localização no artefato, quando o sinal veio de código.
    artifact_sha256: str = ""
    lines: list[int] = field(default_factory=list)
    doc_links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
