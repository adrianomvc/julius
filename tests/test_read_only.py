"""A fronteira do Julius: ele analisa e recomenda, e não muda nada.

Isso não é uma promessa no texto de um docstring — é o que este arquivo cobra.
Uma recomendação de apagar objeto S3, pausar cluster ou reduzir worker é
instrução para o time dono; o Julius nunca a executa. A diferença importa
porque o produto pede credencial AWS de uma conta de produção, e a única coisa
que separa "ferramenta de análise" de "ferramenta com poder de destruir" é a
lista do que ele tem permissão de chamar.

Por isso a garantia é por **allowlist**, não por lista de proibições. Proibir
`delete_object` deixa `delete_objects` passar; permitir só o que está escrito
faz qualquer chamada nova falhar até alguém justificá-la aqui.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1] / "julius"

#: Toda operação AWS que o Julius tem permissão de chamar, e por quê.
#:
#: Acrescentar uma entrada aqui é uma decisão consciente sobre o alcance da
#: credencial que o produto pede. Se a operação altera, cria ou remove qualquer
#: coisa na conta, ela não pertence a esta lista — pertence à recomendação, que
#: o time dono executa.
OPERACOES_PERMITIDAS = {
    # Identidade — confirma que a credencial ativa é a conta esperada.
    "get_caller_identity",
    # Cost Explorer.
    "get_cost_and_usage",
    "get_cost_forecast",
    "get_savings_plans_coverage",
    "get_savings_plans_utilization",
    "get_savings_plans_purchase_recommendation",
    # CloudWatch.
    "get_metric_statistics",
    # Mesma leitura de métrica, em lote: uma chamada carrega até 500 consultas.
    # Não lê nada que `get_metric_statistics` já não lesse — muda quantas idas à
    # AWS as mesmas onze métricas por job custam.
    "get_metric_data",
    # Descobre as séries de integrações de serviço para somar falhas/timeouts;
    # só lê metadados de métricas, limitado pelo coletor a 100 recursos.
    "list_metrics",
    # Athena: histórico e configuração declarada.
    "batch_get_query_execution",
    "get_query_execution",
    "get_query_results",
    "get_work_group",
    "list_work_groups",
    "list_query_executions",
    # Athena: a única operação que **age**. Roda um SELECT no workgroup do
    # Julius para ler a tabela de toques. Não altera dado, mas custa bytes
    # varridos e grava o resultado em S3 — por isso a fonte é opcional, o SQL é
    # validado em `collectors/athena/query.py`, e nada além de SELECT passa.
    "start_query_execution",
    # Glue: inventário, execuções e definições.
    "get_jobs",
    "get_job_runs",
    "get_crawlers",
    "get_crawler_metrics",
    "list_crawls",
    "get_triggers",
    "list_sessions",
    "get_session",
    "list_statements",
    "get_tables",
    "get_table",
    "get_databases",
    # DataBrew.
    "list_jobs",
    "list_job_runs",
    "list_schedules",
    # Step Functions.
    "list_state_machines",
    "describe_state_machine",
    "list_executions",
    "get_execution_history",
    # SageMaker e o autoscaling dos endpoints.
    "list_apps",
    "list_spaces",
    "describe_space",
    "list_domains",
    "describe_domain",
    "describe_app",
    "list_endpoints",
    "describe_endpoint",
    "describe_endpoint_config",
    "list_inference_components",
    "describe_inference_component",
    "list_notebook_instances",
    "describe_notebook_instance",
    "list_training_jobs",
    "describe_training_job",
    "list_processing_jobs",
    "describe_processing_job",
    "list_transform_jobs",
    "describe_transform_job",
    "list_feature_groups",
    "describe_feature_group",
    "list_pipelines",
    "describe_pipeline",
    "list_pipeline_executions",
    "list_pipeline_execution_steps",
    "list_monitoring_schedules",
    "describe_monitoring_schedule",
    "list_inference_recommendations_jobs",
    "describe_inference_recommendations_job",
    "list_tags",
    "describe_scalable_targets",
    "describe_scaling_policies",
    # Redshift.
    "describe_clusters",
    "list_workgroups",
    # S3 — listagem e leitura; nenhuma escrita, nenhuma exclusão.
    "list_objects_v2",
    "list_multipart_uploads",
    "list_parts",
    "get_object",
    "get_bucket_versioning",
    # S3 — configuração declarada do bucket. Lê o que está **ligado**, nunca
    # liga nada: o S3 não expõe último acesso por objeto, e sem saber se há
    # server access logs, Storage Lens, Storage Class Analysis ou
    # Intelligent-Tiering, recomendar classe fria seria apostar que arquivo não
    # regravado não é lido. Habilitar qualquer uma delas é `Put*`, e isso
    # pertence à recomendação, que o time dono executa.
    "get_bucket_logging",
    "get_bucket_lifecycle_configuration",
    "list_bucket_analytics_configurations",
    "list_bucket_intelligent_tiering_configurations",
    "get_bucket_metadata_configuration",
    "list_storage_lens_configurations",
    # EventBridge.
    "list_rules",
    "list_targets_by_rule",
    # CloudTrail e Identity Center.
    "lookup_events",
    "describe_user",
    "list_users",
    # Price List.
    "get_products",
    # Plumbing do boto3, não é operação.
    "get_paginator",
}

#: Verbos que denunciam mutação. Servem para uma mensagem de erro útil quando a
#: allowlist barra algo — não são a garantia, que é a allowlist em si.
_VERBOS_DE_MUTACAO = re.compile(
    r"^(put|post|create|update|delete|remove|modify|set|start(?!_query)|stop|"
    r"terminate|abort|restore|import|apply|attach|detach|enable|disable|tag|"
    r"untag|copy|upload|write|purge|cancel|reboot|resize|pause|resume)_"
)


def _receptor(no: ast.Attribute) -> str:
    alvo = no.value
    if isinstance(alvo, ast.Name):
        return alvo.id
    if isinstance(alvo, ast.Attribute):
        return alvo.attr
    if isinstance(alvo, ast.Call) and isinstance(alvo.func, ast.Attribute):
        return alvo.func.attr
    return ""


#: SMTP não é AWS. O transporte de e-mail tem porteiro próprio, verificado em
#: `test_the_only_outward_action_stays_behind_its_gates`.
_FORA_DA_AWS = ("reporting/delivery/transports/smtp.py",)


def _operacoes_aws() -> dict[str, set[str]]:
    """Operações chamadas sobre algo que parece um cliente boto3.

    Duas exigências, porque só a primeira gerava falso positivo: o receptor
    precisa parecer cliente, e o nome precisa ter forma de operação AWS
    (`verbo_substantivo`). Sem a segunda, `.get()` e `.update()` de dicionário
    entravam na conta.
    """
    encontradas: dict[str, set[str]] = {}
    for caminho in RAIZ.rglob("*.py"):
        relativo = caminho.relative_to(RAIZ).as_posix()
        if relativo in _FORA_DA_AWS:
            continue
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)):
                continue
            receptor = _receptor(no.func).lower()
            if "client" not in receptor and receptor not in {"session"}:
                continue
            if "_" not in no.func.attr:
                continue
            encontradas.setdefault(no.func.attr, set()).add(relativo)
    return encontradas


def test_julius_calls_only_what_it_is_allowed_to_call():
    """Allowlist, não lista de proibições: o que não está escrito não passa."""
    chamadas = _operacoes_aws()
    fora = {
        nome: sorted(arquivos)
        for nome, arquivos in chamadas.items()
        if nome not in OPERACOES_PERMITIDAS and nome != "client"
    }

    detalhe = "\n".join(
        f"  {nome} em {arquivos}"
        + ("   <-- VERBO DE MUTAÇÃO" if _VERBOS_DE_MUTACAO.match(nome) else "")
        for nome, arquivos in sorted(fora.items())
    )
    assert not fora, (
        "operação AWS fora da allowlist:\n"
        f"{detalhe}\n"
        "O Julius analisa e recomenda; alterar a conta é ação do time dono. "
        "Se a operação apenas lê, acrescente-a a OPERACOES_PERMITIDAS com o "
        "motivo."
    )


def test_no_allowed_operation_is_a_mutation():
    """A allowlist não pode ser afrouxada sem que isso apareça.

    `start_query_execution` é a exceção declarada: executa um SELECT no
    workgroup do Julius, não altera dado, e o validador de SQL barra qualquer
    outra coisa. Toda nova exceção precisa passar por aqui.
    """
    excecoes = {"start_query_execution"}
    mutacoes = {
        nome
        for nome in OPERACOES_PERMITIDAS
        if _VERBOS_DE_MUTACAO.match(nome) and nome not in excecoes
    }

    assert not mutacoes, f"verbo de mutação na allowlist: {sorted(mutacoes)}"


def test_julius_deletes_nothing_from_the_filesystem():
    """Ele escreve relatório e estado; não remove arquivo de ninguém."""
    exclusao = re.compile(
        r"\b(os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree|Path\([^)]*\)\.unlink|"
        r"\.unlink\(|\.rmdir\()"
    )
    ofensores = [
        caminho.relative_to(RAIZ).as_posix()
        for caminho in RAIZ.rglob("*.py")
        if exclusao.search(caminho.read_text(encoding="utf-8"))
    ]

    assert not ofensores, f"exclusão de arquivo no pacote: {ofensores}"


def test_the_only_outward_action_stays_behind_its_gates():
    """Enviar e-mail é a única ação para fora, e ela tem porteiro.

    O envio ativo exige modo explícito, configuração local, cadastro da conta,
    confirmação humana e log que impede reenvio. Se algum desses sumir, o teste
    da política de entrega falha antes deste — aqui só se garante que o
    transporte real não é alcançável sem passar pela política.
    """
    servico = (RAIZ / "reporting" / "delivery" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "self.policy.evaluate" in servico, (
        "o envio precisa passar pela avaliação da política"
    )

    # O CLI monta o transporte e o entrega ao serviço — essa é a fiação
    # correta. O que não pode existir é alguém chamando `.send(` do transporte
    # direto, sem a política no caminho.
    fora = []
    for caminho in RAIZ.rglob("*.py"):
        relativo = caminho.relative_to(RAIZ).as_posix()
        if relativo.startswith("reporting/delivery/"):
            continue
        texto = caminho.read_text(encoding="utf-8")
        if re.search(r"transport\.send\(", texto):
            fora.append(relativo)
    assert not fora, f"transporte de e-mail acionado sem política: {fora}"


class _AthenaFalso:
    """Cliente que registra o que foi pedido, sem executar nada."""

    def __init__(self):
        self.chamadas: list[dict] = []

    def start_query_execution(self, **kwargs):
        self.chamadas.append(kwargs)
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **_):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, **_):
        return {"ResultSet": {"Rows": []}}


def test_only_a_select_ever_reaches_athena():
    """A única operação que age só executa leitura."""
    from julius.collection.collectors.athena.query import AthenaQueryError, run_query

    cliente = _AthenaFalso()
    for sql in (
        "DROP TABLE base",
        "INSERT INTO t VALUES (1)",
        "CREATE TABLE x AS SELECT 1",
        "UNLOAD (SELECT 1) TO 's3://x/'",
    ):
        try:
            run_query(cliente, sql)
        except AthenaQueryError:
            continue
        raise AssertionError(f"consulta de escrita passou: {sql!r}")

    assert not cliente.chamadas, "nada de escrita pode chegar ao Athena"


def test_a_select_that_hides_a_write_is_refused():
    """Prefixo não é gramática: começar com SELECT não basta."""
    from julius.collection.collectors.athena.query import AthenaQueryError, run_query

    cliente = _AthenaFalso()
    try:
        run_query(cliente, "SELECT 1 -- e depois DROP TABLE importante")
    except AthenaQueryError:
        pass
    else:
        raise AssertionError("palavra-chave de escrita passou pelo prefixo")

    assert not cliente.chamadas


def test_the_table_name_is_validated_before_it_enters_the_sql():
    """O nome vem da linha de comando e é interpolado na consulta."""
    from julius.collection.collectors.athena.query import (
        AthenaQueryError,
        validate_identifier,
    )

    assert validate_identifier("catalogo.schema.toques") == "catalogo.schema.toques"
    assert validate_identifier('"db"."toques"') == '"db"."toques"'

    for invalido in ("toques; DROP TABLE x", "a b", "", "t/*x*/", "a.b.c.d"):
        try:
            validate_identifier(invalido)
        except AthenaQueryError:
            continue
        raise AssertionError(f"identificador inválido aceito: {invalido!r}")
