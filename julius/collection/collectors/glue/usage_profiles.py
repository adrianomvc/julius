"""Coleta read-only dos Usage Profiles do Glue.

Perfil de uso é guardrail: ele restringe o que um job ou sessão pode pedir —
worker type, número de workers, timeout — antes de a execução começar. É
prevenção, e prevenção não tem economia medida: nada foi desperdiçado ainda.

Por isso esta coleta **não alimenta regra nenhuma**. Ela alimenta o relatório com
o estado: quais perfis existem e o que cada um declara. O Julius diz o que vê;
decidir implantar guardrail é do time de plataforma, e transformar isso em
recomendação com cifra exigiria inventar o desperdício que o perfil evitou.

A conta que não usa o recurso devolve lista vazia, e isso é informação — não
lacuna. A fonte não degrada a coleta por isso.
"""

from __future__ import annotations

from typing import Any

from julius.collection.models import GlueUsageProfile


def _limites(configuration: Any) -> dict[str, str]:
    """Os limites declarados, como texto e por chave.

    Cada bloco de configuração (`SessionConfiguration`, `JobConfiguration`) traz
    parâmetros de tipos diferentes. O relatório mostra o limite **como declarado**;
    interpretar aqui seria decidir por quem lê.
    """
    saida: dict[str, str] = {}
    if not isinstance(configuration, dict):
        return saida
    for bloco, parametros in configuration.items():
        if not isinstance(parametros, dict):
            continue
        for chave, valor in parametros.items():
            if not isinstance(valor, dict):
                continue
            declarado = valor.get("DefaultValue") or valor.get("AllowedValues")
            if declarado is None:
                continue
            if isinstance(declarado, list):
                declarado = ", ".join(str(item) for item in declarado)
            saida[f"{bloco}.{chave}"] = str(declarado)
    return saida


def collect_usage_profiles(glue_client) -> list[GlueUsageProfile]:
    perfis: list[GlueUsageProfile] = []
    paginator = glue_client.get_paginator("list_usage_profiles")
    for page in paginator.paginate():
        for raw in page.get("Profiles", []) or []:
            nome = str(raw.get("Name") or "")
            if not nome:
                continue
            detalhe = glue_client.get_usage_profile(Name=nome) or {}
            perfis.append(
                GlueUsageProfile(
                    name=nome,
                    description=str(
                        detalhe.get("Description") or raw.get("Description") or ""
                    ),
                    limits=_limites(detalhe.get("Configuration")),
                )
            )
    return sorted(perfis, key=lambda item: item.name)
