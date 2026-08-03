"""A camada de IA recomenda e nunca executa — e isso precisa ser cobrado.

`tests/test_read_only.py` já garante a base: nenhuma operação AWS fora da
allowlist sai do pacote `julius`. Só que a allowlist protege **uma** camada. A
partir do momento em que existe um provedor que devolve texto livre, uma Skill
que o host lê como instrução e um relatório que mistura o que foi proposto com o
que foi feito, há três superfícies novas em que "o Julius não altera a conta"
pode deixar de ser verdade sem nenhuma chamada boto3 nova aparecer.

Este arquivo cobre essas superfícies:

- o que a IA consegue pedir ao motor é um conjunto fechado, e nada nele é uma
  operação AWS;
- o conteúdo das Skills não carrega comando executável;
- os provedores não têm como falar com a AWS nem com a rede;
- recomendação é texto, não ação;
- estimativa contextual não entra sozinha no portfólio;
- o relatório separa proposto de realizado;
- no perfil Consumer, nenhuma recomendação toca a infraestrutura do bucket.
"""

from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PACOTE = RAIZ / "julius"

#: Módulos que leem a saída da IA e a transformam em algo que o motor usa. Se
#: algum deles ganhar acesso à AWS, a fronteira deixa de existir no ponto exato
#: em que o texto de um modelo vira ação.
_CONSOMEM_SAIDA_DA_IA = (
    "analysis/response_validator.py",
    "analysis/rule_candidates.py",
    "knowledge/contextual_estimation.py",
    "knowledge/verdict_facts.py",
)

_REDE_OU_AWS = re.compile(
    r"^(boto3|botocore|requests|httpx|urllib3|socket|ftplib|"
    r"telnetlib|smtplib|paramiko|subprocess)\b"
    r"|^urllib\.request\b|^http\.client\b"
)

#: Analisar uma URL não é buscá-la. `urllib.parse` é o que sustenta a exigência
#: de documentação em `docs.aws.amazon.com` — puro tratamento de string, sem
#: nenhuma ida à rede. Barrá-lo mediria o nome do pacote, não a capacidade.
_PARSING_PURO = ("urllib.parse",)


def _modulos_importados(caminho: Path) -> set[str]:
    """Caminho completo de cada módulo importado, direto ou por `from`.

    O caminho inteiro importa: `urllib.parse` e `urllib.request` compartilham o
    topo e não compartilham capacidade nenhuma.
    """
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    nomes: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes.update(alias.name for alias in no.names)
        elif isinstance(no, ast.ImportFrom) and no.module and no.level == 0:
            nomes.add(no.module)
    return {nome for nome in nomes if nome not in _PARSING_PURO}


def test_generative_ai_cannot_request_mutation():
    """O que a IA pede ao motor é cenário, e cenário não é chamada de API.

    `_ALLOWED` é a única alavanca estruturada que a saída da IA tem sobre o
    cálculo. Um nome de método que coincidisse com uma operação AWS — ou que
    tivesse forma de verbo de mutação — seria a porta pela qual "propor" viraria
    "executar".
    """
    from julius.knowledge.contextual_estimation import allowed_methods
    from tests.test_read_only import _VERBOS_DE_MUTACAO, OPERACOES_PERMITIDAS

    metodos = set(allowed_methods().values())
    assert metodos, "sem métodos não há o que proteger; o teste cegou"

    for metodo in sorted(metodos):
        assert re.fullmatch(r"[a-z][a-z0-9_]*_v\d+", metodo), (
            f"método de estimativa com forma inesperada: {metodo!r}. "
            "O sufixo de versão é o que o distingue de um nome de operação."
        )
        assert metodo not in OPERACOES_PERMITIDAS, (
            f"método de estimativa colide com operação AWS: {metodo}"
        )
        assert not _VERBOS_DE_MUTACAO.match(metodo), (
            f"método de estimativa com verbo de mutação: {metodo}"
        )


def test_the_modules_that_read_ai_output_cannot_reach_aws():
    """Entre o texto do modelo e a conta não pode existir caminho curto."""
    ofensores = {}
    for relativo in _CONSOMEM_SAIDA_DA_IA:
        caminho = PACOTE / relativo
        assert caminho.is_file(), f"módulo esperado não existe: {relativo}"
        alcancaveis = sorted(
            nome for nome in _modulos_importados(caminho) if _REDE_OU_AWS.match(nome)
        )
        if alcancaveis:
            ofensores[relativo] = alcancaveis

    assert not ofensores, (
        f"módulo que consome saída da IA importa AWS ou rede: {ofensores}"
    )


def test_provider_has_no_mutation_capability():
    """Um provedor escreve e lê arquivo. Não fala com a AWS, nem com a rede.

    O contrato em `providers/base.py` já diz que nenhuma integração é por rede —
    *"tudo que entrou e tudo que saiu está em disco"*. Aqui isso deixa de ser
    docstring.
    """
    provedores = sorted((PACOTE / "analysis" / "providers").glob("*.py"))
    assert len(provedores) >= 3, "esperados base, devin e manual_file"

    ofensores = {}
    for caminho in provedores:
        alcancaveis = sorted(
            nome for nome in _modulos_importados(caminho) if _REDE_OU_AWS.match(nome)
        )
        if alcancaveis:
            ofensores[caminho.name] = alcancaveis

    assert not ofensores, f"provedor com capacidade de mutação ou rede: {ofensores}"


def test_skill_content_cannot_execute_changes():
    """Skill é conhecimento. O que ela mostra em bloco de código é copiável.

    A prosa da Skill precisa poder **proibir** mutação, e proíbe: ela diz
    "never run create, update, put, delete". Um teste que buscasse a palavra
    solta acusaria justamente a frase que protege. Por isso a varredura é só
    sobre blocos de código cercados — que é o que alguém copia e roda.
    """
    comando_de_mutacao = re.compile(
        r"(?im)^\s*(?:\$\s*)?(?:"
        r"aws\s+\w[\w-]*\s+(?:create|update|put|delete|modify|start|stop|"
        r"terminate|attach|detach|tag|untag|enable|disable)-"
        r"|(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\s"
        r"|terraform\s+apply"
        r"|kubectl\s+(?:apply|delete)"
        r"|(?:sam|serverless|cdk)\s+deploy"
        r")"
    )
    escrita_boto3 = re.compile(
        r"\.\s*(?:put|create|update|delete|modify|start|stop|terminate|attach|"
        r"detach|tag|untag|enable|disable)_\w+\s*\("
    )

    arquivos = sorted((RAIZ / ".agents").rglob("*.md"))
    assert arquivos, "nenhuma Skill encontrada; o teste cegou"

    ofensores = []
    for caminho in arquivos:
        texto = caminho.read_text(encoding="utf-8")
        for bloco in re.findall(r"```[a-zA-Z]*\n(.*?)```", texto, re.S):
            achado = comando_de_mutacao.search(bloco) or escrita_boto3.search(bloco)
            if achado:
                ofensores.append(
                    f"{caminho.relative_to(RAIZ).as_posix()}: {achado.group(0)!r}"
                )

    assert not ofensores, (
        "bloco de código executável com mutação dentro de uma Skill:\n"
        + "\n".join(ofensores)
        + "\nSkill carrega conhecimento e orientação; executar é ação do time dono."
    )


def test_recommendation_does_not_imply_execution():
    """Recomendação é texto e lista de texto — nunca algo invocável.

    Um campo que carregasse callable, comando ou payload transformaria a
    recomendação em coisa executável por quem a lê. A forma do contrato é o que
    impede isso, e ela precisa ser cobrada.
    """
    from julius.analysis.response_validator import ContextualRecommendation
    from julius.findings.investigation import AIRecommendation

    permitidos = {
        "str",
        "list[str]",
        "list[DocumentationReference]",
    }
    for classe in (AIRecommendation, ContextualRecommendation):
        for campo in fields(classe):
            tipo = (
                campo.type if isinstance(campo.type, str) else getattr(campo.type, "__name__", "")
            )
            assert tipo in permitidos, (
                f"{classe.__name__}.{campo.name} é {tipo!r}; recomendação só pode "
                "carregar texto — o que executa é o time dono, não o dado"
            )


def test_human_approval_required_for_changes():
    """Estimativa contextual não entra no portfólio por conta própria.

    `include_in_portfolio` nasce `False`, e nenhum caminho do pacote o liga. A
    promoção depende de decisão humana registrada, que ainda não existe em
    código — e enquanto não existir, o padrão precisa continuar sendo o não.
    """
    from julius.findings.investigation import ContextualEstimate

    assert ContextualEstimate(method="x", status="estimated").include_in_portfolio is False

    ligam = []
    padrao = re.compile(r"include_in_portfolio\s*=\s*True")
    for caminho in PACOTE.rglob("*.py"):
        if padrao.search(caminho.read_text(encoding="utf-8")):
            ligam.append(caminho.relative_to(PACOTE).as_posix())

    assert not ligam, (
        f"estimativa contextual entrando no portfólio sem aprovação humana: {ligam}"
    )


def test_reports_distinguish_proposed_from_implemented():
    """Proposto e realizado não podem ocupar o mesmo campo.

    A presença de uma recomendação não significa execução; a de uma estimativa
    não significa economia realizada. No relatório isso é estrutura: o que a IA
    propôs vive em `ai_*`, o que foi de fato observado depois vive em
    `previous_results` e `realized_fmt`.
    """
    from julius.reporting.view_models import ReportViewModel

    nomes = {campo.name for campo in fields(ReportViewModel)}

    propostos = {"ai_recommendations", "ai_implementation_order", "ai_summary"}
    realizados = {"previous_results", "realized_fmt", "committed_fmt"}

    assert propostos <= nomes, f"campos de proposta ausentes: {propostos - nomes}"
    assert realizados <= nomes, f"campos de resultado ausentes: {realizados - nomes}"
    assert not (propostos & realizados), "proposto e realizado no mesmo campo"

    # O anexo da análise contextual não pode escrever no lado do que aconteceu.
    anexo = (PACOTE / "reporting" / "contextual.py").read_text(encoding="utf-8")
    for campo in sorted(realizados):
        assert f"vm.{campo} =" not in anexo, (
            f"a análise contextual escreve em {campo}, que é o lado do realizado"
        )


def test_s3_consumer_mode_never_recommends_infrastructure():
    """No Consumer, a classe do objeto pode mudar; a infraestrutura do bucket não.

    A fronteira não é "S3 é só evidência" — objetos podem ser reescritos em
    classe mais barata pelo time dono, e `storage_class_only` existe para isso.
    O que nenhuma recomendação pode propor é Lifecycle, versionamento,
    replicação, criptografia, política de bucket ou habilitar análise: tudo isso
    é `Put*` na configuração do bucket.

    A varredura é sobre `action` e `how_to_apply` — o que se propõe fazer. Riscos
    e evidências citam versionamento e Lifecycle de propósito, para explicar por
    que a conta é a que é, e não são recomendação.
    """
    from julius.collection.models import Account, S3Prefix, Table
    from julius.collection.policy import CONSUMER_DATAMESH, policy_for_profile
    from julius.config import DEFAULT_CONFIG
    from julius.knowledge.rules import REGISTRY

    politica = policy_for_profile(CONSUMER_DATAMESH)
    assert politica.s3_mode == "storage_class_only", (
        "o perfil Consumer mudou de modo; esta fronteira precisa ser reavaliada"
    )

    conta = Account(
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
        s3_prefixes=[
            S3Prefix(
                bucket="lake",
                prefix="vendas/",
                kind="table_location",
                source_asset="db.vendas",
                object_count=5000,
                total_bytes=500 * 1024**3,
                average_object_bytes=100 * 1024 * 1024,
                bytes_by_class={"STANDARD": float(500 * 1024**3)},
                object_count_by_class={"STANDARD": 5000},
                last_read_at="2024-01-01",
                read_coverage_days=1000,
                access_source="persisted_touch_history",
                access_quality="measured",
            )
        ],
    )

    infraestrutura = re.compile(
        r"(?i)(lifecycle|ciclo de vida|versionamento|versioning|replica[çc]|"
        r"replication|criptografia|encryption|pol[íi]tica do bucket|bucket policy|"
        r"storage lens|storage class analysis|intelligent[- ]tiering)"
    )

    achados = []
    for familia in REGISTRY:
        if familia.service != "s3":
            continue
        achados += familia.detect(conta, DEFAULT_CONFIG, "scan-1")

    assert achados, "nenhum achado S3 no perfil Consumer; o teste cegou"

    ofensores = []
    for achado in achados:
        for campo in ("recommended_action", "how_to_apply"):
            texto = getattr(achado, campo, "") or ""
            encontrado = infraestrutura.search(texto)
            if encontrado:
                ofensores.append(f"{achado.rule_id}.{campo}: {encontrado.group(0)!r}")

    assert not ofensores, (
        "recomendação de infraestrutura S3 no perfil Consumer:\n"
        + "\n".join(ofensores)
        + "\nClasse de objeto o time dono pode mudar; configuração de bucket, não."
    )
