"""Parâmetros da própria coleta.

Não são premissas de domínio — não é preço nem limiar de regra. São o tamanho
da janela, a conversão para mês e a versão do dataset: coisas que descrevem
*como* a coleta mede, e por isso moram junto dela.
"""

from __future__ import annotations

# Janela de análise padrão, em dias UTC completos. É o período de tudo que é
# comparado entre serviços. O teto prático é o histórico de execuções do
# Athena, que a AWS retém por ~45 dias.
ANALYSIS_WINDOW_DAYS = 30

# Um mês médio tem 365,25/12 = 30,44 dias. A medição acontece na janela, em
# dias; quando um número precisa ser expresso "por mês" a conversão passa por
# aqui, uma vez só e com nome. Tratar 30 dias como mês subestima ~1,4%.
DAYS_PER_MONTH = 365.25 / 12

# Mínimo de dias observados para projetar o fechamento do mês. Abaixo disso o
# fator explode (no dia 2 seria ×15) e a projeção deixa de ser informação.
MIN_DAYS_FOR_FORECAST = 5

# Versão do dataset exportado. Sobe quando o significado de um campo muda, não
# só quando um campo é adicionado: um dataset da versão anterior mede
# mês-corrente, e esse número não pode ser reinterpretado como janela móvel.
DATASET_SCHEMA_VERSION = 2

# DPU por worker type do Glue (Glue 2.0+). É um fato da AWS sobre o recurso,
# não uma premissa de preço.
DPU_PER_WORKER: dict[str, float] = {
    "G.025X": 0.25,
    "G.1X": 1,
    "G.2X": 2,
    "G.4X": 4,
    "G.8X": 8,
    "G.12X": 12,
    "G.16X": 16,
    "R.1X": 1,
    "R.2X": 2,
    "R.4X": 4,
    "R.8X": 8,
    # Ray usa M-DPU; o valor representa unidades faturáveis por worker.
    "Z.2X": 2,
    "Standard": 1,
}
