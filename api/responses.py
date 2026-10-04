"""Resposta JSON das rotas da API, com o `Cache-Control` de cada uma.

As leituras devolvem os dicionários prontos dos selectors (docs/CONTRACT.md §3–4):
a resposta sai direto em JSON, sem revalidar o dicionário num schema (os schemas
das rotas documentam o formato no OpenAPI).
"""

from __future__ import annotations

from typing import Any

from django.http import JsonResponse
from ninja.responses import NinjaJSONEncoder

# Dado ao vivo ou da sessão: nada de cache no navegador nem no proxy.
NO_STORE = "no-store"
# Menu de competições: muda raramente (cadastro no admin).
MENU_CACHE = "public, max-age=60"
# Catálogo do operador: só muda com deploy; privado (rota com sessão).
CATALOG_CACHE = "private, max-age=300"


def respond(data: Any, *, status: int = 200, cache: str = NO_STORE) -> JsonResponse:
    """JSON com status e Cache-Control."""
    response = JsonResponse(data, status=status, encoder=NinjaJSONEncoder, safe=False)
    response["Cache-Control"] = cache
    return response
