"""Coletor de Glue Interactive Sessions (estrutura básica).

Campos de ociosidade (`idle_hours_per_day`) exigem CloudWatch/atividade — ficam
0 aqui; o detector de sessão ociosa só dispara quando essa métrica for adicionada.
"""

from __future__ import annotations

from julius.inventory.model import InteractiveSession


def collect_sessions(glue_client) -> list[InteractiveSession]:
    out: list[InteractiveSession] = []
    paginator = glue_client.get_paginator("list_sessions")
    for page in paginator.paginate():
        for s in page.get("Sessions", []):
            dpu = int(s.get("MaxCapacity") or s.get("NumberOfWorkers") or 5)
            out.append(
                InteractiveSession(
                    session_id=s.get("Id", "?"),
                    dpu=dpu,
                    idle_timeout_min=int(s.get("IdleTimeout", 2880) or 2880),
                    status=s.get("Status", "READY"),
                    idle_hours_per_day=0.0,  # requer CloudWatch
                    owner_tag=(s.get("Tags", {}) or {}).get("Owner"),
                )
            )
    return out
