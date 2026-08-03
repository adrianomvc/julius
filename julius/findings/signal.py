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

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class PotentialRange:
    """Ordem de grandeza de um sinal, e nada além disso.

    Existe para responder "vale a pena investigar?" sem responder "quanto vou
    economizar?". A diferença não é de precisão, é de natureza: a faixa é o
    custo do ativo multiplicado por uma fração plausível do padrão, e a fração
    é premissa — nenhuma medição a sustenta, porque se houvesse medição o
    achado não seria um sinal.

    Por isso `quality` é sempre `potential` e nunca `measured` ou `modeled`, e
    por isso nenhum destes valores entra em `identified_monthly`, no ranking ou
    no backlog. `tests/test_signal_range_never_enters_portfolio.py` é o que
    impede essa fronteira de se apagar sozinha com o tempo.
    """

    low: float
    expected: float
    high: float
    #: De onde veio o custo que serviu de base, em uma frase.
    basis: str
    #: O que a faixa assume e ninguém verificou.
    caveat: str
    quality: str = "potential"


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
    #: Ordem de grandeza, quando o ativo tem custo atribuído. `None` quando não
    #: há base — e `None` é resposta melhor que zero, que se leria como "não há
    #: o que ganhar aqui".
    potential_range: PotentialRange | None = None

    def fingerprint(self, account: str) -> str:
        """Identidade estável entre execuções, para religar sinal e veredito.

        Mesma forma da de `Opportunity`, e pelo mesmo motivo: é o que permite a
        um julgamento feito hoje continuar valendo amanhã. A conta entra como
        parâmetro porque a regra que emitiu o sinal não conhece a conta — quem
        conhece é o scan.
        """
        raw = f"{account}|{self.asset_type}:{self.asset_name}|{self.rule_id}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"{raw}#{digest}"

    def evidence_signature(self) -> str:
        """O que precisa mudar para um sinal descartado merecer nova análise.

        Script com hash diferente, linhas diferentes, ou evidência que faltava e
        apareceu. Um descarte vale enquanto o que o sustentou continuar igual —
        a mesma disciplina que `Opportunity.evidence_signature` aplica a um
        achado dispensado.
        """
        payload = {
            "rule_id": self.rule_id,
            "artifact_sha256": self.artifact_sha256,
            "lines": sorted(self.lines),
            "missing_evidence": sorted(self.missing_evidence),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)
