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

from julius.collection.collectors.paginate import safe_call
from julius.collection.models import S3BucketConfig

#: `NoSuchLifecycleConfiguration` chega aqui já normalizado por `error_category`
#: como `not_found`, e ausência de configuração é **resposta**, não falha: o
#: bucket foi consultado e não tem lifecycle. É o que autoriza a regra a afirmar
#: que não há automação, em vez de só dizer que não olhou.
_AUSENCIA = "not_found"


def collect_bucket_configs(
    s3_client,
    *,
    names: list[str],
    s3control_client=None,
    account_id: str = "",
    gaps: list[str] | None = None,
) -> list[S3BucketConfig]:
    """Um retrato de configuração por bucket, sem ler um objeto sequer."""
    if s3_client is None:
        return []
    lens = _storage_lens_enabled(s3control_client, account_id, gaps)
    return [
        _config_do_bucket(s3_client, name, lens, gaps) for name in names
    ]


def _config_do_bucket(
    s3_client, bucket: str, lens: bool | None, gaps: list[str] | None
) -> S3BucketConfig:
    config = S3BucketConfig(bucket=bucket, storage_lens_enabled=lens)

    resposta, falha = safe_call(s3_client, "get_bucket_logging", Bucket=bucket)
    if falha:
        _anotar(gaps, "get_bucket_logging", falha)
    else:
        config.access_logging_enabled = bool(resposta.get("LoggingEnabled"))

    resposta, falha = safe_call(
        s3_client, "list_bucket_analytics_configurations", Bucket=bucket
    )
    if falha:
        _anotar(gaps, "list_bucket_analytics_configurations", falha)
    else:
        config.storage_class_analysis_ids = _ids(
            resposta.get("AnalyticsConfigurationList")
        )

    resposta, falha = safe_call(
        s3_client, "list_bucket_intelligent_tiering_configurations", Bucket=bucket
    )
    if falha:
        _anotar(gaps, "list_bucket_intelligent_tiering_configurations", falha)
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
        _anotar(gaps, "get_bucket_lifecycle_configuration", falha)
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
        _anotar(gaps, "get_bucket_metadata_configuration", falha)
    else:
        config.metadata_table_enabled = bool(
            resposta.get("GetBucketMetadataConfigurationResult")
            or resposta.get("MetadataConfigurationResult")
        )

    return config


def _storage_lens_enabled(
    s3control_client, account_id: str, gaps: list[str] | None
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
        _anotar(gaps, "list_storage_lens_configurations", falha)
        return None
    return bool(resposta.get("StorageLensConfigurationList"))


def _ids(itens) -> list[str]:
    return [
        str(item.get("Id"))
        for item in itens or ()
        if isinstance(item, dict) and item.get("Id")
    ]


def _anotar(gaps: list[str] | None, operacao: str, categoria: str) -> None:
    if gaps is None:
        return
    entrada = f"{operacao}: {categoria}"
    # O mesmo erro repetido em cem buckets é uma permissão faltando, não cem
    # problemas: a saúde da coleta fica ilegível se cada um virar uma linha.
    if entrada not in gaps:
        gaps.append(entrada)


def buckets_without_access_evidence(configs: list[S3BucketConfig]) -> list[str]:
    """Buckets onde nenhuma fonte de último acesso está ligada.

    É o que separa "estes 4 TB não são lidos há um ano" de "estes 4 TB não são
    *escritos* há um ano, e não temos como saber se são lidos".
    """
    return sorted(
        config.bucket for config in configs if config.last_access_source == "none"
    )
