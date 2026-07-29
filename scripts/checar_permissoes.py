"""Testa, uma a uma, se a credencial consegue buscar o que o Julius precisa.

Roda antes da coleta e **nunca aborta**: cada chamada é isolada, e o que falha
vira uma linha na tabela em vez de derrubar o resto. É a diferença para o
`julius collect`, que para na primeira fonte obrigatória — quando o `Glue Jobs`
falha por permissão, você não chega a descobrir o que mais está faltando.

Cada chamada pede **um item**. Não lista conta, não varre bucket, não custa nada
além do request. As duas linhas marcadas com `!` são as que derrubam a coleta
inteira quando faltam.

    python scripts/checar_permissoes.py --profile <perfil>
    python scripts/checar_permissoes.py --profile <perfil> --region sa-east-1
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

import boto3

# Fonte do Julius → (serviço, operação, argumentos mínimos, é obrigatória?)
# A ordem é a de execução da coleta, para a tabela ler como o scan lê.
SONDAS: list[tuple[str, str, str, dict, bool]] = [
    ("AWS identity",            "sts",         "get_caller_identity",   {}, True),
    ("Cost Explorer",           "ce",          "get_cost_and_usage",    {}, False),
    ("Glue Jobs",               "glue",        "get_jobs",              {"MaxResults": 1}, True),
    ("Glue Catalog",            "glue",        "get_databases",         {"MaxResults": 1}, False),
    ("Glue Crawlers",           "glue",        "get_crawlers",          {"MaxResults": 1}, False),
    ("Glue Triggers",           "glue",        "get_triggers",          {"MaxResults": 1}, False),
    ("Glue Interactive Sessions", "glue",      "list_sessions",         {"MaxResults": 1}, False),
    ("Glue DataBrew",           "databrew",    "list_jobs",             {"MaxResults": 1}, False),
    ("CloudWatch (métricas)",   "cloudwatch",  "get_metric_data",       {}, False),
    ("Athena Queries",          "athena",      "list_work_groups",      {"MaxResults": 1}, False),
    ("Step Functions",          "stepfunctions", "list_state_machines", {"maxResults": 1}, False),
    ("EventBridge Schedules",   "events",      "list_rules",            {"Limit": 1}, False),
    ("SageMaker Studio",        "sagemaker",   "list_apps",             {"MaxResults": 1}, False),
    ("SageMaker Endpoints",     "sagemaker",   "list_endpoints",        {"MaxResults": 1}, False),
    ("SageMaker (autoscaling)", "application-autoscaling", "describe_scalable_targets",
     {"ServiceNamespace": "sagemaker"}, False),
    ("Amazon Redshift",         "redshift",    "describe_clusters",     {"MaxRecords": 20}, False),
    ("Redshift Serverless",     "redshift-serverless", "list_workgroups", {"maxResults": 1}, False),
    ("CloudTrail Ownership",    "cloudtrail",  "lookup_events",         {"MaxResults": 1}, False),
]


def argumentos_dinamicos(operacao: str) -> dict:
    """Argumentos que dependem da data de hoje."""
    if operacao == "get_cost_and_usage":
        ontem = date.today() - timedelta(days=1)
        return {
            "TimePeriod": {
                "Start": (ontem - timedelta(days=1)).isoformat(),
                "End": ontem.isoformat(),
            },
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
        }
    if operacao == "get_metric_data":
        agora = datetime.now(timezone.utc)
        return {
            "MetricDataQueries": [
                {
                    "Id": "sonda",
                    "MetricStat": {
                        "Metric": {"Namespace": "Glue", "MetricName": "glue.ALL.system.cpuSystemLoad"},
                        "Period": 86400,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
            ],
            "StartTime": agora - timedelta(days=1),
            "EndTime": agora,
        }
    return {}


def categoria(exc: Exception) -> str:
    """Mesma classificação que a coleta usa, sem persistir mensagem."""
    resposta = getattr(exc, "response", None)
    codigo = ""
    if isinstance(resposta, dict):
        erro = resposta.get("Error")
        if isinstance(erro, dict):
            codigo = str(erro.get("Code") or "")
    normal = codigo.lower()
    if any(t in normal for t in ("accessdenied", "unauthorized", "forbidden")):
        return "SEM PERMISSÃO"
    if "throttl" in normal or "limitexceeded" in normal:
        return "throttled"
    if any(t in normal for t in ("nosuch", "notfound", "resourcenotfound")):
        return "não encontrado"
    if "expired" in normal or "invalidtoken" in normal:
        return "CREDENCIAL EXPIRADA"
    if "endpointconnection" in normal or isinstance(exc, (TimeoutError, ConnectionError)):
        return "sem conexão"
    return codigo or type(exc).__name__


def quantos(resposta: dict) -> str:
    """Quantos itens vieram, sem imprimir nome de recurso nenhum."""
    for chave, valor in (resposta or {}).items():
        if isinstance(valor, list):
            return f"{len(valor)} em {chave}"
    return "ok"


def primeiro_nome(resposta: dict, chave: str) -> str | None:
    """O primeiro item de uma lista da resposta, para sondar o passo seguinte.

    `get_jobs` pode passar e `get_job_runs` falhar — são permissões separadas, e
    as duas derrubam a coleta. Sondar só a primeira daria um OK que não vale.
    """
    itens = (resposta or {}).get(chave) or []
    if not itens:
        return None
    primeiro = itens[0]
    if isinstance(primeiro, str):
        return primeiro
    if isinstance(primeiro, dict):
        return primeiro.get("Name")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="perfil SSO do AWS CLI")
    parser.add_argument("--region", default="sa-east-1")
    args = parser.parse_args()

    sessao = boto3.Session(profile_name=args.profile, region_name=args.region)
    print(f"perfil={args.profile or 'default'} região={args.region}\n")
    print(f"{'RESULTADO':34s} {'PESO':12s} {'FONTE':30s} CHAMADA")
    print("-" * 110)

    falhas_obrigatorias = 0

    def sondar(fonte: str, servico: str, operacao: str, kwargs: dict, obrigatoria: bool):
        nonlocal falhas_obrigatorias
        try:
            cliente = sessao.client(servico)
            resultado = getattr(cliente, operacao)(**kwargs)
            estado = f"OK   ({quantos(resultado)})"
        except Exception as exc:  # noqa: BLE001 - a sonda existe para não abortar
            resultado = None
            estado = f"FALHOU  {categoria(exc)}"
            if obrigatoria:
                falhas_obrigatorias += 1
        # "obrigatória" por extenso, e não um `!` na margem: marcador na
        # primeira coluna se confunde com alerta, e o leitor conclui que a
        # linha falhou quando ela só é essencial.
        peso = "obrigatória" if obrigatoria else ""
        print(f"{estado:34s} {peso:12s} {fonte:30s} {servico}:{operacao}")
        return resultado

    for fonte, servico, operacao, base, obrigatoria in SONDAS:
        resposta = sondar(
            fonte, servico, operacao,
            {**base, **argumentos_dinamicos(operacao)}, obrigatoria,
        )
        # Sondas dependentes: permissão separada, e sem um recurso real elas não
        # podem ser testadas. É aqui que um `get_jobs` OK deixa de bastar.
        if operacao == "get_jobs":
            nome = primeiro_nome(resposta or {}, "Jobs")
            if nome:
                sondar("Glue Jobs (execuções)", "glue", "get_job_runs",
                       {"JobName": nome, "MaxResults": 1}, True)
            else:
                motivo = (
                    "get_jobs falhou antes"
                    if resposta is None
                    else "a conta não tem job"
                )
                print(
                    f"{'NÃO SONDADO  ' + motivo:34s} {'obrigatória':12s} "
                    f"{'Glue Jobs (execuções)':30s} glue:get_job_runs"
                )
        if operacao == "get_databases":
            nome = primeiro_nome(resposta or {}, "DatabaseList")
            if nome:
                sondar("Glue Catalog (tabelas)", "glue", "get_tables",
                       {"DatabaseName": nome, "MaxResults": 1}, False)

    print("-" * 110)
    if falhas_obrigatorias:
        print(
            f"BLOQUEADO: {falhas_obrigatorias} permissão(ões) obrigatória(s) faltando.\n"
            "Sem elas o `julius collect` aborta e não produz relatório nenhum."
        )
        return 1
    print(
        "LIBERADO: as obrigatórias passaram, o `julius collect` vai rodar.\n"
        "O que estiver FALHOU acima degrada o relatório — a fonte aparece na\n"
        "saúde da coleta com o motivo, e o scan continua."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
