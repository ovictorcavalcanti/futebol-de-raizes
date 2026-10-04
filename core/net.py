"""Endereço do cliente para limites de acesso e auditoria.

Usa só `REMOTE_ADDR`. Atrás do Caddy, o uvicorn (`--proxy-headers`) já troca
`REMOTE_ADDR` pelo IP vindo de `X-Forwarded-For`, e o Caddy não repassa esse
header recebido de fora (sem `trusted_proxies`, ele o substitui pelo IP real da
conexão). Ler `X-Forwarded-For` direto aqui deixaria qualquer cliente escolher o
próprio IP e escapar dos limites.
"""

from __future__ import annotations


def client_ip(request) -> str | None:
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None
