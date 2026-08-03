"""Um terceiro perfil, para a conta em que nem a classe do objeto pode mudar.

A fronteira do Consumer é infraestrutura × objeto: configuração de bucket nunca é
recomendação, classe do objeto é — executada pelo time dono via `CopyObject`. Há
conta onde nem isso cabe: dado sob retenção contratual, bucket compartilhado com
outro time, política interna que trate qualquer reescrita como mudança.

O modo `evidence_only` já existia nas regras e nenhum perfil o produzia — só um
dataset editado à mão chegava lá. Código inalcançável não é neutro: ele confunde
quem lê as regras e apodrece sem ninguém notar, porque nenhum teste passa por ele
no caminho normal.

O que este arquivo cobra é a diferença entre os dois perfis Consumer, medida sobre
a **mesma conta**. Testar cada um isoladamente diria que ambos funcionam sem dizer
que eles diferem — e é a diferença que justifica o perfil existir.
"""

from __future__ import annotations

import pytest

from julius.collection.models import Account, S3Prefix, Table
from julius.collection.policy import (
    CONSUMER_DATAMESH,
    CONSUMER_EVIDENCE_ONLY,
    FULL_ANALYSIS,
    available_scope_profiles,
    policy_for_profile,
)
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules import REGISTRY, disabled_rule_ids

_GB = 1024**3


def _conta(perfil: str, **overrides) -> Account:
    politica = policy_for_profile(perfil)
    base = {
        "bucket": "lake",
        "prefix": "vendas/",
        "kind": "table_location",
        "source_asset": "db.vendas",
        "object_count": 5000,
        "total_bytes": 500 * _GB,
        "average_object_bytes": 100 * 1024 * 1024,
        "bytes_by_class": {"STANDARD": float(500 * _GB)},
        "object_count_by_class": {"STANDARD": 5000},
        "last_read_at": "2024-01-01",
        "read_coverage_days": 1000,
        "access_source": "persisted_touch_history",
        "access_quality": "measured",
    }
    return Account(
        account_id="123456789012",
        window_end="2026-07-29",
        scope_profile=politica.profile,
        s3_mode=politica.s3_mode,
        tables=[
            Table(
                name="db.vendas",
                location="s3://lake/vendas/",
                last_read_at="2024-01-01",
            )
        ],
        s3_prefixes=[S3Prefix(**{**base, **overrides})],
    )


def _s3(conta: Account) -> tuple[list, list]:
    achados = [
        item
        for familia in REGISTRY
        if familia.service == "s3"
        for item in familia.detect(conta, DEFAULT_CONFIG, "scan")
    ]
    sinais = [
        item
        for familia in REGISTRY
        if familia.service == "s3" and familia.signals
        for item in familia.signals(conta, DEFAULT_CONFIG)
    ]
    return achados, sinais


# ---------------------------------------------------------------------------
# O perfil existe e é alcançável
# ---------------------------------------------------------------------------


def test_the_mode_is_now_reachable_from_a_profile():
    """Era o problema: o modo existia nas regras e nenhum perfil o produzia."""
    modos = {
        nome: policy_for_profile(nome).s3_mode for nome in available_scope_profiles()
    }

    assert "evidence_only" in modos.values(), (
        "nenhum perfil produz evidence_only; o tratamento nas regras é inalcançável"
    )
    assert modos[CONSUMER_EVIDENCE_ONLY] == "evidence_only"


def test_every_s3_mode_the_rules_handle_has_a_profile():
    """A trava contra a regressão: tratamento sem perfil volta a ser código morto.

    A varredura é sobre os próprios módulos de regra — o que eles comparam contra
    `s3_mode` é o conjunto de modos que precisa existir.
    """
    import ast
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1] / "julius" / "knowledge"
    tratados: set[str] = set()
    for caminho in raiz.rglob("*.py"):
        for no in ast.walk(ast.parse(caminho.read_text(encoding="utf-8"))):
            if not isinstance(no, ast.Compare):
                continue
            alvo = ast.unparse(no.left)
            if "s3_mode" not in alvo:
                continue
            for comparado in no.comparators:
                if isinstance(comparado, ast.Constant) and isinstance(
                    comparado.value, str
                ):
                    tratados.add(comparado.value)

    produzidos = {
        policy_for_profile(nome).s3_mode for nome in available_scope_profiles()
    }
    orfaos = tratados - produzidos - {"proposal"}

    assert tratados, "a varredura não achou tratamento de s3_mode; o teste cegou"
    assert not orfaos, (
        f"modo tratado nas regras e produzido por nenhum perfil: {sorted(orfaos)}. "
        "Ou crie o perfil, ou remova o tratamento — código inalcançável apodrece."
    )


# ---------------------------------------------------------------------------
# A diferença entre os dois perfis Consumer, na mesma conta
# ---------------------------------------------------------------------------


def test_the_object_class_recommendation_exists_in_one_profile_and_not_the_other():
    """É a diferença inteira, e ela precisa aparecer sobre a mesma conta."""
    achados_padrao, _ = _s3(_conta(CONSUMER_DATAMESH))
    achados_estrito, sinais_estrito = _s3(_conta(CONSUMER_EVIDENCE_ONLY))

    assert any(
        item.rule_id == "S3-STORAGE-CLASS-TRANSITION" for item in achados_padrao
    ), "o perfil padrão do Consumer recomenda transição de classe por objeto"

    assert not achados_estrito, (
        "no perfil evidence_only nenhuma recomendação de S3 sobrevive"
    )
    assert sinais_estrito, "o achado vira pergunta, não desaparece"


def test_what_disappears_becomes_a_question_not_a_silence():
    """Sumir sem virar sinal se leria como "não há nada aqui"."""
    _, sinais = _s3(_conta(CONSUMER_EVIDENCE_ONLY))

    assert sinais
    for sinal in sinais:
        assert sinal.observation.strip(), "sinal sem observação não informa nada"
        assert sinal.question.strip(), "sinal sem pergunta não pede nada"
        # E a pergunta precisa apontar para o processo, não para o bucket.
        assert "S3" not in sinal.question or "sem alterar o S3" in sinal.question


def test_the_stricter_profile_disables_at_least_what_the_other_disables():
    """Um perfil mais restritivo nunca pode liberar o que o outro barra."""
    padrao = disabled_rule_ids(_conta(CONSUMER_DATAMESH))
    estrito = disabled_rule_ids(_conta(CONSUMER_EVIDENCE_ONLY))

    assert padrao < estrito, (
        f"o perfil estrito precisa desabilitar mais: {sorted(padrao - estrito)}"
    )
    assert "S3-STORAGE-CLASS-TRANSITION" in estrito
    assert "S3-STORAGE-CLASS-TRANSITION" not in padrao


def test_the_small_files_recommendation_survives_when_the_producer_is_known():
    """A exceção que prova a fronteira: a ação é sobre o código, não sobre o S3.

    Arquivo pequeno com processo produtor identificado continua virando
    recomendação mesmo no perfil estrito — porque quem muda é o job que escreve,
    não a configuração do bucket.
    """
    from julius.knowledge.rules.s3 import small_files

    conta = _conta(
        CONSUMER_EVIDENCE_ONLY,
        average_object_bytes=64 * 1024,
        object_count=200000,
    )
    conta.tables[0].written_by = "glue:etl-vendas"

    achados = small_files.detect(conta, DEFAULT_CONFIG, "scan")

    assert achados, (
        "com produtor conhecido a recomendação é sobre o código que escreve, e "
        "essa não é uma mudança de infraestrutura S3"
    )


# ---------------------------------------------------------------------------
# Contrato do perfil
# ---------------------------------------------------------------------------


def test_the_new_profile_keeps_every_other_capability():
    """A diferença é o S3. Coletar menos Glue ou Athena seria outro produto."""
    padrao = policy_for_profile(CONSUMER_DATAMESH)
    estrito = policy_for_profile(CONSUMER_EVIDENCE_ONLY)

    assert estrito.enabled_capabilities == padrao.enabled_capabilities


def test_the_full_profile_is_untouched():
    """A onda não pode mexer no perfil que já existia."""
    assert policy_for_profile(FULL_ANALYSIS).s3_mode == "proposal"
    assert policy_for_profile(CONSUMER_DATAMESH).s3_mode == "storage_class_only"


def test_an_unknown_profile_still_fails_loudly():
    with pytest.raises(ValueError, match="scope_profile inválido"):
        policy_for_profile("nao_existe")


@pytest.mark.parametrize("nome", available_scope_profiles())
def test_every_profile_declares_a_mode_the_rules_understand(nome):
    assert policy_for_profile(nome).s3_mode in {
        "proposal",
        "storage_class_only",
        "evidence_only",
    }
