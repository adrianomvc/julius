"""Pontuação: transforma um achado em prioridade de execução.

Ganho, dificuldade, confiança e qualidade de evidência são **determinísticos**
— mesma entrada, mesmo score. Nada aqui chama AWS nem lê configuração de
runtime além do `Config` que recebe.
"""

from julius.scoring.gain import build_gain, months_remaining_in_year

__all__ = ["build_gain", "months_remaining_in_year"]
