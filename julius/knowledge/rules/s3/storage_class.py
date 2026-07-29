"""Mover dado frio para classe mais barata — quando dá para provar que é frio.

O sinal `S3-COLD-DATA-REWRITE` já apontava bytes em Standard, mas nascia sem
evidência de uso e sem tarifa, então nunca virava economia. Esta regra fecha as
duas pontas, e o que ela **não** faz é tão importante quanto o que faz.

**Não usa `LastModified` como prova de que o dado é frio.** O S3 não expõe último
acesso por objeto: `LastModified` é a última escrita, e um arquivo gravado uma
vez e lido todo dia parece antigo por ela. A evidência de leitura vem de
`Table.last_read_at` (histórico Athena ou tabela oficial de toques) ou de uma
fonte configurada no bucket (`S3BucketConfig.last_access_source`). Sem nenhuma
delas, o achado sai como **sinal**: descreve o candidato e diz o que ligar, em
vez de anunciar economia sobre uma suposição.

**Desconta o que a AWS cobra além da tarifa**, senão a recomendação promete um
número que não acontece:

- cobrança mínima de 128 KB por objeto em IA e Glacier IR, que torna um prefixo
  de arquivo pequeno **mais caro** depois da transição;
- mínimo de retenção da classe alvo — 30 dias IA, 90 Glacier, 180 Deep Archive —
  cobrado por inteiro mesmo se o objeto for apagado antes;
- o request de transição, um por objeto.

E não recomenda onde já há automação: lifecycle com transição ou
Intelligent-Tiering movem o dado sozinhos, e cobrar por isso seria contar a
mesma economia duas vezes.

O Julius não executa a transição. Num ambiente onde lifecycle não pode ser
configurado, a ação é `CopyObject` com `StorageClass` pelo time dono.
"""

from __future__ import annotations

from dataclasses import dataclass

from julius.collection.collectors.last_read import last_read_by_prefix, last_read_for
from julius.collection.models import Account, S3BucketConfig, S3Prefix
from julius.config import Config
from julius.findings.build import RuleContext, build
from julius.findings.evidence import Evidence
from julius.findings.finding import Finding
from julius.findings.opportunity import Estimation, Opportunity
from julius.findings.recommendation import Recommendation
from julius.findings.signal import Signal

RULE_ID = "S3-STORAGE-CLASS-TRANSITION"
SIGNAL_ID = "S3-COLD-DATA-REWRITE"

_GB = 1024**3

_DOC_STORAGE_CLASS = (
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html"
)
_DOC_LOGGING = (
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html"
)
_DOC_COPY = (
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/copy-object.html"
)

#: Dias sem leitura → classe alvo, da mais cara para a mais barata. Escolhe-se a
#: **última** que couber: a mais barata que o tempo parado justifica.
#:
#: Os cortes são o dobro do mínimo de retenção de cada classe (30 → 90, 90 → 180,
#: 180 → 365), e a folga é deliberada. Mover no limite do mínimo significa que
#: uma única releitura no mês seguinte paga o período inteiro sem ter economizado
#: nada. O dobro é a margem que faz a recomendação valer mesmo se a janela de
#: observação tiver perdido um acesso.
#:
#: Deep Archive fica de fora de propósito: 180 dias de retenção mínima e horas
#: até o primeiro byte não são coisas que uma regra deva escolher pelo time dono.
_ALVOS = (
    (90, "standard_ia", "Standard-IA"),
    (180, "glacier_ir", "Glacier Instant Retrieval"),
    (365, "glacier_flexible", "Glacier Flexible Retrieval"),
)

#: Classes que já são frias: mover de novo não economiza nada.
_JA_FRIAS = frozenset(
    {
        "STANDARD_IA",
        "ONEZONE_IA",
        "INTELLIGENT_TIERING",
        "GLACIER",
        "GLACIER_IR",
        "DEEP_ARCHIVE",
        "REDUCED_REDUNDANCY",
    }
)


@dataclass(frozen=True)
class Candidato:
    """Um prefixo que pode render transição, e tudo que se sabe sobre ele."""

    prefixo: S3Prefix
    config_bucket: S3BucketConfig | None
    bytes_quentes: float
    objetos_quentes: int
    dias_sem_leitura: int | None
    fonte_de_leitura: str

    @property
    def tem_evidencia_de_leitura(self) -> bool:
        return self.dias_sem_leitura is not None


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    """Só vira oportunidade quando dá para provar que o dado não é lido."""
    return [
        _oportunidade(account, candidato, alvo, config, scan_id)
        for candidato in _candidatos(account, config)
        if candidato.tem_evidencia_de_leitura
        and (alvo := _alvo(candidato, config)) is not None
    ]


def signals(account: Account, config: Config) -> list[Signal]:
    """Sem evidência de leitura, o achado é pergunta — não economia."""
    return [
        _sinal(candidato, config)
        for candidato in _candidatos(account, config)
        if not candidato.tem_evidencia_de_leitura
    ]


# ---------------------------------------------------------------------------
# Quem é candidato
# ---------------------------------------------------------------------------


def _candidatos(account: Account, config: Config) -> list[Candidato]:
    por_bucket = {
        item.bucket: item for item in getattr(account, "s3_bucket_configs", None) or ()
    }
    leituras = last_read_by_prefix(account)
    out: list[Candidato] = []
    for prefixo in getattr(account, "s3_prefixes", None) or ():
        config_bucket = por_bucket.get(prefixo.bucket)
        if config_bucket is not None and config_bucket.transitions_automatically:
            # Lifecycle ou Intelligent-Tiering já movem este dado. Recomendar
            # aqui seria cobrar de novo por uma economia que já está em curso.
            continue
        quentes, objetos = _em_classe_quente(prefixo)
        if quentes < config.thresholds.s3_min_cold_bytes:
            continue
        if not _objeto_grande_o_bastante(prefixo, config):
            continue
        dias, fonte = _dias_sem_leitura(prefixo, leituras, config_bucket, account)
        out.append(
            Candidato(
                prefixo=prefixo,
                config_bucket=config_bucket,
                bytes_quentes=quentes,
                objetos_quentes=objetos,
                dias_sem_leitura=dias,
                fonte_de_leitura=fonte,
            )
        )
    return out


def _em_classe_quente(prefixo: S3Prefix) -> tuple[float, int]:
    """Bytes e objetos que ainda estão numa classe cara."""
    bytes_quentes = sum(
        valor
        for classe, valor in prefixo.bytes_by_class.items()
        if classe.upper() not in _JA_FRIAS
    )
    objetos = sum(
        valor
        for classe, valor in prefixo.object_count_by_class.items()
        if classe.upper() not in _JA_FRIAS
    )
    return bytes_quentes, objetos


def _objeto_grande_o_bastante(prefixo: S3Prefix, config: Config) -> bool:
    """Arquivo pequeno **encarece** ao ir para IA ou Glacier IR.

    A cobrança mínima é de 128 KB por objeto: um prefixo com milhões de arquivos
    de 4 KB pagaria 32x mais espaço faturado. Sem o tamanho medido não há como
    saber, e não saber é motivo para não recomendar.
    """
    if prefixo.average_object_bytes is None:
        return False
    return prefixo.average_object_bytes >= config.thresholds.s3_min_object_bytes_for_ia


def _dias_sem_leitura(
    prefixo: S3Prefix,
    leituras: dict[str, str],
    config_bucket: S3BucketConfig | None,
    account: Account,
) -> tuple[int | None, str]:
    """Há quantos dias este prefixo não é lido, e como se sabe disso.

    `None` significa que ninguém mediu — nunca "não é lido". A diferença é a
    razão de existir desta regra: `LastModified` responderia sempre, e
    responderia a outra pergunta.
    """
    ultima = last_read_for(prefixo, leituras)
    if ultima:
        dias = _dias_ate(ultima, account.window_end)
        if dias is not None:
            return dias, "histórico de leitura do catálogo"
    fonte = config_bucket.last_access_source if config_bucket else "none"
    if fonte != "none":
        # A fonte está ligada no bucket mas o Julius ainda não lê o conteúdo
        # dela. Saber que existe já muda a recomendação: a evidência é
        # obtenível, e o próximo passo é consultá-la, não habilitá-la.
        return None, fonte
    return None, "none"


def _dias_ate(quando: str, referencia: str) -> int | None:
    from datetime import date, datetime

    try:
        lido = datetime.fromisoformat(quando).date()
    except (TypeError, ValueError):
        return None
    try:
        fim = date.fromisoformat(referencia[:10]) if referencia else date.today()
    except (TypeError, ValueError):
        fim = date.today()
    return max(0, (fim - lido).days)


def _alvo(candidato: Candidato, config: Config) -> tuple[str, str] | None:
    """A classe mais barata que o tempo sem leitura sustenta, ou `None`."""
    dias = candidato.dias_sem_leitura or 0
    if dias < config.thresholds.s3_cold_after_days:
        return None
    retencao = dict(config.thresholds.s3_min_retention_days)
    escolhido: tuple[str, str] | None = None
    for minimo, chave, rotulo in _ALVOS:
        # O dado precisa estar parado por mais tempo que o mínimo de retenção
        # cobrado pela classe: mover antes disso paga o período inteiro assim
        # mesmo, e a economia vira prejuízo se alguém reler no meio.
        if dias >= minimo and dias >= retencao.get(chave, minimo):
            escolhido = (chave, rotulo)
    return escolhido


# ---------------------------------------------------------------------------
# A conta
# ---------------------------------------------------------------------------


def _estimation(
    candidato: Candidato, alvo: tuple[str, str], config: Config
) -> Estimation:
    chave, rotulo = alvo
    pricing = config.pricing
    delta_gb = pricing.s3_storage_delta(chave)
    if delta_gb is None:
        return Estimation(
            method="s3_storage_class_transition_v1",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                f"tarifa de S3 ausente na tabela {pricing.region}",
                "rode `julius pricing refresh` para quantificar",
            ],
            saving_quality="unavailable",
        )

    gb = candidato.bytes_quentes / _GB
    economia_mensal = delta_gb * gb
    transicao = pricing.s3_request_cost(
        "lifecycle_transition", candidato.objetos_quentes
    ) or 0.0
    baseline = (pricing.s3_storage_gb_month.get("standard") or 0.0) * gb
    liquida = max(0.0, economia_mensal - transicao)
    return Estimation(
        method="s3_storage_class_transition_v1",
        baseline_cost=round(baseline, 2),
        projected_cost=round(max(0.0, baseline - liquida), 2),
        estimated_saving=round(liquida, 2),
        assumptions=[
            f"{gb:.1f} GB em classe quente movidos para {rotulo}",
            f"economia por GB-mês = diferença de tarifa ({delta_gb:.5f} USD)",
            (
                f"descontado o custo de transição de {candidato.objetos_quentes} "
                f"objeto(s): US$ {transicao:.2f}"
            ),
            f"tarifa: {pricing.provenance}",
            "recorrente a partir do mês seguinte à execução",
        ],
        baseline_quality="modeled",
        # A tarifa é da tabela oficial; a evidência de que o dado é frio é
        # inferida de leitura no catálogo, não medida por objeto.
        saving_quality="modeled_evidence",
        baseline_bytes=int(candidato.bytes_quentes),
        avoidable_bytes=int(candidato.bytes_quentes),
    )


# ---------------------------------------------------------------------------
# As saídas
# ---------------------------------------------------------------------------


def _oportunidade(
    account: Account,
    candidato: Candidato,
    alvo: tuple[str, str],
    config: Config,
    scan_id: str,
) -> Opportunity:
    _, rotulo = alvo
    prefixo = candidato.prefixo
    gb = candidato.bytes_quentes / _GB
    parcial = not prefixo.listing_complete
    est = _estimation(candidato, alvo, config)

    opportunity = build(
        Finding(
            rule_id=RULE_ID,
            rule_version="1.0.0",
            asset_type="s3_prefix",
            asset_name=prefixo.location,
            title=f"Dado sem leitura ocupando classe quente ({rotulo} serve)",
            why=(
                f"{gb:.1f} GB em classe quente sob '{prefixo.location}', sem "
                f"leitura registrada há {candidato.dias_sem_leitura} dias. O "
                f"objeto médio tem {(prefixo.average_object_bytes or 0) / 1024:.0f} KB, "
                f"acima do mínimo faturável de 128 KB — a transição para {rotulo} "
                "reduz o armazenamento sem cair na cobrança mínima por objeto."
            ),
            source_process=prefixo.source_asset or None,
        ),
        Recommendation(
            difficulty=2,
            action=f"Mover os objetos deste prefixo para {rotulo}",
            how_to_apply=(
                f"Reescrever os objetos com StorageClass={rotulo} via CopyObject "
                "(ou uma regra de lifecycle, onde ela puder ser configurada). "
                "O Julius não executa a transição — a ação é do time dono do "
                "prefixo, que confirma antes que nenhum consumidor relê estes "
                "dados fora da janela observada."
            ),
            how_to_validate=(
                "Comparar BucketSizeBytes por StorageType no CloudWatch e a "
                "linha de armazenamento do S3 no Cost Explorer no mês seguinte."
            ),
            risks=[
                f"{rotulo} cobra retrieval por leitura; releitura frequente reverte a economia",
                "mínimo de retenção da classe alvo é cobrado mesmo se o objeto for apagado antes",
                "a leitura observada cobre só a janela de análise",
                *(["listagem parcial: o volume real pode ser maior"] if parcial else []),
            ],
            docs=[_DOC_STORAGE_CLASS, _DOC_COPY],
            blocked=est.saving_quality == "unavailable",
        ),
        Evidence(
            items=[
                f"{gb:.2f} GB em classe quente",
                f"{candidato.objetos_quentes} objeto(s) a mover",
                f"sem leitura há {candidato.dias_sem_leitura} dias "
                f"({candidato.fonte_de_leitura})",
                f"objeto médio: {(prefixo.average_object_bytes or 0) / 1024:.0f} KB",
                (
                    "listagem completa"
                    if prefixo.listing_complete
                    else "listagem truncada: evidência parcial"
                ),
            ],
            sources=["S3 ListObjectsV2", "Athena GetQueryExecution", "Price List"],
            observed_runs=1,
            coverage_days=config.thresholds.min_coverage_days,
            has_optional_metrics=est.saving_quality != "unavailable",
            owner_tag=prefixo.owner_tag,
        ),
        est,
        RuleContext(account=account.account_id, config=config, scan_id=scan_id),
    )
    faltando = []
    if parcial:
        faltando.append("listagem completa do prefixo: o volume medido é piso")
    if candidato.fonte_de_leitura != "server_access_logs":
        faltando.append(
            "leitura por objeto: a evidência atual é do catálogo, não do bucket"
        )
    opportunity.missing_evidence = faltando
    return opportunity


def _sinal(candidato: Candidato, config: Config) -> Signal:
    prefixo = candidato.prefixo
    gb = candidato.bytes_quentes / _GB
    obtenivel = candidato.fonte_de_leitura != "none"
    return Signal(
        kind="config",
        rule_id=SIGNAL_ID,
        asset_type="s3_prefix",
        asset_name=prefixo.location,
        observation=(
            f"{gb:.1f} GB em classe quente sob '{prefixo.location}', com objeto "
            f"médio de {(prefixo.average_object_bytes or 0) / 1024:.0f} KB. "
            "A última **escrita** é conhecida; a última leitura, não."
        ),
        question=(
            "Estes dados são lidos? O S3 não registra último acesso por objeto, "
            "e a data de modificação não responde isso — um arquivo escrito uma "
            "vez e lido todo dia parece antigo por ela."
        ),
        missing_evidence=[
            (
                f"consultar a fonte de acesso já habilitada neste bucket "
                f"({candidato.fonte_de_leitura})"
                if obtenivel
                else "nenhuma fonte de último acesso habilitada no bucket: "
                "server access logging, Storage Lens advanced, Storage Class "
                "Analysis ou Intelligent-Tiering"
            ),
            "leituras deste prefixo fora do Athena (Spark, EMR, download direto)",
        ],
        doc_links=[_DOC_STORAGE_CLASS, _DOC_LOGGING],
    )
