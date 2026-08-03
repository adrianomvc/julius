"""Sessão boto3 read-only via perfis AWS CLI SSO."""

from __future__ import annotations

import boto3
from botocore.config import Config

#: Configuração de todo cliente da coleta.
#:
#: O default do botocore é o modo `legacy` de retry: cinco tentativas com
#: backoff fixo e nenhuma noção de que *nós* somos a causa do throttling. Ao
#: varrer um catálogo de Data Mesh ou pedir métrica de trezentos jobs, isso vira
#: lentidão composta — a resposta ao throttle é insistir no ritmo que o
#: provocou. O modo `adaptive` mantém um limitador do lado do cliente e recua
#: sozinho.
#:
#: `connect_timeout` curto é o que evita uma coleta parada por um minuto num
#: endpoint inalcançável; o de leitura continua largo porque Cost Explorer e
#: Athena respondem devagar por natureza.
#: Prefixos listados ao mesmo tempo quando a listagem completa está ligada. O
#: gargalo é latência de rede, não CPU, e o limitador adaptativo do botocore
#: segura o ritmo se o S3 reclamar. Conservador de propósito: o teto útil é o
#: `max_pool_connections`, e passar dele só troca espera por espera.
S3_LISTING_WORKERS = 8

#: Jobs cujo histórico de execuções é paginado ao mesmo tempo. `GetJobRuns` é uma
#: chamada por job, e numa conta com centenas de jobs a série domina o scan
#: inteiro. Mesma natureza do caso do S3 — latência, não CPU — e mesmo teto útil.
GLUE_RUN_HISTORY_WORKERS = 8

#: Máquinas Step Functions analisadas ao mesmo tempo. Cada alvo faz chamadas
#: independentes de Describe/List/GetExecutionHistory; o limite evita que uma
#: conta com muitas máquinas transforme latência de rede em uma fila serial.
STEP_FUNCTIONS_WORKERS = 8

#: Lotes de até 50 execuções Athena consultados ao mesmo tempo. Cinquenta é o
#: limite de BatchGetQueryExecution; o número abaixo limita quantos lotes ficam
#: em voo, sem alterar cobertura nem a ordem determinística do resultado.
ATHENA_QUERY_BATCH_WORKERS = 4

#: Descrições de jobs SageMaker em voo. As APIs List devolvem apenas resumo;
#: configuração de instância, Spot e checkpoint exigem um Describe por job.
SAGEMAKER_DETAIL_WORKERS = 8

#: Fontes independentes em voo no DAG. Cada fonte pode ter concorrência interna,
#: portanto este teto é menor que a soma dos pools dos coletores.
SOURCE_WORKERS = 4

#: O maior grupo de threads que um cliente atende de uma vez. As fontes rodam em
#: série, então nunca são os dois grupos somados.
_MAX_CONCURRENT_WORKERS = max(
    S3_LISTING_WORKERS,
    GLUE_RUN_HISTORY_WORKERS,
    STEP_FUNCTIONS_WORKERS,
    ATHENA_QUERY_BATCH_WORKERS,
    SAGEMAKER_DETAIL_WORKERS,
)

CLIENT_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=10,
    read_timeout=60,
    # Precisa acompanhar o maior grupo de workers: com o pool menor que o número
    # de threads, elas disputam conexão e o paralelismo vira fila — o urllib3
    # ainda avisa "Connection pool is full" a cada requisição. A folga é para o
    # cliente compartilhado atender listagem e métrica ao mesmo tempo.
    max_pool_connections=max(25, _MAX_CONCURRENT_WORKERS * 2),
)


def make_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    """Sessão via cadeia de credenciais SSO do AWS CLI."""
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def make_client(session, service: str):
    """Cliente do serviço com a configuração de retry e timeout da coleta."""
    return session.client(service, config=CLIENT_CONFIG)
