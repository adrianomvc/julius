"""Renderização dos artefatos a partir do ReportViewModel."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from julius import __version__
from julius.findings.opportunity import Opportunity
from julius.reporting import design, design_view
from julius.reporting.view_models import ReportViewModel, manifest_val

_TEMPLATES = Path(__file__).parent / "templates"
_ASSETS = Path(__file__).parent / "assets"


@lru_cache(maxsize=1)
def avatar_data_uri() -> str:
    """Avatar do Julius embutido em base64 (não depende de host externo,
    que clientes de e-mail bloqueiam)."""
    png = _ASSETS / "julius-avatar.png"
    if not png.exists():
        return ""
    data = base64.b64encode(png.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(vm: ReportViewModel, *, variant: str = design.SCREEN) -> str:
    """O relatório desenhado, alimentado pelo view model.

    O template vem de `templates/design/` e não é editado aqui: `design.load`
    traduz a sintaxe do editor na carga, e o adaptador entrega os nomes que o
    desenho pede. Trocar o desenho por uma versão nova é trocar o arquivo.
    """
    version = manifest_val(vm.manifest, "julius_version") or __version__
    fonte = design.inline_assets(
        design.load(variant), avatar=avatar_data_uri(), fonts=design.font_face_css()
    )
    return _env().from_string(fonte).render(design_view.build(vm, version=version))


def render_email(vm: ReportViewModel, report_url: str | None = None) -> tuple[str, str]:
    """Renderiza email.html + email.txt.

    `report_url`: link real (http/https) ou caminho local que funcione ao abrir
    o arquivo. Se `None`, o relatório completo vai como **anexo** e o e-mail
    referencia o anexo em vez de um botão-link sem destino.
    """
    env = _env()
    subj = subject(vm)
    version = manifest_val(vm.manifest, "julius_version") or __version__
    html = env.get_template("email.html.j2").render(
        r=vm, report_url=report_url, subject=subj, avatar=avatar_data_uri(), version=version
    )
    text = env.get_template("email.txt.j2").render(r=vm, report_url=report_url, subject=subj)
    return html, text


def render_json(vm: ReportViewModel, opportunities: list[Opportunity]) -> str:
    payload = {
        "account": vm.account_id,
        "period": vm.period,
        "scan_id": vm.scan_id,
        "currency": vm.currency,
        "summary": {
            "billing_cost_mtd": vm.total_cost_fmt,
            "identified_monthly": vm.identified_fmt,
            "technical_identified_monthly": vm.technical_identified_fmt,
            "high_confidence_monthly": vm.high_conf_fmt,
            "realizable_year": vm.realizable_year_fmt,
            "pareto_pct": vm.pareto_pct,
            "pareto_count": vm.pareto_count,
            "recommendation": vm.recommendation,
            "committed_monthly": vm.committed_fmt,
            "realized_monthly": vm.realized_fmt,
            "realization_rate": vm.realization_rate_pct,
        },
        "lifecycle": {
            "lead_times": vm.lifecycle_lead_times,
            "diff_events": vm.diff_events,
            "validated_results": [asdict(result) for result in vm.previous_results],
        },
        # Fora de `ai_analysis` porque o sinal existe com ou sem provedor: o
        # scanner o produz no scan, e o veredito é o que pode não vir.
        "signals": vm.signals,
        "ai_analysis": {
            "source": "Devin" if vm.ai_summary else None,
            "executive_summary": vm.ai_summary,
            "implementation_order": vm.ai_implementation_order,
            "recommendations": vm.ai_recommendations,
            "coverage": vm.ai_coverage,
            "signal_verdicts": vm.ai_signal_verdicts,
            "uncovered_findings": vm.ai_uncovered_findings,
            "investigations": vm.ai_investigations,
        },
        "athena": {
            "coverage": vm.athena_coverage,
            "query_patterns": vm.athena_queries,
            "actor_usage": vm.athena_actors,
            "gaps": vm.athena_gaps,
        },
        # O HTML desenhado é o documento do analista e não tem apêndice
        # técnico. Nada do que saía lá pode sumir: o JSON passa a ser o registro
        # completo do scan — é dele que a auditoria e o Devin leem.
        "glue_cost": vm.glue_cost,
        "s3_cost": vm.s3_cost,
        "producer_candidates": [asdict(pc) for pc in vm.producers],
        "integrity": {
            "inventory": vm.inventory_integrity,
            "rule_families_without_evidence": vm.rule_families_without_evidence,
        },
        "manifest": vm.manifest,
        "collection_health": {
            "status": vm.collection_status,
            "label": vm.collection_status_label,
            "summary": vm.collection_health_summary,
            "sources": vm.collection_health,
        },
        "process_costs": vm.process_costs,
        "opportunities": [
            {
                **asdict(o),
                "classification": (
                    "validated_opportunity"
                    if o.status == "validated"
                    else "modeled_opportunity"
                ),
            }
            for o in opportunities
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def subject(vm: ReportViewModel) -> str:
    return (
        f"Julius · {vm.account_id} · {vm.period} · "
        f"{vm.kpi_actionable} ações · economia est. {vm.identified_fmt}/mês"
    )
