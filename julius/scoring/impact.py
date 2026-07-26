"""Score de ganho (0–100), multidimensional e configurável.

No MVP 1A a dimensão dominante é **economia** (as demais dimensões — desempenho,
governança, alcance, tendência — entram nas fases seguintes com valores-base
modestos). Migrações/estratégicas recebem ganho alto por risco de não agir.
"""

from __future__ import annotations

from julius.config import Config

# Referência para normalizar a economia mensal (USD que mapeia perto do topo).
# No MVP 1A a única dimensão medida é ECONOMIA, então o score é dominado por ela;
# desempenho/governança/alcance/tendência entram (com os pesos de `config`) nas
# fases seguintes, quando houver dado para medi-las.
ECONOMIA_REF = 150.0


def gain_score(monthly_expected: float, config: Config, *, is_strategic: bool = False) -> int:
    if is_strategic:
        return 92  # alta prioridade estratégica (risco de manter em produção fora da Producer)
    economia = min(1.0, monthly_expected / ECONOMIA_REF)
    return round(economia * 100)
