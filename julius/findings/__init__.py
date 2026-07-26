"""Achados: o que uma regra viu, o que fazer, e quanto vale.

`Opportunity` compõe três coisas que antes eram sessenta campos soltos num
dataclass só — o achado, a recomendação e a evidência que sustenta ambos —
mais o ganho e os scores que a camada de pontuação preenche.

Duas funções são contrato congelado: `fingerprint()` e `evidence_signature()`.
O backlog e o histórico em DuckDB religam revisões humanas por elas, então
mudar a fórmula desconecta decisões já tomadas de seus achados.
"""

from julius.findings.opportunity import EstimatedGain, Estimation, Opportunity

__all__ = ["EstimatedGain", "Estimation", "Opportunity"]
