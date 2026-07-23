"""Modelos financeiros por detector + projeção temporal.

Cada função devolve uma tupla `(Estimation, EstimatedGain)` já preenchida.
Ganho = custo_atual_normalizado − custo_projetado. A projeção temporal separa
potencial mensal/anual do **ganho realizável no ano** (considera data provável
de implementação e fator de realização).
"""

from julius.estimation.project import build_gain, months_remaining_in_year

__all__ = ["build_gain", "months_remaining_in_year"]
