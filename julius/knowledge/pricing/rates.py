"""Tarifas por serviço, em USD. Premissas explícitas e versionadas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pricing:
    """Preços (premissas) por região. Ajustáveis via config."""

    region: str = "sa-east-1"
    currency: str = "USD"
    version: str = "2.0-usd"
    # USD/DPU-hora. Fallback versionado; a cobrança permanece no Cost Explorer.
    glue_dpu_hour: float = 0.44
    glue_flex_dpu_hour: float = 0.29
    glue_ray_mdpu_hour: float = 0.44
    # DataBrew usa node-hour, não Glue DPU-hour.
    databrew_node_hour: float = 0.48
    # USD/TB escaneado no Athena (fallback versionado; a cobrança real vem do
    # Cost Explorer quando a coleta é reconciliada).
    athena_per_tb_usd: float = 5.0
    # Step Functions: USD/state transition (Standard) e USD/request (Express).
    sfn_standard_per_transition: float = 0.000025
    sfn_express_per_request: float = 0.000001

    def glue_rate(self, execution_class: str = "STANDARD") -> float:
        return (
            self.glue_flex_dpu_hour
            if execution_class.upper() == "FLEX"
            else self.glue_dpu_hour
        )
