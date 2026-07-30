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
    qualidade_de_leitura: str

    @property
    def tem_evidencia_de_leitura(self) -> bool:
        return self.dias_sem_leitura is not None


def detect(account: Account, config: Config, scan_id: str) -> list[Opportunity]:
    """Só vira oportunidade quando dá para provar que o dado não é lido."""
    if getattr(account, "s3_mode", "proposal") == "evidence_only":
        return []
    out: list[Opportunity] = []
    for candidato in _candidatos(account, config):
        if not candidato.tem_evidencia_de_leitura:
            continue
        alvo = _alvo(candidato, config)
        if alvo is None:
            continue
        oportunidade = _oportunidade(account, candidato, alvo, config, scan_id)
        estimation = oportunidade.estimation
        if estimation is None:
            continue
        if (
            estimation.saving_quality != "unavailable"
            and estimation.estimated_saving <= 0
        ):
            continue
        expiration = (
            candidato.config_bucket.expiration_days_for_prefix(
                candidato.prefixo.prefix
            )
            if candidato.config_bucket
            else None
        )
        break_even = estimation.break_even_months
        oldest = candidato.prefixo.oldest_object_age_days
        if (
            expiration is not None
            and oldest is not None
            and break_even is not None
        ):
            remaining_days = max(0, expiration - oldest)
            if remaining_days <= break_even * 30:
                # A expiração conta desde a criação do objeto. Se ao menos o
                # objeto mais antigo sai antes do payback, o agregado não sabe
                # separar os demais sem risco de dupla contagem.
                continue
        out.append(oportunidade)
    return out


def signals(account: Account, config: Config) -> list[Signal]:
    """Sem evidência de leitura, o achado é pergunta — não economia."""
    if getattr(account, "s3_mode", "proposal") == "evidence_only":
        return [_evidence_only_signal(item) for item in _candidatos(account, config)]
    return [
        _sinal(candidato, config)
        for candidato in _candidatos(account, config)
        if not candidato.tem_evidencia_de_leitura
    ]


def _evidence_only_signal(candidato: Candidato) -> Signal:
    prefixo = candidato.prefixo
    return Signal(
        kind="inventory_integrity",
        rule_id=RULE_ID,
        asset_type="s3_prefix",
        asset_name=prefixo.location,
        observation=(
            f"{candidato.bytes_quentes / _GB:.1f} GB permanecem em classe quente "
            f"sob '{prefixo.location}'."
        ),
        question=(
            "O padrão de leitura/escrita indica ineficiência no processo produtor "
            "ou consumidor que deve ser corrigida sem alterar o S3 diretamente?"
        ),
        missing_evidence=[
            "processo produtor/consumidor responsável",
            "padrão de acesso e requisito de retenção",
        ],
        doc_links=[_DOC_STORAGE_CLASS],
    )


# ---------------------------------------------------------------------------
# Quem é candidato
# ---------------------------------------------------------------------------


def _candidatos(account: Account, config: Config) -> list[Candidato]:
    por_bucket = {
        item.bucket: item for item in getattr(account, "s3_bucket_configs", None) or ()
    }
    leituras = last_read_by_prefix(account)
    out: list[Candidato] = []
    prefixos = _sem_sobreposicao(
        [
            item
            for item in getattr(account, "s3_prefixes", None) or ()
            if item.kind == "table_location"
        ]
    )
    for prefixo in prefixos:
        config_bucket = por_bucket.get(prefixo.bucket)
        if config_bucket is not None and config_bucket.transitions_prefix(
            prefixo.prefix
        ):
            # Lifecycle ou Intelligent-Tiering já movem este dado. Recomendar
            # aqui seria cobrar de novo por uma economia que já está em curso.
            continue
        quentes, objetos = _em_classe_quente(prefixo)
        if quentes < config.thresholds.s3_min_cold_bytes:
            continue
        if not _objeto_grande_o_bastante(prefixo, config):
            continue
        dias, fonte, qualidade = _dias_sem_leitura(
            prefixo, leituras, config_bucket, account, config
        )
        out.append(
            Candidato(
                prefixo=prefixo,
                config_bucket=config_bucket,
                bytes_quentes=quentes,
                objetos_quentes=objetos,
                dias_sem_leitura=dias,
                fonte_de_leitura=fonte,
                qualidade_de_leitura=qualidade,
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
    config: Config,
) -> tuple[int | None, str, str]:
    """Há quantos dias este prefixo não é lido, e como se sabe disso.

    `None` significa que ninguém mediu — nunca "não é lido". A diferença é a
    razão de existir desta regra: `LastModified` responderia sempre, e
    responderia a outra pergunta.
    """
    ultima = prefixo.last_read_at or last_read_for(prefixo, leituras)
    if ultima:
        dias = _dias_ate(ultima, account.window_end)
        if dias is not None:
            source = prefixo.access_source or "catalog_read_history"
            if (
                dias >= config.thresholds.s3_cold_after_days
                and prefixo.read_coverage_days
                < config.thresholds.s3_cold_after_days
            ):
                return None, source, "insufficient_coverage"
            if prefixo.access_quality in {"partial", "unavailable"}:
                return None, source, prefixo.access_quality
            return (
                dias,
                source,
                prefixo.access_quality or "prefix_inferred",
            )
    if (
        prefixo.read_requests_window == 0
        and prefixo.access_quality != "partial"
        and prefixo.read_coverage_days >= config_bucket_days(account)
    ):
        return (
            prefixo.read_coverage_days,
            prefixo.access_source or "observed_access",
            prefixo.access_quality or "measured",
        )
    fonte = config_bucket.last_access_source if config_bucket else "none"
    if fonte != "none":
        # A fonte está ligada no bucket mas o Julius ainda não lê o conteúdo
        # dela. Saber que existe já muda a recomendação: a evidência é
        # obtenível, e o próximo passo é consultá-la, não habilitá-la.
        return None, fonte, "configured_not_collected"
    return None, "none", "unavailable"


def config_bucket_days(account: Account) -> int:
    """Cobertura mínima útil para afirmar ausência de leitura."""
    return max(1, int(getattr(account, "window_days", 0) or 0))


def _sem_sobreposicao(prefixos: list[S3Prefix]) -> list[S3Prefix]:
    """Mantém o prefixo mais específico quando dois cobrem os mesmos objetos."""
    escolhidos: list[S3Prefix] = []
    for item in sorted(
        prefixos,
        key=lambda p: (p.bucket, -len(p.prefix.strip("/")), p.prefix),
    ):
        local = item.prefix.strip("/")
        if any(
            outro.bucket == item.bucket
            and (
                local == outro.prefix.strip("/")
                or local.startswith(outro.prefix.strip("/") + "/")
                or outro.prefix.strip("/").startswith(local + "/")
            )
            for outro in escolhidos
        ):
            continue
        escolhidos.append(item)
    return escolhidos


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
        if (
            dias >= minimo
            and dias >= retencao.get(chave, minimo)
            and candidato.prefixo.read_coverage_days >= minimo
        ):
            escolhido = (chave, rotulo)
    return escolhido


# ---------------------------------------------------------------------------
# A conta
# ---------------------------------------------------------------------------


def _estimation(
    account: Account, candidato: Candidato, alvo: tuple[str, str], config: Config
) -> Estimation:
    chave, rotulo = alvo
    pricing = config.pricing
    delta_gb = pricing.s3_storage_delta(chave)
    copy_cost = pricing.s3_request_cost(
        f"copy_{chave}", candidato.objetos_quentes
    )
    retrieval_probe = pricing.s3_retrieval_cost(
        chave, bytes_read=0, requests=0
    )
    if delta_gb is None or copy_cost is None or retrieval_probe is None:
        return Estimation(
            method="s3_storage_class_transition_v2",
            baseline_cost=0.0,
            projected_cost=0.0,
            estimated_saving=0.0,
            assumptions=[
                f"tarifa de S3 ausente na tabela {pricing.region}",
                "armazenamento, PUT/COPY e retrieval precisam estar verificados",
                "rode `julius pricing refresh --only s3` para quantificar",
            ],
            saving_quality="unavailable",
            pricing_dependencies=("s3",),
        )

    gb = candidato.bytes_quentes / _GB
    bytes_alvo = _bytes_faturaveis(candidato, chave, config)
    gb_alvo = bytes_alvo / _GB
    transicao = copy_cost
    standard_rate = pricing.s3_storage_gb_month.get("standard") or 0.0
    target_rate = pricing.s3_storage_gb_month.get(chave) or 0.0
    baseline_modelado = standard_rate * gb
    baseline, baseline_quality = _baseline_real(
        account, candidato, baseline_modelado
    )
    # Quando o baseline vem da cobrança real, aplica-se a razão entre tarifas
    # apenas para projetar a classe alvo; assim taxas/descontos do contrato
    # continuam ancorados no que efetivamente foi pago.
    projected_storage = (
        baseline * (target_rate / standard_rate) * (gb_alvo / gb)
        if standard_rate > 0 and gb > 0
        else target_rate * gb_alvo
    )
    retrieval = 0.0
    retrieval_known = (
        candidato.prefixo.read_requests_window is not None
        and candidato.prefixo.bytes_read_window is not None
    )
    if retrieval_known:
        retrieval = (
            pricing.s3_retrieval_cost(
                chave,
                bytes_read=candidato.prefixo.bytes_read_window or 0,
                requests=candidato.prefixo.read_requests_window or 0,
            )
            or 0.0
        )
    recorrente = max(0.0, baseline - projected_storage - retrieval)
    primeiro_mes = recorrente - transicao
    break_even = round(transicao / recorrente, 3) if recorrente > 0 else None
    custo_leitura_total = pricing.s3_retrieval_cost(
        chave,
        bytes_read=int(candidato.bytes_quentes),
        requests=candidato.objetos_quentes,
    )
    max_reads = (
        round((baseline - projected_storage) / custo_leitura_total, 2)
        if custo_leitura_total and custo_leitura_total > 0
        else None
    )
    return Estimation(
        method="s3_storage_class_transition_v2",
        baseline_cost=round(baseline, 2),
        projected_cost=round(projected_storage + retrieval, 2),
        estimated_saving=round(recorrente, 2),
        assumptions=[
            (
                f"{gb:.1f} GB físicos em classe quente; "
                f"{gb_alvo:.1f} GB faturáveis em {rotulo}"
            ),
            f"economia por GB-mês = diferença de tarifa ({delta_gb:.5f} USD)",
            (
                f"custo pontual de transição de {candidato.objetos_quentes} "
                f"objeto(s): US$ {transicao:.2f}; não descontado dos meses seguintes"
            ),
            (
                f"retrieval observado na janela: US$ {retrieval:.2f}"
                if retrieval_known
                else "retrieval não medido; cenário de releitura aparece como risco"
            ),
            f"tarifa: {pricing.provenance}",
            "economia recorrente após a execução; primeiro mês separa o custo pontual",
        ],
        baseline_quality=baseline_quality,
        # A tarifa é da tabela oficial; a evidência de que o dado é frio é
        # inferida de leitura no catálogo, não medida por objeto.
        saving_quality=(
            "allocated_partial"
            if baseline_quality.startswith("allocated")
            else "modeled_evidence"
        ),
        baseline_bytes=int(candidato.bytes_quentes),
        projected_bytes=int(bytes_alvo),
        avoidable_bytes=int(candidato.bytes_quentes),
        one_time_cost=round(transicao, 2),
        monthly_recurring_saving=round(recorrente, 2),
        first_month_net_saving=round(primeiro_mes, 2),
        break_even_months=break_even,
        maximum_profitable_reads=max_reads,
        pricing_dependencies=("s3",),
    )


def _bytes_faturaveis(
    candidato: Candidato, target_class: str, config: Config
) -> float:
    """Aplica o mínimo faturável por objeto somente à classe quente."""
    minimo = int(config.pricing.s3_minimum_billable_bytes.get(target_class, 0))
    prefixo = candidato.prefixo
    if not prefixo.bytes_by_class_size or not prefixo.object_count_by_class_size:
        return max(
            candidato.bytes_quentes,
            float(candidato.objetos_quentes * minimo),
        )
    total = 0.0
    for classe, por_faixa in prefixo.bytes_by_class_size.items():
        if classe.upper() in _JA_FRIAS:
            continue
        contagens = prefixo.object_count_by_class_size.get(classe, {})
        for faixa, physical in por_faixa.items():
            total += max(float(physical), float(contagens.get(faixa, 0) * minimo))
    return total


def _baseline_real(
    account: Account, candidato: Candidato, fallback: float
) -> tuple[float, str]:
    """Rateia a cobrança Standard real pelo volume, quando a cobertura fecha."""
    coverage = getattr(account, "s3_cost_coverage", None)
    buckets = getattr(account, "s3_buckets", None) or ()
    standard_cost = (
        coverage.cost_for({"storage_standard"})
        if coverage is not None and coverage.cost_quality != "unavailable"
        else 0.0
    )
    total_standard = sum(
        float(bucket.bytes_by_class.get("StandardStorage", 0.0))
        for bucket in buckets
    )
    if coverage is not None and standard_cost > 0 and total_standard > 0:
        return (
            standard_cost * candidato.bytes_quentes / total_standard,
            (
                "allocated"
                if coverage.cost_quality == "reconciled"
                else "allocated_partial"
            ),
        )
    return fallback, "modeled"


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
    chave, rotulo = alvo
    prefixo = candidato.prefixo
    gb = candidato.bytes_quentes / _GB
    parcial = not prefixo.listing_complete
    versionado = any(
        bucket.name == prefixo.bucket and bucket.versioning_enabled is True
        for bucket in (getattr(account, "s3_buckets", None) or ())
    )
    est = _estimation(account, candidato, alvo, config)

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
                f"objeto médio não-zero tem "
                f"{(prefixo.average_object_bytes or 0) / 1024:.0f} KB. O cálculo "
                f"aplica o mínimo faturável por objeto da classe {rotulo}, em vez "
                "de comparar somente bytes físicos."
            ),
            source_process=prefixo.source_asset or None,
        ),
        Recommendation(
            difficulty=2,
            action=f"Mover os objetos deste prefixo para {rotulo}",
            how_to_apply=(
                f"Reescrever os objetos com StorageClass={rotulo} via CopyObject "
                "(multipart CopyObject para objetos acima do limite da operação "
                "simples). O Julius não executa a cópia — a ação é do time dono do "
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
                *(
                    [
                        "bucket versionado: CopyObject cria uma nova versão e "
                        "mantém a anterior como versão não corrente"
                    ]
                    if versionado
                    else []
                ),
            ],
            docs=[_DOC_STORAGE_CLASS, _DOC_COPY],
            blocked=(
                est.saving_quality == "unavailable"
                or chave == "glacier_flexible"
                or parcial
                or versionado
            ),
        ),
        Evidence(
            items=[
                f"{gb:.2f} GB em classe quente",
                f"{candidato.objetos_quentes} objeto(s) a mover",
                f"sem leitura há {candidato.dias_sem_leitura} dias "
                f"({candidato.fonte_de_leitura}; "
                f"qualidade={candidato.qualidade_de_leitura})",
                f"objeto médio: {(prefixo.average_object_bytes or 0) / 1024:.0f} KB",
                (
                    f"economia recorrente: US$ "
                    f"{est.monthly_recurring_saving or 0:.2f}/mês; "
                    f"custo pontual: US$ {est.one_time_cost or 0:.2f}"
                ),
                (
                    f"break-even: {est.break_even_months:.3f} mês(es)"
                    if est.break_even_months is not None
                    else "break-even indisponível"
                ),
                (
                    "listagem completa"
                    if prefixo.listing_complete
                    else "listagem truncada: evidência parcial"
                ),
            ],
            sources=[
                "S3 ListObjectsV2",
                candidato.fonte_de_leitura,
                "Cost Explorer",
                "Price List",
            ],
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
        faltando.append("listagem completa do prefixo: cifra parcial não entra no portfólio")
    if versionado:
        faltando.append(
            "custo e retenção das versões não correntes criadas pelo CopyObject"
        )
    if (
        prefixo.read_requests_window is None
        or prefixo.bytes_read_window is None
    ):
        faltando.append(
            "volume e quantidade de leituras para incorporar retrieval observado"
        )
    if est.maximum_profitable_reads is None:
        faltando.append(
            "tarifa de retrieval por GB/request para calcular o limite de releituras"
        )
    if chave == "glacier_flexible":
        faltando.append(
            "confirmação humana de que o SLA aceita recuperação em horas"
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
