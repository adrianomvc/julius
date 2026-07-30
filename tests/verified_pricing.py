"""Configuração de preço conferida usada por testes de fórmulas financeiras."""

from dataclasses import replace

from julius.config import DEFAULT_CONFIG, Config


def verified_config(*sections: str) -> Config:
    checked = "2026-07-30"
    return replace(
        DEFAULT_CONFIG,
        pricing=replace(
            DEFAULT_CONFIG.pricing,
            verified=True,
            verified_at=checked,
            verification={
                section: {"verified": True, "verified_at": checked}
                for section in sections
            },
        ),
    )
