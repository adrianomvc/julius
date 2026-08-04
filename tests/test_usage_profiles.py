"""Usage Profiles: guardrail é estado, não recomendação.

Perfil de uso restringe o que um job pode pedir — worker type, número de workers,
timeout — antes de a execução começar. É prevenção, e prevenção não tem economia
medida: nada foi desperdiçado ainda.

Por isso esta coleta **não alimenta regra nenhuma**. O Julius diz o que vê;
transformar isso em recomendação com cifra exigiria inventar o desperdício que o
perfil evitou, e é exatamente o tipo de número que este produto não produz.

A conta sem perfil devolve lista vazia, e isso é resposta — não lacuna.
"""

from __future__ import annotations

from julius.collection.collectors.glue.usage_profiles import collect_usage_profiles
from julius.collection.models import Account, GlueUsageProfile
from julius.knowledge.remediation import CATALOG


class _Glue:
    """O cliente Glue, com só o que este coletor chama."""

    def __init__(self, perfis, detalhes=None) -> None:
        self._perfis = perfis
        self._detalhes = detalhes or {}
        self.detalhes_pedidos: list[str] = []

    def get_paginator(self, nome):
        assert nome == "list_usage_profiles"
        perfis = self._perfis

        class _P:
            def paginate(self, **_):
                return [{"Profiles": perfis}]

        return _P()

    def get_usage_profile(self, Name):  # noqa: N803 — assinatura do boto3
        self.detalhes_pedidos.append(Name)
        return self._detalhes.get(Name, {})


def test_an_account_without_profiles_answers_empty():
    """Zero aqui é resposta — "esta conta não tem guardrail" — e não lacuna."""
    assert collect_usage_profiles(_Glue([])) == []


def test_it_reads_the_declared_limits():
    cliente = _Glue(
        [{"Name": "restrito"}],
        {
            "restrito": {
                "Description": "limita capacidade em não-produção",
                "Configuration": {
                    "JobConfiguration": {
                        "--number-of-workers": {"DefaultValue": "5"},
                        "--worker-type": {"AllowedValues": ["G.1X", "G.2X"]},
                    }
                },
            }
        },
    )
    perfil = collect_usage_profiles(cliente)[0]

    assert perfil.name == "restrito"
    assert perfil.description == "limita capacidade em não-produção"
    assert perfil.limits["JobConfiguration.--number-of-workers"] == "5"
    assert perfil.limits["JobConfiguration.--worker-type"] == "G.1X, G.2X"


def test_a_parameter_without_a_declared_value_is_skipped():
    """Parâmetro sem limite declarado não restringe nada; mostrá-lo sugeriria
    guardrail onde não há."""
    cliente = _Glue(
        [{"Name": "vazio"}],
        {"vazio": {"Configuration": {"JobConfiguration": {"--x": {}}}}},
    )

    assert collect_usage_profiles(cliente)[0].limits == {}


def test_a_profile_without_a_name_is_skipped():
    assert collect_usage_profiles(_Glue([{"Description": "sem nome"}])) == []


def test_the_order_is_stable():
    cliente = _Glue([{"Name": "zeta"}, {"Name": "alfa"}])

    assert [p.name for p in collect_usage_profiles(cliente)] == ["alfa", "zeta"]


def test_it_never_becomes_a_rule():
    """Nenhum `rule_id` de usage profile existe, e é decisão — não esquecimento."""
    assert not [r for r in CATALOG if "USAGE-PROFILE" in r or "CUSTOM-PROFILE" in r]


def test_the_profiles_reach_the_report():
    """Campo coletado sem leitor é chamada de API paga para produzir nada."""
    from julius.reporting.view_models import build
    from julius.state.audit import build_manifest

    conta = Account(
        account_id="123456789012",
        glue_usage_profiles=[
            GlueUsageProfile(
                name="restrito",
                description="limita capacidade",
                limits={"JobConfiguration.--number-of-workers": "5"},
            )
        ],
    )
    from julius.config import DEFAULT_CONFIG

    vm = build(conta, [], build_manifest(conta, DEFAULT_CONFIG, "scan", source="teste"))

    assert vm.guardrails == [
        {
            "kind": "glue_usage_profile",
            "name": "restrito",
            "description": "limita capacidade",
            "limits": {"JobConfiguration.--number-of-workers": "5"},
            "sessions_using": 0,
        }
    ]


def test_a_profile_nobody_uses_is_visible_as_such():
    """Perfil que existe e ninguém usa não restringe nada. A contagem separa
    "temos guardrail" de "o guardrail está valendo"."""
    from julius.collection.models.glue import InteractiveSession
    from julius.config import DEFAULT_CONFIG
    from julius.reporting.view_models import build
    from julius.state.audit import build_manifest

    conta = Account(
        account_id="123456789012",
        glue_usage_profiles=[GlueUsageProfile(name="restrito")],
        interactive_sessions=[
            InteractiveSession(session_id="a", profile_name="restrito"),
            InteractiveSession(session_id="b"),
            InteractiveSession(session_id="c"),
        ],
    )
    vm = build(conta, [], build_manifest(conta, DEFAULT_CONFIG, "scan", source="teste"))
    por_tipo = {item["kind"]: item for item in vm.guardrails}

    assert por_tipo["glue_usage_profile"]["sessions_using"] == 1
    assert por_tipo["sessions_without_profile"]["sessions_using"] == 2


def test_the_json_publishes_them_outside_the_opportunities():
    """Guardrail fora de `opportunities` porque não é oportunidade: não tem
    economia a somar e não disputa posição no ranking."""
    import json

    from julius.pipeline import analyze
    from julius.reporting.renderer import render_json

    analise = analyze("data/sample/consumer-avi.json")
    payload = json.loads(render_json(analise.vm, analise.opportunities))

    assert "guardrails" in payload
    assert "guardrails" not in payload["summary"]
