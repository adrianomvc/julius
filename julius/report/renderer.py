"""Renderização dos artefatos a partir do ReportViewModel."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from julius import __version__
from julius.opportunities.base import Opportunity
from julius.report.view_models import ReportViewModel, manifest_val

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


def render_html(vm: ReportViewModel) -> str:
    env = _env()
    return env.get_template("report.html.j2").render(
        r=vm,
        avatar=avatar_data_uri(),
        manifest_version=manifest_val(vm.manifest, "julius_version") or __version__,
    )


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
        "summary": {
            "total_cost_monthly": vm.total_cost_fmt,
            "identified_monthly": vm.identified_fmt,
            "high_confidence_monthly": vm.high_conf_fmt,
            "realizable_year": vm.realizable_year_fmt,
            "pareto_pct": vm.pareto_pct,
            "pareto_count": vm.pareto_count,
            "recommendation": vm.recommendation,
        },
        "manifest": vm.manifest,
        "opportunities": [asdict(o) for o in opportunities],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def subject(vm: ReportViewModel) -> str:
    return (
        f"Julius · {vm.account_id} · {vm.period} · "
        f"{vm.kpi_actionable} ações · economia est. {vm.identified_fmt}/mês"
    )
