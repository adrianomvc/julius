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
CLIENT_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=10,
    read_timeout=60,
    # Folga para quando um coletor paralelizar internamente; em série, o custo
    # de reservar isto é zero.
    max_pool_connections=25,
)


def make_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    """Sessão via cadeia de credenciais SSO do AWS CLI."""
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def make_client(session, service: str):
    """Cliente do serviço com a configuração de retry e timeout da coleta."""
    return session.client(service, config=CLIENT_CONFIG)
