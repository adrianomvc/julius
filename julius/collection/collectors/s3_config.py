"""O que está configurado no bucket — e, portanto, o que dá para afirmar.

**O S3 não tem last access time nativo por objeto.** `LastModified` é a data da
última *escrita*, e usá-la como proxy de uso trocaria a classe de armazenamento
de um arquivo gravado uma vez e lido todo dia. Saber se um objeto é lido depende
de algo que precisa estar ligado **antes**, no bucket:

- `GetBucketLogging` → server access logs, o único por objeto e sem custo de
  entrega (paga-se o armazenamento do log). Best-effort: registro pode atrasar
  horas ou não chegar.
- `ListBucketAnalyticsConfigurations` → Storage Class Analysis, que observa 30
  dias ou mais e só recomenda Standard → Standard-IA.
- `ListBucketIntelligentTieringConfigurations` → a AWS mede o acesso e move
  sozinha, cobrando monitoramento por objeto.
- `GetBucketLifecycleConfiguration` → não mede acesso, mas diz se já existe
  automação de transição; recomendar onde ela age é cobrar duas vezes.
- `GetBucketMetadataConfiguration` → tabelas Iceberg com chave, tamanho, classe
  e data de modificação, consultáveis por Athena. Substitui a listagem; **não**
  resolve leitura, porque a journal table registra CREATE/UPDATE/DELETE e não
  GET.

Por isso esta fonte não coleta uso: coleta a **capacidade de medir uso**, para
que o relatório diga "não dá para saber se estes 4 TB são lidos; ligue o server
access logging neste bucket" em vez de recomendar Glacier no escuro.

Cada chamada é isolada. Uma negada deixa o campo em `None` — "não consultado" —
e nunca vira `False`, que se leria como "consultado e está desligado".
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from julius.collection.collectors.paginate import safe_call
from julius.collection.models import IamGap, S3BucketConfig

#: `NoSuchLifecycleConfiguration` chega aqui já normalizado por `error_category`
#: como `not_found`, e ausência de configuração é **resposta**, não falha: o
#: bucket foi consultado e não tem lifecycle. É o que autoriza a regra a afirmar
#: que não há automação, em vez de só dizer que não olhou.
_AUSENCIA = "not_found"
S3_CONFIG_WORKERS = 8
_PARALLEL_THRESHOLD = 4

_IAM_ACTIONS = {
    "get_bucket_logging": "s3:GetBucketLogging",
    "get_bucket_lifecycle_configuration": "s3:GetLifecycleConfiguration",
    "list_bucket_analytics_configurations": "s3:GetAnalyticsConfiguration",
    "list_bucket_intelligent_tiering_configurations": (
        "s3:GetIntelligentTieringConfiguration"
    ),
    "get_bucket_metadata_configuration": "s3:GetBucketMetadataTableConfiguration",
    "list_storage_lens_configurations": "s3:ListStorageLensConfigurations",
}


def collect_bucket_configs(
    s3_client,
    *,
    names: list[str],
    s3control_client=None,
    account_id: str = "",
    gaps: list[str] | None = None,
    iam_gaps: dict[tuple[str, str], IamGap] | None = None,
    workers: int = S3_CONFIG_WORKERS,
) -> list[S3BucketConfig]:
    """Um retrato de configuração por bucket, sem ler um objeto sequer."""
    if s3_client is None:
        return []
    lens = _storage_lens_enabled(
        s3control_client, account_id, gaps, iam_gaps=iam_gaps
    )
    def collect_one(name: str):
        local_gaps: list[str] = []
        local_iam: dict[tuple[str, str], IamGap] = {}
        config = _config_do_bucket(
            s3_client,
            name,
            lens,
            local_gaps,
            iam_gaps=local_iam,
        )
        return config, local_gaps, local_iam

    if workers <= 1 or len(names) < _PARALLEL_THRESHOLD:
        results = [collect_one(name) for name in names]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(names))) as pool:
            # `map` preserva a ordem de `names`; rede corre em paralelo, modelo
            # e gaps só entram no agregador depois que o worker termina.
            results = list(pool.map(collect_one, names))

    configs: list[S3BucketConfig] = []
    for config, local_gaps, local_iam in results:
        configs.append(config)
        _merge_gaps(gaps, local_gaps)
        _merge_iam_gaps(iam_gaps, local_iam)
    return configs


def _config_do_bucket(
    s3_client,
    bucket: str,
    lens: bool | None,
    gaps: list[str] | None,
    *,
    iam_gaps: dict[tuple[str, str], IamGap] | None = None,
) -> S3BucketConfig:
    config = S3BucketConfig(bucket=bucket, storage_lens_enabled=lens)

    resposta, falha = safe_call(s3_client, "get_bucket_logging", Bucket=bucket)
    if falha:
        _anotar(gaps, "get_bucket_logging", falha, bucket, iam_gaps)
    else:
        logging = resposta.get("LoggingEnabled") or {}
        config.access_logging_enabled = bool(logging)
        config.access_log_target_bucket = str(logging.get("TargetBucket") or "")
        config.access_log_target_prefix = str(logging.get("TargetPrefix") or "")

    resposta, falha = safe_call(
        s3_client, "list_bucket_analytics_configurations", Bucket=bucket
    )
    if falha:
        _anotar(
            gaps, "list_bucket_analytics_configurations", falha, bucket, iam_gaps
        )
    else:
        config.storage_class_analysis_ids = _ids(
            resposta.get("AnalyticsConfigurationList")
        )

    resposta, falha = safe_call(
        s3_client, "list_bucket_intelligent_tiering_configurations", Bucket=bucket
    )
    if falha:
        _anotar(
            gaps,
            "list_bucket_intelligent_tiering_configurations",
            falha,
            bucket,
            iam_gaps,
        )
    else:
        config.intelligent_tiering_ids = _ids(
            resposta.get("IntelligentTieringConfigurationList")
        )

    resposta, falha = safe_call(
        s3_client, "get_bucket_lifecycle_configuration", Bucket=bucket
    )
    if falha == _AUSENCIA:
        config.lifecycle_rules = []
    elif falha:
        _anotar(
            gaps, "get_bucket_lifecycle_configuration", falha, bucket, iam_gaps
        )
    else:
        config.lifecycle_rules = [
            regra for regra in resposta.get("Rules", []) or [] if isinstance(regra, dict)
        ]

    resposta, falha = safe_call(
        s3_client, "get_bucket_metadata_configuration", Bucket=bucket
    )
    if falha == _AUSENCIA:
        config.metadata_table_enabled = False
    elif falha:
        _anotar(
            gaps, "get_bucket_metadata_configuration", falha, bucket, iam_gaps
        )
    else:
        config.metadata_table_enabled = bool(
            resposta.get("GetBucketMetadataConfigurationResult")
            or resposta.get("MetadataConfigurationResult")
        )

    return config


def _storage_lens_enabled(
    s3control_client,
    account_id: str,
    gaps: list[str] | None,
    *,
    iam_gaps: dict[tuple[str, str], IamGap] | None = None,
) -> bool | None:
    """Storage Lens é por conta, não por bucket — uma consulta serve para todos.

    O tier avançado é o que traz activity metrics por prefixo; o gratuito não
    mede requisição. Aqui só se registra que existe configuração: qual tier está
    ligado sai do `GetStorageLensConfiguration` de cada uma, e essa distinção
    ainda não muda nenhuma recomendação.
    """
    if s3control_client is None or not account_id:
        return None
    resposta, falha = safe_call(
        s3control_client, "list_storage_lens_configurations", AccountId=account_id
    )
    if falha:
        _anotar(
            gaps,
            "list_storage_lens_configurations",
            falha,
            account_id,
            iam_gaps,
        )
        return None
    return bool(resposta.get("StorageLensConfigurationList"))


def _ids(itens) -> list[str]:
    return [
        str(item.get("Id"))
        for item in itens or ()
        if isinstance(item, dict) and item.get("Id")
    ]


def _anotar(
    gaps: list[str] | None,
    operacao: str,
    categoria: str,
    recurso: str = "",
    iam_gaps: dict[tuple[str, str], IamGap] | None = None,
) -> None:
    if gaps is None:
        return
    entrada = f"{operacao}: {categoria}"
    # O mesmo erro repetido em cem buckets é uma permissão faltando, não cem
    # problemas: a saúde da coleta fica ilegível se cada um virar uma linha.
    if entrada not in gaps:
        gaps.append(entrada)
    if categoria != "permission_denied" or iam_gaps is None:
        return
    key = (operacao, categoria)
    gap = iam_gaps.setdefault(
        key,
        IamGap(
            service="s3control" if operacao.startswith("list_storage_lens") else "s3",
            operation=operacao,
            iam_action=_IAM_ACTIONS[operacao],
        ),
    )
    gap.affected_resources += 1
    if recurso and recurso not in gap.examples and len(gap.examples) < 3:
        gap.examples.append(recurso)


def _merge_gaps(target: list[str] | None, source: list[str]) -> None:
    if target is None:
        return
    for gap in source:
        if gap not in target:
            target.append(gap)


def _merge_iam_gaps(
    target: dict[tuple[str, str], IamGap] | None,
    source: dict[tuple[str, str], IamGap],
) -> None:
    if target is None:
        return
    for key, incoming in source.items():
        current = target.setdefault(
            key,
            IamGap(
                service=incoming.service,
                operation=incoming.operation,
                iam_action=incoming.iam_action,
                category=incoming.category,
            ),
        )
        current.affected_resources += incoming.affected_resources
        for example in incoming.examples:
            if example not in current.examples and len(current.examples) < 3:
                current.examples.append(example)


def buckets_without_access_evidence(configs: list[S3BucketConfig]) -> list[str]:
    """Buckets onde nenhuma fonte de último acesso está ligada.

    É o que separa "estes 4 TB não são lidos há um ano" de "estes 4 TB não são
    *escritos* há um ano, e não temos como saber se são lidos".
    """
    return sorted(
        config.bucket for config in configs if config.last_access_source == "none"
    )
