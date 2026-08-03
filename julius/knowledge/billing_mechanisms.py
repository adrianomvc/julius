"""Como cada serviço cobra — a unidade, não o preço.

A distinção é a que separa duas fontes que costumam ser tratadas como uma só. A
**documentação oficial** responde *como se cobra*: qual unidade, qual
granularidade, qual mínimo faturável, o que entra na conta e o que não entra.
A **fonte de preço** responde *quanto custa*, e essa é resolvida pelo Python, em
`knowledge/pricing/`, com tabela versionada por região.

Confundir as duas é como uma estimativa ganha um número inventado com aparência
de rigor: alguém lê numa página de produto que "Express custa a fração de
Standard", trata a frase como tarifa e monta a conta em cima dela. Aqui a frase
vira mecanismo — *Express cobra por GB-segundo e duração; Standard cobra por
transição de estado* — e o preço de cada unidade vem da tabela.

O mínimo faturável é o campo que mais evita erro na prática. Uma query Athena que
varre 2 MB é cobrada como 10 MB, e um prefixo de arquivos pequenos fica **mais
caro** em Glacier IR do que em Standard por causa do mínimo de 128 KB por objeto.
Quem estima sem esse campo produz economia que não acontece.
"""

from __future__ import annotations

from dataclasses import dataclass

DOC_GLUE = "https://docs.aws.amazon.com/glue/latest/dg/monitor-debug-capacity.html"
DOC_ATHENA = "https://docs.aws.amazon.com/athena/latest/ug/performance-tuning.html"
DOC_SFN = "https://docs.aws.amazon.com/step-functions/latest/dg/express-limitations.html"
DOC_SAGEMAKER = "https://docs.aws.amazon.com/sagemaker/latest/dg/model-managed-spot-training.html"
DOC_S3 = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html"


@dataclass(frozen=True)
class BillingMechanism:
    """A unidade cobrada de uma dimensão de um serviço."""

    key: str
    service: str
    #: A unidade que aparece na fatura, em uma frase.
    unit: str
    #: O menor incremento cobrado, quando existe um. `""` quando é contínuo.
    minimum: str
    #: O que a conta precisa considerar e é fácil esquecer.
    caveat: str
    doc: str


_MECHANISMS: dict[str, BillingMechanism] = {
    item.key: item
    for item in (
        BillingMechanism(
            key="glue_dpu_hour",
            service="glue",
            unit="DPU-hora, por segundo de execução",
            minimum="1 minuto por execução",
            caveat=(
                "reduzir workers sem reduzir duração não reduz a conta na mesma "
                "proporção; o produto DPU × tempo é o que se paga"
            ),
            doc=DOC_GLUE,
        ),
        BillingMechanism(
            key="athena_bytes_scanned",
            service="athena",
            unit="TB varrido pela query",
            minimum="10 MB por query",
            caveat=(
                "compressão e formato colunar mudam bytes varridos sem mudar o "
                "resultado; query cancelada também é cobrada pelo que varreu"
            ),
            doc=DOC_ATHENA,
        ),
        BillingMechanism(
            key="sfn_state_transition",
            service="stepfunctions",
            unit="transição de estado (Standard)",
            minimum="",
            caveat=(
                "Express cobra por requisição, duração e memória — mudar de "
                "modalidade troca a unidade, não só a tarifa"
            ),
            doc=DOC_SFN,
        ),
        BillingMechanism(
            key="sfn_express_duration",
            service="stepfunctions",
            unit="GB-segundo mais requisições (Express)",
            minimum="100 ms por execução",
            caveat=(
                "execução longa em Express pode custar mais que em Standard; a "
                "comparação exige benchmark de duração e memória reais"
            ),
            doc=DOC_SFN,
        ),
        BillingMechanism(
            key="sagemaker_instance_second",
            service="sagemaker",
            unit="segundo de instância, por tipo",
            minimum="",
            caveat=(
                "o tempo de download do dado antes da primeira época é cobrado; "
                "managed spot cobra o tempo útil, não o interrompido"
            ),
            doc=DOC_SAGEMAKER,
        ),
        BillingMechanism(
            key="s3_storage_gb_month",
            service="s3",
            unit="GB-mês por classe de armazenamento",
            minimum="128 KB por objeto em IA e Glacier IR; 30/90/180 dias de retenção mínima",
            caveat=(
                "prefixo de arquivo pequeno fica mais caro em classe fria por "
                "causa do mínimo por objeto; a transição em si custa um request"
            ),
            doc=DOC_S3,
        ),
    )
}


class UnknownBillingMechanismError(KeyError):
    """Estimar sem saber a unidade cobrada é estimar sobre nada."""

    def __init__(self, key: str):
        super().__init__(
            f"mecanismo de cobrança desconhecido: {key!r}. "
            f"Conhecidos: {', '.join(sorted(_MECHANISMS))}. "
            "Acrescente uma entrada com a unidade, o mínimo faturável e a "
            "documentação oficial que a sustenta."
        )


def mechanism(key: str) -> BillingMechanism:
    if key not in _MECHANISMS:
        raise UnknownBillingMechanismError(key)
    return _MECHANISMS[key]


def known_mechanisms() -> tuple[str, ...]:
    return tuple(sorted(_MECHANISMS))


#: Método de estimativa → a dimensão que ele mexe. É o que liga uma proposta ao
#: mecanismo que a explica, sem a IA ter de descrevê-lo por extenso.
METHOD_MECHANISM = {
    "glue_interactive_capacity_reduction_v1": "glue_dpu_hour",
    "glue_shuffle_reduction_v1": "glue_dpu_hour",
    "sagemaker_managed_spot_training_v1": "sagemaker_instance_second",
    "sagemaker_gpu_to_cpu_instance_v1": "sagemaker_instance_second",
    "sfn_standard_to_express_v1": "sfn_state_transition",
}


def mechanism_for_method(method: str) -> BillingMechanism | None:
    key = METHOD_MECHANISM.get(method)
    return mechanism(key) if key else None
