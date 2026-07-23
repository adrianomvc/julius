"""Governança de contas Consumer (MVP 2 em diante). No MVP 1A: só a
recomendação Producer sobre scores fornecidos, para o relatório."""

from julius.governance.producer import (
    ProducerRecommendation,
    compute_candidates,
    recommend,
)

__all__ = ["ProducerRecommendation", "compute_candidates", "recommend"]
