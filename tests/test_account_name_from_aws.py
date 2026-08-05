"""O nome da conta vem da conta, e não do apelido que alguém deu ao perfil.

O nome lógico recorta o Glue Catalog. O último degrau da cascata era o apelido do
perfil SSO — escolha de quem rodou `aws configure sso`, sem relação com a conta —,
e numa máquina sem `~/.julius-accounts.json` era ele que decidia o escopo. O
sintoma é um `database_db_compartilhado_consumer_<apelido>` que não casa com nada.

A conta informa o nome certo por `account:GetContactInformation`.
`iam:ListAccountAliases` seria a API própria e não tocaria dado pessoal, mas
devolve `[]` na organização onde isto foi verificado.

O que estes testes prendem é a **precedência**: quem declarou à mão vence a API,
sempre. Uma API que sobrescreve o cadastro tira do operador a capacidade de
corrigir um nome que a AWS traz errado.
"""

from __future__ import annotations

import pytest

from julius.collection.collectors.account_name import collect_account_name
from julius.collection.health import CollectionRecorder
from julius.collection.orchestrator import _named_catalog_scope
from julius.collection.scope import CatalogScope

NOME_NA_AWS = "consumeratendimentodataservice-pro"


class _Account:
    """O cliente `account`, com só o que este caminho chama."""

    def __init__(self, payload=None, erro: Exception | None = None) -> None:
        self._payload = payload
        self._erro = erro
        self.chamadas = 0

    def get_contact_information(self, **_):
        self.chamadas += 1
        if self._erro is not None:
            raise self._erro
        return self._payload


class _Session:
    def __init__(self, cliente) -> None:
        self._cliente = cliente
        self.region_name = "sa-east-1"

    def client(self, service, **_):
        assert service == "account", f"serviço inesperado: {service}"
        return self._cliente


def _com(payload=None, erro=None):
    cliente = _Account(payload, erro)
    return _Session(cliente), cliente


def _completo() -> dict:
    return {
        "ContactInformation": {
            "FullName": NOME_NA_AWS,
            "AddressLine1": "Rua Exemplo, 1000",
            "PhoneNumber": "+55 11 99999-0000",
            "CompanyName": "Empresa Exemplo",
        }
    }


# --- o coletor -------------------------------------------------------------


def test_it_reads_only_the_name():
    sessao, _ = _com(_completo())

    assert collect_account_name(sessao) == NOME_NA_AWS


def test_nothing_but_the_name_leaves_the_collector():
    """Endereço, telefone e empresa não saem daqui — é a razão de o módulo existir."""
    sessao, _ = _com(_completo())
    devolvido = collect_account_name(sessao)

    for sensivel in ("Rua Exemplo", "99999-0000", "Empresa Exemplo"):
        assert sensivel not in devolvido


@pytest.mark.parametrize(
    "payload",
    [{}, {"ContactInformation": {}}, {"ContactInformation": {"FullName": "   "}}],
    ids=["sem-contato", "contato-vazio", "nome-em-branco"],
)
def test_an_empty_answer_is_an_empty_name(payload):
    sessao, _ = _com(payload)

    assert collect_account_name(sessao) == ""


# --- a precedência ---------------------------------------------------------


def _resolve(escopo: CatalogScope, sessao) -> tuple[CatalogScope, CollectionRecorder]:
    saude = CollectionRecorder()
    return _named_catalog_scope(sessao, saude, escopo), saude


def test_the_aws_name_beats_the_profile_nickname():
    """O caso que motivou tudo: sem cadastro, o apelido decidia o escopo."""
    sessao, _ = _com(_completo())
    escopo, _saude = _resolve(
        CatalogScope(account_name="meu-perfil", name_source="profile"), sessao
    )

    assert escopo.account_name == NOME_NA_AWS
    assert escopo.name_source == "aws"
    assert escopo.shared_database == (
        "database_db_compartilhado_consumer_atendimentodataservice"
    )


def test_the_registry_beats_the_aws_name():
    """Quem cadastrou à mão teve uma razão, e uma API não a sobrescreve."""
    sessao, _ = _com(_completo())
    escopo, _saude = _resolve(
        CatalogScope(account_name="consumer-avi", name_source="registry"), sessao
    )

    assert escopo.account_name == "consumer-avi"
    assert escopo.name_source == "registry"


def test_an_explicit_name_beats_everything():
    sessao, _ = _com(_completo())
    escopo, _saude = _resolve(
        CatalogScope(account_name="mandei-este", name_source="explicit"), sessao
    )

    assert escopo.account_name == "mandei-este"


def test_a_divergence_is_reported_without_changing_the_name():
    """Cadastro desatualizado envelheceria calado se ninguém comparasse."""
    sessao, _ = _com(_completo())
    escopo, saude = _resolve(
        CatalogScope(account_name="consumer-avi", name_source="registry"), sessao
    )
    linha = saude.entries[-1]

    assert escopo.account_name == "consumer-avi"
    assert linha.status == "partial"
    assert linha.error_category == "name_mismatch"
    assert NOME_NA_AWS in linha.impact and "consumer-avi" in linha.impact


def test_an_agreeing_registry_raises_no_warning():
    sessao, _ = _com({"ContactInformation": {"FullName": "consumer-avi"}})
    _escopo, saude = _resolve(
        CatalogScope(account_name="consumer-avi", name_source="registry"), sessao
    )

    assert saude.entries[-1].error_category != "name_mismatch"


# --- degradação ------------------------------------------------------------


def test_a_denied_permission_keeps_the_scan_going():
    """Nome de banco errado degrada o escopo; não interrompe trinta e nove fontes."""
    sessao, _ = _com(erro=RuntimeError("AccessDeniedException"))
    escopo, saude = _resolve(
        CatalogScope(account_name="meu-perfil", name_source="profile"), sessao
    )

    assert escopo.account_name == "meu-perfil"
    assert saude.entries[-1].status != "ok"
    assert "account:GetContactInformation" in saude.entries[-1].next_action


def test_a_failure_never_fails_the_collection():
    sessao, _ = _com(erro=RuntimeError("boom"))

    _resolve(CatalogScope(account_name="x", name_source="profile"), sessao)


def test_not_knowing_the_name_does_not_degrade_the_scan():
    """Não saber o nome estreita o recorte do catálogo; não estraga medição.

    `CollectionRecorder.capture` assume o contrário para fonte não obrigatória, e
    a primeira versão disto rebaixava o scan inteiro para `partial`. Quem reporta
    a consequência é `Glue Catalog Scope`, que também não degrada — as duas
    precisam dizer a mesma coisa sobre a mesma coisa.
    """
    sessao, _ = _com(erro=RuntimeError("AccessDeniedException"))
    _escopo, saude = _resolve(
        CatalogScope(account_name="meu-perfil", name_source="profile"), sessao
    )

    assert saude.entries[-1].affects_status is False


def test_the_divergence_warning_does_not_degrade_the_scan_either():
    sessao, _ = _com(_completo())
    _escopo, saude = _resolve(
        CatalogScope(account_name="consumer-avi", name_source="registry"), sessao
    )

    assert saude.entries[-1].status == "partial"
    assert saude.entries[-1].affects_status is False


def test_an_empty_name_leaves_the_previous_one_alone():
    sessao, _ = _com({"ContactInformation": {"FullName": ""}})
    escopo, _saude = _resolve(
        CatalogScope(account_name="meu-perfil", name_source="profile"), sessao
    )

    assert escopo.account_name == "meu-perfil"


def test_an_explicit_database_list_skips_the_call():
    """A lista substitui a regra de nome: pedir dado de contato para descartá-lo
    seria gratuito, e o dado é pessoal."""
    sessao, cliente = _com(_completo())
    escopo, saude = _resolve(
        CatalogScope(databases=("banco1", "banco2"), name_source="profile"), sessao
    )

    assert cliente.chamadas == 0
    assert escopo.databases == ("banco1", "banco2")
    assert not saude.entries


# --- a cascata offline -----------------------------------------------------


def test_the_cascade_reports_where_the_name_came_from(tmp_path):
    import json

    from julius.collection.targets import (
        resolve_account_name,
        resolve_account_name_source,
    )

    cadastro = tmp_path / "contas.json"
    cadastro.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "accounts": [
                    {
                        "name": "consumer-avi",
                        "expected_account_id": "123456789012",
                        "sso_profile": "perfil-avi",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    for kwargs, nome, origem in (
        ({"explicit_name": "mandei"}, "mandei", "explicit"),
        ({"sso_profile": "perfil-avi"}, "consumer-avi", "registry"),
        ({"sso_profile": "sem-cadastro"}, "sem-cadastro", "profile"),
    ):
        argumentos = {"config_path": cadastro, **kwargs}
        assert resolve_account_name(**argumentos) == nome
        assert resolve_account_name_source(**argumentos) == origem
