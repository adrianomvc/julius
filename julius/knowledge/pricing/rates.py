"""Tarifas por serviço, carregadas de tabela versionada por região.

Os valores eram literais no código, com um campo `region` que ninguém garantia
ser coerente com eles. Agora a região **vem da tabela**: as duas coisas não têm
como discordar, e atualizar preço é trocar um arquivo de dados — diffável,
auditável, sem tocar em código.

Nenhuma tabela herda preço de outra. Região sem arquivo falha na carga, em vez
de rodar silenciosamente com a tarifa de outro lugar.

Estas tarifas governam apenas o caminho **modelado**. Quando o rateio do Cost
Explorer fecha, a tarifa usada é a implícita na cobrança real, e nada aqui é
consultado.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

TABLES = Path(__file__).resolve().parent / "tables"
DEFAULT_REGION = "sa-east-1"


class UnknownPricingRegionError(RuntimeError):
    """Não existe tabela de preço para a região pedida."""

    def __init__(self, region: str, available: list[str]):
        self.region = region
        super().__init__(
            f"sem tabela de preço para {region!r}; disponíveis: {', '.join(available) or 'nenhuma'}. "
            f"Adicione {region}.toml em knowledge/pricing/tables com uma fonte citável — "
            "herdar preço de outra região produziria número sem procedência."
        )


def available_regions() -> list[str]:
    return sorted(path.stem for path in TABLES.glob("*.toml"))


@cache
def load_table(region: str) -> dict:
    path = TABLES / f"{region}.toml"
    if not path.is_file():
        raise UnknownPricingRegionError(region, available_regions())
    return tomllib.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Pricing:
    """Preços (premissas) de uma região. Construir por `for_region`."""

    region: str = DEFAULT_REGION
    currency: str = "USD"
    version: str = "2.0-usd"
    # `False` enquanto os valores não forem conferidos contra fonte citável.
    # O relatório mostra isso ao lado de qualquer número modelado.
    verified: bool = False
    effective_date: str = ""
    sources: tuple[str, ...] = ()
    # USD/DPU-hora. Fallback versionado; a cobrança permanece no Cost Explorer.
    glue_dpu_hour: float = 0.44
    glue_flex_dpu_hour: float = 0.29
    glue_ray_mdpu_hour: float = 0.44
    # DataBrew usa node-hour, não Glue DPU-hour.
    databrew_node_hour: float = 0.48
    # USD/TB escaneado no Athena.
    athena_per_tb_usd: float = 5.0
    # Step Functions: USD/state transition (Standard) e USD/request (Express).
    sfn_standard_per_transition: float = 0.000025
    sfn_express_per_request: float = 0.000001
    sagemaker_default_hourly: float = 0.18
    sagemaker_instances: dict[str, float] = field(default_factory=dict)
    # S3 — deliberadamente **sem default**. As outras tarifas têm fallback
    # porque governam um custo modelado que já existia; esta governa a
    # comparação entre duas classes de armazenamento, e um número chutado aí
    # não produz uma estimativa imprecisa: produz uma recomendação de mover
    # petabytes para Glacier com uma economia inventada ao lado. Vazio significa
    # que a regra de transição sai como sinal, sem cifra.
    #
    # Popular com `julius pricing refresh --region <regiao>`.
    s3_storage_gb_month: dict[str, float] = field(default_factory=dict)
    s3_request_per_1000: dict[str, float] = field(default_factory=dict)

    @classmethod
    def for_region(cls, region: str = DEFAULT_REGION) -> Pricing:
        table = load_table(region)
        glue = table.get("glue", {})
        athena = table.get("athena", {})
        sfn = table.get("stepfunctions", {})
        sagemaker = table.get("sagemaker", {})
        s3 = table.get("s3", {})
        return cls(
            s3_storage_gb_month={
                nome.removeprefix("storage_"): float(valor)
                for nome, valor in s3.items()
                if nome.startswith("storage_")
            },
            s3_request_per_1000={
                nome.removeprefix("request_").removesuffix("_per_1000"): float(valor)
                for nome, valor in s3.items()
                if nome.startswith("request_")
            },
            # A região é a da tabela, não a pedida: se um dia divergirem, o
            # relatório carimba o que foi realmente usado.
            region=str(table["region"]),
            currency=str(table.get("currency", "USD")),
            version=str(table.get("version", "")),
            verified=bool(table.get("verified", False)),
            effective_date=str(table.get("effective_date", "")),
            sources=tuple(str(item) for item in table.get("sources", ())),
            glue_dpu_hour=float(glue["dpu_hour"]),
            glue_flex_dpu_hour=float(glue["flex_dpu_hour"]),
            glue_ray_mdpu_hour=float(glue["ray_mdpu_hour"]),
            databrew_node_hour=float(glue["databrew_node_hour"]),
            athena_per_tb_usd=float(athena["per_tb"]),
            sfn_standard_per_transition=float(sfn["standard_per_transition"]),
            sfn_express_per_request=float(sfn["express_per_request"]),
            sagemaker_default_hourly=float(sagemaker.get("default_hourly", 0.18)),
            sagemaker_instances={
                str(name): float(value)
                for name, value in (sagemaker.get("instances") or {}).items()
            },
        )

    def glue_rate(self, execution_class: str = "STANDARD") -> float:
        return (
            self.glue_flex_dpu_hour
            if execution_class.upper() == "FLEX"
            else self.glue_dpu_hour
        )

    def sagemaker_hourly(self, instance_type: str) -> float:
        return self.sagemaker_instances.get(
            instance_type, self.sagemaker_default_hourly
        )

    @property
    def has_s3_storage_rates(self) -> bool:
        """A tabela sabe comparar classes de armazenamento nesta região?

        Falso é o estado inicial e é uma resposta legítima: a regra de transição
        descreve o achado e diz o que falta, em vez de anunciar uma economia que
        ninguém conferiu.
        """
        return "standard" in self.s3_storage_gb_month

    def s3_storage_delta(self, target_class: str) -> float | None:
        """Quanto se economiza por GB-mês ao sair de Standard para `target_class`.

        `None` quando qualquer uma das duas tarifas falta — nunca zero, que se
        leria como "não compensa" em vez de "não sabemos".
        """
        origem = self.s3_storage_gb_month.get("standard")
        destino = self.s3_storage_gb_month.get(target_class)
        if origem is None or destino is None:
            return None
        return round(origem - destino, 8)

    def s3_request_cost(self, kind: str, requests: int) -> float | None:
        """Custo de `requests` chamadas de um tipo. `None` se a tarifa falta."""
        tarifa = self.s3_request_per_1000.get(kind)
        if tarifa is None:
            return None
        return round(tarifa * requests / 1000.0, 8)

    @property
    def provenance(self) -> str:
        """Uma linha dizendo de onde a tarifa veio — para o relatório."""
        confidence = "conferida" if self.verified else "não conferida"
        when = f", vigente em {self.effective_date}" if self.effective_date else ""
        return f"tabela {self.region} v{self.version} ({confidence}{when})"
