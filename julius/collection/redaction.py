"""Remoção de segredos de qualquer texto coletado.

Mora na coleta porque é ali que o risco nasce: um script Glue, uma mensagem de
erro ou um argumento de job podem carregar credencial, e o conteúdo precisa
chegar já redigido a qualquer camada acima. Quem monta contexto para o agente
consome a mesma função, em vez de manter a própria.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*(['\"]?)[^,\s;'\"]+\2"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
)


def redact_secrets(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
