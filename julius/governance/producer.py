"""Governança Producer determinística.

Calcula, por produto de dados (tabela), dois scores independentes a partir de
sinais do inventário — substituindo a ingestão manual:
- **Candidatura**: o processo assumiu perfil de produção? (consumo por múltiplas
  comunidades, toques recorrentes, persistência, publicação via DataWarm)
- **Prontidão**: é viável migrar agora? (owner e linhagem conhecidos, não temporária,
  consumidores mapeados)

E recomenda pela matriz 2×2. Nunca recomenda migrar a conta inteira — só o produto.
"""

from __future__ import annotations

from dataclasses import dataclass

from julius.collection.models import Account, ProducerCandidate, Table

_CAND_HIGH = 60
_READY_HIGH = 55


@dataclass(frozen=True)
class ProducerRecommendation:
    label: str
    color: str
    note: str


def recommend(candidate: int, readiness: int) -> ProducerRecommendation:
    if candidate >= _CAND_HIGH and readiness >= _READY_HIGH:
        return ProducerRecommendation(
            "Migrar", "#0053b3", "alta candidatura e prontidão — priorizar a migração do processo."
        )
    if candidate >= _CAND_HIGH and readiness < _READY_HIGH:
        return ProducerRecommendation(
            "Preparar", "#8a6a2a", "perfil de produção sem prontidão — montar plano de preparação."
        )
    if candidate < _CAND_HIGH and readiness >= _READY_HIGH:
        return ProducerRecommendation(
            "Monitorar", "#e8730c", "viável mas ainda não é produção — manter e observar evolução."
        )
    return ProducerRecommendation(
        "Não priorizar", "#9a9a90", "sem perfil de produção nem prontidão."
    )


def _clamp(v: float) -> int:
    return int(max(0, min(100, round(v))))


def candidate_score(table: Table, recurring_writer: bool) -> int:
    """Assumiu perfil de produção? (0–100, determinístico)."""
    communities = {0: 0, 1: 10, 2: 25}.get(table.consuming_communities, 40)  # 3+ = 40
    touches = min(25, table.touches_90d / 40)          # satura em 1000 toques/90d
    persistence = 20 if recurring_writer else 0
    publication = 15 if table.datawarm_published else 0
    return _clamp(communities + touches + persistence + publication)


def readiness_score(table: Table, recurring_writer: bool) -> int:
    """É viável migrar agora? (0–100, determinístico)."""
    owner = 30 if table.owner_tag else 0
    lineage = 25 if table.written_by else 0
    not_temp = 0 if table.temporary else 15
    consumers = 15 if table.consuming_accounts > 0 else 0
    stable = 15  # schema estável assumido quando não há sinal em contrário
    return _clamp(owner + lineage + not_temp + consumers + stable)


def compute_candidates(account: Account) -> list[ProducerCandidate]:
    """Deriva candidatos a Producer das tabelas com sinal de produção.

    Considera apenas tabelas não-temporárias que são de fato consumidas (toques>0)
    ou publicadas via DataWarm — o "sem uso" é caso dos detectores de dados.
    """
    out: list[ProducerCandidate] = []
    writers = {j.name: j for j in account.glue_jobs}
    for t in account.tables:
        if t.temporary:
            continue
        if t.touches_90d <= 0 and not t.datawarm_published:
            continue
        w = writers.get(t.written_by or "")
        recurring = bool(w and w.runs_per_month >= 4)
        out.append(
            ProducerCandidate(
                name=t.name,
                candidate_score=candidate_score(t, recurring),
                readiness_score=readiness_score(t, recurring),
            )
        )
    return out
