"""O identificador com que se acha o recurso no console.

O relatório desenhado mostra um bloco de código com o recurso de cada
recomendação, e quem vai executar procura por ele. Nome solto — `jupyter-avi-ds`
— obriga a adivinhar em qual serviço procurar; o ARN não.

Nem todo achado tem ARN, e isso não é falha: um padrão de query Athena é um
agregado de execuções, um prefixo S3 é um caminho, uma tabela do catálogo tem
nome qualificado. Nesses casos o identificador natural do recurso é mais útil
que um ARN inventado — e inventar um que a AWS não reconheceria seria pior que
não ter nenhum.
"""

from __future__ import annotations

#: `asset_type` → (serviço no ARN, prefixo do recurso).
#: `None` no prefixo significa recurso sem sub-tipo (`arn:…:cluster-name`).
_ARN = {
    "glue_job": ("glue", "job"),
    "glue_session": ("glue", "session"),
    "glue_crawler": ("glue", "crawler"),
    "databrew_job": ("databrew", "job"),
    "state_machine": ("states", "stateMachine"),
    "sagemaker_app": ("sagemaker", "app"),
    "sagemaker_endpoint": ("sagemaker", "endpoint"),
    "redshift_cluster": ("redshift", "cluster"),
    "athena_workgroup": ("athena", "workgroup"),
}


def resource_id(asset_type: str, asset_name: str, account: str, region: str) -> str:
    """ARN quando o tipo tem um; identificador natural quando não tem."""
    if not asset_name:
        return "—"

    servico = _ARN.get(asset_type)
    if servico and account and region:
        nome, prefixo = servico
        return f"arn:aws:{nome}:{region}:{account}:{prefixo}/{asset_name}"

    if asset_type in {"s3_prefix", "s3_bucket"}:
        # O prefixo já vem como `s3://bucket/caminho/`; o bucket, como nome.
        return asset_name if asset_name.startswith("s3://") else f"s3://{asset_name}"

    if asset_type == "glue_service":
        return f"AWS Glue · conta {account}" if account else "AWS Glue"

    # Tabela do catálogo, padrão de query Athena e o que vier depois: o nome é
    # o identificador, e é o que se digita para achar.
    return asset_name
