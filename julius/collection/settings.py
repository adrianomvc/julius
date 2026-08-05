"""Parâmetros da própria coleta.

Não são premissas de domínio — não é preço nem limiar de regra. São o tamanho
da janela, a conversão para mês e a versão do dataset: coisas que descrevem
*como* a coleta mede, e por isso moram junto dela.
"""

from __future__ import annotations

# Janela de análise padrão, em dias UTC completos. É o período de tudo que é
# comparado entre serviços. O teto prático é o histórico de execuções do
# Athena, que a AWS retém por 45 dias.
ANALYSIS_WINDOW_DAYS = 30

# Profundidade pedida na primeira coleta de uma conta. Não é premissa de
# retenção: é escolha de produto. Várias regras só produzem cifra com
# maturidade — três coletas consistentes ou 90 dias de cobertura — e sem isso uma
# conta nova espera de um a três meses de coletas semanais para o portfólio ter
# número, mesmo quando a AWS já retém o histórico hoje.
BOOTSTRAP_WINDOW_DAYS = 90

# Onde a AWS retém **menos** que a janela pedida. Família fora deste mapa não
# tem limite documentado abaixo de `BOOTSTRAP_WINDOW_DAYS`.
#
# Por que o teto importa: pedir mais dias do que o serviço retém não devolve
# erro, devolve menos dado — e `coverage_days` dos modelos é preenchido com
# `window.days`, a janela *pedida*. Sem o teto, o dataset afirmaria a cobertura
# maior e inflaria em silêncio todos os gates de regra que leem cobertura.
RETENTION_CEILING_DAYS: dict[str, int] = {
    # A documentação da AWS se contradiz: a referência de `GetJobRun` diz 365
    # dias, enquanto a visão geral da API de Job Runs e a do console dizem 90.
    # Fica em 90 — pedir 365 contra um limite real de 90 produz exatamente a
    # cobertura fantasma descrita acima. Explícito mesmo coincidindo com
    # `BOOTSTRAP_WINDOW_DAYS`, para a contradição ficar registrada.
    # https://docs.aws.amazon.com/glue/latest/dg/aws-glue-api-jobs-runs.html
    "glue": 90,
    # "Athena retains query history for 45 days" — e existe página dedicada a
    # contornar isso exportando o histórico, o que confirma o limite.
    # https://docs.aws.amazon.com/athena/latest/ug/querying-keeping-query-history.html
    "athena": 45,
    # Standard workflows retêm 90 dias, mas é **quota reduzível a 30** por conta
    # e região, e não há como descobrir qual vale sem pedir o valor da quota.
    # Fica no piso: regra bloqueada por falta de cobertura é recuperável na
    # coleta seguinte, cobertura inflada corrompe a cifra sem avisar. Suba para
    # 90 se a conta confirmou que mantém a quota padrão.
    # https://docs.aws.amazon.com/step-functions/latest/dg/service-quotas.html
    "stepfunctions": 30,
    # Sem entrada para s3, sagemaker, redshift e billing: a telemetria deles vem
    # do CloudWatch com período diário, disponível por 455 dias (o corte de 63
    # dias vale para período de 5 minutos, não para o diário), e o Cost Explorer
    # tem período próprio em `BillingMonth`.
    # https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch_concepts.html
}


def retention_ceiling(family: str) -> int:
    """Dias que a família consegue devolver, no máximo."""
    return RETENTION_CEILING_DAYS.get(family, BOOTSTRAP_WINDOW_DAYS)

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
#
# 3 — a cobrança passou a ser o mês anterior fechado por padrão, e o campo
# mudou de nome junto: `billing_cost_mtd` virou `billing_cost_period`. É o caso
# que o parágrafo acima já antecipava, agora acontecido. Um dataset da versão 2
# mede mês-corrente e não pode ser somado nem comparado com um da 3.
DATASET_SCHEMA_VERSION = 3

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
