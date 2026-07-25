"""Coleta: o que é lido da AWS e como isso vira inventário normalizado.

Três responsabilidades, uma por subpacote:

- `collectors/` fala com a AWS. boto3 é dependência opcional
  (`pip install julius[aws]`) e estes módulos só são importados quando a coleta
  ao vivo é pedida — o caminho offline não depende dele.
- `normalizers/` converte entre o dataset exportado e o inventário.
- `health/` registra a telemetria sanitizada de cada fonte.

`models/` guarda os dataclasses que os dois caminhos preenchem, e `window.py` a
janela de análise que todos compartilham. Nada aqui importa conhecimento de
domínio, pontuação ou relatório: a seta aponta só para fora.
"""

from julius.collection.window import AnalysisWindow, BillingMonth

__all__ = ["AnalysisWindow", "BillingMonth"]
