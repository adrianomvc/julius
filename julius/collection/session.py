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

CLIENT_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=10,
    read_timeout=60,
    # Precisa acompanhar `S3_LISTING_WORKERS`: com o pool menor que o número de
    # threads, elas disputam conexão e o paralelismo vira fila — o urllib3 ainda
    # avisa "Connection pool is full" a cada requisição. A folga é para o
    # cliente compartilhado atender listagem e métrica ao mesmo tempo.
    max_pool_connections=max(25, S3_LISTING_WORKERS * 2),
)


def make_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    """Sessão via cadeia de credenciais SSO do AWS CLI."""
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def make_client(session, service: str):
    """Cliente do serviço com a configuração de retry e timeout da coleta."""
    return session.client(service, config=CLIENT_CONFIG)
