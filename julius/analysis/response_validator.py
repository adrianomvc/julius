"""Contrato estruturado da análise contextual produzida pelo Devin."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from julius.findings.investigation import (
    AIContextualEstimate,
    AIEstimationProposal,
    AIRecommendation,
)
from julius.knowledge.generative_estimation import eligible, eligible_rule_ids

#: Toda conclusão sobre um artefato precisa dizer qual artefato e onde. Sem
#: isso não há como distinguir leitura do script de suposição sobre ele.
_EVIDENCE_REF_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sha256", "lines"],
    "properties": {
        "sha256": {"type": "string"},
        "lines": {"type": "array", "items": {"type": "integer"}},
    },
}

DEVIN_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "account",
        "scan_id",
        "executive_summary",
        "implementation_order",
        "recommendations",
        "signal_verdicts",
        "uncovered_findings",
    ],
    "properties": {
        "account": {"type": "string"},
        "scan_id": {"type": "string"},
        "executive_summary": {"type": "string"},
        "signal_verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "rule_id",
                    "asset_name",
                    "verdict",
                    "rationale",
                    "evidence_ref",
                ],
                "properties": {
                    "rule_id": {"type": "string"},
                    "asset_name": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["confirmed", "rejected", "needs_evidence"],
                    },
                    "rationale": {"type": "string"},
                    "evidence_ref": _EVIDENCE_REF_SCHEMA,
                    "recommendation": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "required": [
                            "recommended_action",
                            "why",
                            "risks",
                            "required_validation",
                        ],
                        "properties": {
                            "recommended_action": {"type": "string"},
                            "why": {"type": "string"},
                            "risks": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "required_validation": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                    "estimation_proposal": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "required": ["method", "target", "evidence_refs"],
                        "properties": {
                            "method": {"type": "string"},
                            "target": {"type": "object"},
                            "evidence_refs": {
                                "type": "array",
                                "items": _EVIDENCE_REF_SCHEMA,
                            },
                        },
                    },
                    # O único ponto do contrato em que a análise devolve número.
                    # Só para sinal confirmado, só para `rule_id` elegível, e o
                    # motor confere a faixa contra o baseline que ele resolve.
                    "contextual_estimate": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "required": [
                            "billing_mechanism",
                            "reasoning",
                            "inputs",
                            "low",
                            "expected",
                            "high",
                            "assumptions",
                            "validation_plan",
                            "documentation",
                            "missing_evidence",
                        ],
                        "properties": {
                            "billing_mechanism": {"type": "string"},
                            "reasoning": {"type": "string"},
                            "inputs": {"type": "object"},
                            "low": {"type": "number"},
                            "expected": {"type": "number"},
                            "high": {"type": "number"},
                            "assumptions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "validation_plan": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "documentation": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "missing_evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "uncovered_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "asset_type",
                    "asset_name",
                    "evidence_ref",
                    "why_not_covered",
                    "proposed_rule_id",
                    "confidence_basis",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "asset_type": {"type": "string"},
                    "asset_name": {"type": "string"},
                    "evidence_ref": _EVIDENCE_REF_SCHEMA,
                    "why_not_covered": {"type": "string"},
                    "proposed_rule_id": {"type": "string"},
                    "confidence_basis": {"type": "string"},
                },
            },
        },
        "implementation_order": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "opportunity_id",
                    "contextual_diagnosis",
                    "recommendation",
                    "implementation_steps",
                    "validation_steps",
                    "dependencies",
                    "conflicts",
                    "risks",
                    "documentation",
                    "assumptions",
                    "missing_evidence",
                ],
                "properties": {
                    "opportunity_id": {"type": "string"},
                    "contextual_diagnosis": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "implementation_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "validation_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "conflicts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "risks": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "documentation": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["title", "url", "relevance"],
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "relevance": {"type": "string"},
                            },
                        },
                    },
                    "assumptions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "missing_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

_LIST_FIELDS = (
    "implementation_steps",
    "validation_steps",
    "dependencies",
    "conflicts",
    "risks",
    "assumptions",
    "missing_evidence",
)


class AgentOutputError(ValueError):
    """Saída do agente não respeita o contrato ou as evidências do scan."""


@dataclass(frozen=True)
class DocumentationReference:
    title: str
    url: str
    relevance: str


@dataclass(frozen=True)
class ContextualRecommendation:
    opportunity_id: str
    contextual_diagnosis: str
    recommendation: str
    implementation_steps: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    documentation: list[DocumentationReference] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceRef:
    sha256: str
    lines: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class SignalVerdict:
    """O julgamento de uma hipótese estática contra o artefato inteiro."""

    rule_id: str
    asset_name: str
    verdict: str
    rationale: str
    evidence_ref: EvidenceRef
    recommendation: AIRecommendation | None = None
    estimation_proposal: AIEstimationProposal | None = None
    contextual_estimate: AIContextualEstimate | None = None


@dataclass(frozen=True)
class UncoveredFinding:
    """Desperdício observado que nenhuma regra do catálogo cobre.

    Não recebe economia e não entra no ranking: é candidato a virar regra
    determinística depois de revisão humana, não achado pronto para execução.
    """

    title: str
    asset_type: str
    asset_name: str
    evidence_ref: EvidenceRef
    why_not_covered: str
    proposed_rule_id: str
    confidence_basis: str


@dataclass(frozen=True)
class ContextualAnalysis:
    account: str
    scan_id: str
    executive_summary: str
    implementation_order: list[str]
    recommendations: list[ContextualRecommendation]
    signal_verdicts: list[SignalVerdict] = field(default_factory=list)
    uncovered_findings: list[UncoveredFinding] = field(default_factory=list)


def validate_agent_output(
    payload: object,
    *,
    account: str,
    scan_id: str,
    allowed_opportunity_ids: set[str],
    expected_signals: dict[tuple[str, str], str] | None = None,
    known_artifact_hashes: set[str] | None = None,
    known_rule_ids: set[str] | None = None,
) -> ContextualAnalysis:
    """Valida estrutura, vínculo ao scan e links oficiais antes de aceitar a IA."""
    if not isinstance(payload, dict):
        raise AgentOutputError("saída estruturada deve ser um objeto JSON")
    expected_top = {
        "account",
        "scan_id",
        "executive_summary",
        "implementation_order",
        "recommendations",
        "signal_verdicts",
        "uncovered_findings",
    }
    if set(payload) != expected_top:
        raise AgentOutputError("campos de topo ausentes ou não permitidos")
    if payload.get("account") != account or payload.get("scan_id") != scan_id:
        raise AgentOutputError("saída não pertence à conta e ao scan solicitados")
    summary = payload.get("executive_summary")
    order = payload.get("implementation_order")
    recommendations = payload.get("recommendations")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentOutputError("executive_summary ausente")
    if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
        raise AgentOutputError("implementation_order inválida")
    if len(order) != len(set(order)) or not set(order) <= allowed_opportunity_ids:
        raise AgentOutputError("implementation_order contém IDs inválidos ou duplicados")
    if not isinstance(recommendations, list):
        raise AgentOutputError("recommendations inválida")

    parsed: list[ContextualRecommendation] = []
    seen: set[str] = set()
    expected_recommendation = {
        "opportunity_id",
        "contextual_diagnosis",
        "recommendation",
        *_LIST_FIELDS,
        "documentation",
    }
    for raw in recommendations:
        if not isinstance(raw, dict):
            raise AgentOutputError("recomendação deve ser um objeto")
        if set(raw) != expected_recommendation:
            raise AgentOutputError("campos da recomendação ausentes ou não permitidos")
        opportunity_id = raw.get("opportunity_id")
        if opportunity_id not in allowed_opportunity_ids or opportunity_id in seen:
            raise AgentOutputError(f"opportunity_id inválido ou duplicado: {opportunity_id}")
        seen.add(opportunity_id)
        diagnosis = raw.get("contextual_diagnosis")
        recommendation = raw.get("recommendation")
        if not isinstance(diagnosis, str) or not isinstance(recommendation, str):
            raise AgentOutputError("diagnóstico e recomendação devem ser texto")
        # Estrutura válida com texto vazio passaria como "análise completa"; o
        # contrato é de conteúdo, não só de forma.
        if not diagnosis.strip() or not recommendation.strip():
            raise AgentOutputError(
                f"diagnóstico e recomendação não podem ser vazios: {opportunity_id}"
            )
        list_values: dict[str, list[str]] = {}
        for key in _LIST_FIELDS:
            value = raw.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise AgentOutputError(f"{key} deve ser uma lista de textos")
            list_values[key] = value
        # Ou você diz como fazer, ou diz o que falta para saber. As duas listas
        # vazias significam que a oportunidade não foi analisada de fato.
        if not list_values["implementation_steps"] and not list_values["missing_evidence"]:
            raise AgentOutputError(
                "recomendação precisa de implementation_steps ou missing_evidence: "
                f"{opportunity_id}"
            )
        docs_raw = raw.get("documentation")
        if not isinstance(docs_raw, list):
            raise AgentOutputError("documentation deve ser uma lista")
        docs: list[DocumentationReference] = []
        for item in docs_raw:
            if not isinstance(item, dict):
                raise AgentOutputError("referência de documentação inválida")
            if set(item) != {"title", "url", "relevance"}:
                raise AgentOutputError("campos de documentação ausentes ou não permitidos")
            title, url, relevance = (
                item.get("title"),
                item.get("url"),
                item.get("relevance"),
            )
            if not all(isinstance(value, str) and value.strip() for value in (title, url, relevance)):
                raise AgentOutputError("referência de documentação incompleta")
            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or parsed_url.hostname != "docs.aws.amazon.com":
                raise AgentOutputError(f"documentação fora do domínio oficial permitido: {url}")
            docs.append(DocumentationReference(str(title), str(url), str(relevance)))
        if list_values["implementation_steps"] and not docs:
            raise AgentOutputError(
                f"passo de implementação exige documentação oficial: {opportunity_id}"
            )
        parsed.append(
            ContextualRecommendation(
                opportunity_id=opportunity_id,
                contextual_diagnosis=diagnosis,
                recommendation=recommendation,
                documentation=docs,
                **list_values,
            )
        )
    if seen != allowed_opportunity_ids or set(order) != seen:
        raise AgentOutputError(
            "a análise e a ordem devem cobrir todas as oportunidades do contexto"
        )
    verdicts = _parse_signal_verdicts(
        payload.get("signal_verdicts"),
        expected_signals or {},
        known_artifact_hashes or set(),
    )
    uncovered = _parse_uncovered_findings(
        payload.get("uncovered_findings"),
        known_artifact_hashes or set(),
        known_rule_ids or set(),
    )
    return ContextualAnalysis(
        account=account,
        scan_id=scan_id,
        executive_summary=summary,
        implementation_order=order,
        recommendations=parsed,
        signal_verdicts=verdicts,
        uncovered_findings=uncovered,
    )


def _parse_evidence_ref(
    raw: object,
    known_hashes: set[str],
    context: str,
    *,
    required_sha256: str | None = None,
) -> EvidenceRef:
    """Valida a âncora da conclusão no artefato.

    `required_sha256` é o hash que aquele achado precisa citar — o do próprio
    script de onde o sinal saiu. Quando o sinal não vem de artefato (config
    declarada do recurso), não há hash a exigir e `""` é a resposta correta:
    a evidência já está no contexto, não num arquivo.
    """
    if not isinstance(raw, dict) or set(raw) != {"sha256", "lines"}:
        raise AgentOutputError(f"evidence_ref ausente ou malformado em {context}")
    digest = raw.get("sha256")
    lines = raw.get("lines")
    if not isinstance(digest, str):
        raise AgentOutputError(f"evidence_ref.sha256 deve ser texto em {context}")
    if not isinstance(lines, list) or not all(isinstance(item, int) for item in lines):
        raise AgentOutputError(f"evidence_ref.lines deve ser lista de inteiros em {context}")
    if required_sha256 is not None and digest != required_sha256:
        expected = required_sha256[:16] or "vazio (sinal sem artefato)"
        raise AgentOutputError(
            f"evidence_ref não aponta o artefato do achado em {context}: "
            f"esperado {expected}"
        )
    # Conclusão sobre artefato que não veio no pacote não é leitura, é suposição.
    if digest and known_hashes and digest not in known_hashes:
        raise AgentOutputError(
            f"evidence_ref aponta artefato fora do pacote em {context}: {digest[:16]}"
        )
    return EvidenceRef(sha256=digest, lines=list(lines))


def _parse_signal_verdicts(
    raw_verdicts: object,
    expected_signals: dict[tuple[str, str], str],
    known_hashes: set[str],
) -> list[SignalVerdict]:
    """Todo sinal enviado precisa voltar julgado — silêncio não é veredito."""
    if not isinstance(raw_verdicts, list):
        raise AgentOutputError("signal_verdicts inválida")
    parsed: list[SignalVerdict] = []
    seen: set[tuple[str, str]] = set()
    legacy_keys = {"rule_id", "asset_name", "verdict", "rationale", "evidence_ref"}
    extended_keys = legacy_keys | {"recommendation", "estimation_proposal"}
    generative_keys = extended_keys | {"contextual_estimate"}
    for raw in raw_verdicts:
        if not isinstance(raw, dict) or frozenset(raw) not in {
            frozenset(legacy_keys),
            frozenset(extended_keys),
            frozenset(generative_keys),
        }:
            raise AgentOutputError("campos do veredito ausentes ou não permitidos")
        rule_id = raw.get("rule_id")
        asset_name = raw.get("asset_name")
        verdict = raw.get("verdict")
        rationale = raw.get("rationale")
        if not isinstance(rule_id, str) or not isinstance(asset_name, str):
            raise AgentOutputError("veredito precisa de rule_id e asset_name")
        key = (rule_id, asset_name)
        if key in seen:
            raise AgentOutputError(f"veredito duplicado: {rule_id} em {asset_name}")
        if expected_signals and key not in expected_signals:
            raise AgentOutputError(
                f"veredito sobre sinal fora do pacote: {rule_id} em {asset_name}"
            )
        if verdict not in {"confirmed", "rejected", "needs_evidence"}:
            raise AgentOutputError(f"veredito inválido para {rule_id}: {verdict}")
        if not isinstance(rationale, str) or not rationale.strip():
            raise AgentOutputError(f"veredito sem justificativa: {rule_id} em {asset_name}")
        recommendation = _parse_ai_recommendation(raw.get("recommendation"))
        proposal = _parse_estimation_proposal(
            raw.get("estimation_proposal"), known_hashes
        )
        if verdict != "confirmed" and proposal is not None:
            raise AgentOutputError(
                f"estimativa contextual exige veredito confirmed: {rule_id}"
            )
        contextual = _parse_contextual_estimate(raw.get("contextual_estimate"))
        if verdict != "confirmed" and contextual is not None:
            raise AgentOutputError(
                f"faixa contextual exige veredito confirmed: {rule_id}"
            )
        if contextual is not None and eligible(rule_id) is None:
            # A elegibilidade é por `rule_id`, e recusá-la aqui é o que impede a
            # faixa de virar categoria: um sinal fora da lista não recebe número
            # nenhum, por mais bem preenchida que a proposta esteja.
            raise AgentOutputError(
                f"{rule_id} não aceita faixa contextual; elegíveis: "
                f"{', '.join(eligible_rule_ids()) or 'nenhum'}"
            )
        seen.add(key)
        parsed.append(
            SignalVerdict(
                rule_id=rule_id,
                asset_name=asset_name,
                verdict=str(verdict),
                rationale=rationale,
                evidence_ref=_parse_evidence_ref(
                    raw.get("evidence_ref"),
                    known_hashes,
                    f"veredito {rule_id}",
                    required_sha256=expected_signals.get(key),
                ),
                recommendation=recommendation,
                estimation_proposal=proposal,
                contextual_estimate=contextual,
            )
        )
    if seen != set(expected_signals):
        missing = sorted(
            f"{rule}@{asset}" for rule, asset in set(expected_signals) - seen
        )
        raise AgentOutputError(
            "todo sinal do contexto precisa de veredito; faltam: "
            + ", ".join(missing[:5])
        )
    return parsed


def _parse_ai_recommendation(raw: object) -> AIRecommendation | None:
    if raw is None:
        return None
    keys = {"recommended_action", "why", "risks", "required_validation"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise AgentOutputError("recomendação de sinal inválida")
    action, why = raw.get("recommended_action"), raw.get("why")
    risks, validation = raw.get("risks"), raw.get("required_validation")
    if not isinstance(action, str) or not action.strip():
        raise AgentOutputError("recommended_action de sinal ausente")
    if not isinstance(why, str) or not why.strip():
        raise AgentOutputError("why de sinal ausente")
    if not isinstance(risks, list) or not all(isinstance(x, str) for x in risks):
        raise AgentOutputError("risks de sinal inválido")
    if not isinstance(validation, list) or not all(
        isinstance(x, str) for x in validation
    ):
        raise AgentOutputError("required_validation de sinal inválido")
    return AIRecommendation(action, why, list(risks), list(validation))


def _parse_estimation_proposal(
    raw: object, known_hashes: set[str]
) -> AIEstimationProposal | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "method",
        "target",
        "evidence_refs",
    }:
        raise AgentOutputError("estimation_proposal inválida")
    method, target, refs = raw.get("method"), raw.get("target"), raw.get("evidence_refs")
    if not isinstance(method, str) or not method.strip():
        raise AgentOutputError("método da estimativa contextual ausente")
    if not isinstance(target, dict) or not all(
        isinstance(key, str)
        and isinstance(value, (str, int, float, bool))
        for key, value in target.items()
    ):
        raise AgentOutputError("target da estimativa contextual inválido")
    if not isinstance(refs, list):
        raise AgentOutputError("evidence_refs da estimativa contextual inválida")
    parsed_refs = []
    for index, ref in enumerate(refs):
        parsed = _parse_evidence_ref(
            ref, known_hashes, f"estimation_proposal evidence_refs[{index}]"
        )
        parsed_refs.append({"sha256": parsed.sha256, "lines": parsed.lines})
    return AIEstimationProposal(method, dict(target), parsed_refs)


def _parse_contextual_estimate(raw: object) -> AIContextualEstimate | None:
    """A faixa como estrutura. O conteúdo dela é conferido pelo motor.

    Aqui só se cobra forma: campo obrigatório presente, tipo certo, faixa
    ordenada e não negativa. Baseline, mecanismo, documentação oficial e plano de
    validação são verificados em `generative_estimation`, contra o inventário —
    fazê-lo aqui exigiria dar ao validador acesso à conta, e ele existe
    justamente para não precisar dela.
    """
    if raw is None:
        return None
    chaves = {
        "billing_mechanism",
        "reasoning",
        "inputs",
        "low",
        "expected",
        "high",
        "assumptions",
        "validation_plan",
        "documentation",
        "missing_evidence",
    }
    if not isinstance(raw, dict) or set(raw) != chaves:
        raise AgentOutputError("faixa contextual com campos ausentes ou não permitidos")
    mecanismo, expressao = raw.get("billing_mechanism"), raw.get("reasoning")
    if not isinstance(mecanismo, str) or not mecanismo.strip():
        raise AgentOutputError("faixa contextual sem mecanismo de cobrança")
    if not isinstance(expressao, str) or not expressao.strip():
        raise AgentOutputError("faixa contextual sem raciocínio declarado")
    entradas = raw.get("inputs")
    if not isinstance(entradas, dict) or not all(
        isinstance(chave, str) and isinstance(valor, (str, int, float, bool))
        for chave, valor in entradas.items()
    ):
        raise AgentOutputError("entradas da faixa contextual inválidas")
    numeros = {}
    for campo in ("low", "expected", "high"):
        valor = raw.get(campo)
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            raise AgentOutputError(f"faixa contextual com {campo} não numérico")
        if valor < 0:
            raise AgentOutputError(f"faixa contextual com {campo} negativo")
        numeros[campo] = float(valor)
    if not numeros["low"] <= numeros["expected"] <= numeros["high"]:
        raise AgentOutputError("faixa contextual fora de ordem")
    listas = {}
    for campo in ("assumptions", "validation_plan", "documentation", "missing_evidence"):
        valor = raw.get(campo)
        if not isinstance(valor, list) or not all(
            isinstance(item, str) for item in valor
        ):
            raise AgentOutputError(f"{campo} da faixa contextual inválido")
        listas[campo] = list(valor)
    return AIContextualEstimate(
        billing_mechanism=mecanismo,
        reasoning=expressao,
        inputs=dict(entradas),
        low=numeros["low"],
        expected=numeros["expected"],
        high=numeros["high"],
        **listas,
    )


def _parse_uncovered_findings(
    raw_findings: object,
    known_hashes: set[str],
    known_rule_ids: set[str],
) -> list[UncoveredFinding]:
    if not isinstance(raw_findings, list):
        raise AgentOutputError("uncovered_findings inválida")
    expected_keys = {
        "title",
        "asset_type",
        "asset_name",
        "evidence_ref",
        "why_not_covered",
        "proposed_rule_id",
        "confidence_basis",
    }
    parsed: list[UncoveredFinding] = []
    seen: set[str] = set()
    for raw in raw_findings:
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise AgentOutputError("campos do achado não coberto ausentes ou não permitidos")
        values = {key: raw.get(key) for key in expected_keys - {"evidence_ref"}}
        for key, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise AgentOutputError(f"achado não coberto com {key} vazio")
        proposed = str(values["proposed_rule_id"])
        # Se já existe regra para o padrão, isso não é lacuna de catálogo — é
        # uma recomendação que deveria estar ligada à oportunidade existente.
        if proposed in known_rule_ids:
            raise AgentOutputError(
                f"proposed_rule_id colide com regra existente: {proposed}"
            )
        if proposed in seen:
            raise AgentOutputError(f"proposed_rule_id duplicado: {proposed}")
        seen.add(proposed)
        parsed.append(
            UncoveredFinding(
                title=str(values["title"]),
                asset_type=str(values["asset_type"]),
                asset_name=str(values["asset_name"]),
                evidence_ref=_parse_evidence_ref(
                    raw.get("evidence_ref"), known_hashes, f"achado {proposed}"
                ),
                why_not_covered=str(values["why_not_covered"]),
                proposed_rule_id=proposed,
                confidence_basis=str(values["confidence_basis"]),
            )
        )
    return parsed
