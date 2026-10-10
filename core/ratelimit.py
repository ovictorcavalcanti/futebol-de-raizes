"""Limite de requisições por IP nas rotas `/api/` (janela fixa de 1 minuto).

Protege o processo ASGI único contra enxurrada de requisições: passou de
`API_RATE_LIMIT_PER_MINUTE` no minuto, responde 429 `rate_limited` com
`Retry-After`, sem chegar à view nem ao banco. 0 desliga. A API pública tem o
limite próprio, por chave (public_api/throttle.py), e o stream tem o limite de
conexões abertas (realtime/views.py).

O IP vem de core.net.client_ip (só REMOTE_ADDR: não dá para forjar pelo header).
O contador fica no cache `ratelimit` do processo — um processo ASGI só —, à parte
para o giro de uma chave por IP por minuto não despejar o bloqueio de login nem o
limite da API pública.
"""

from __future__ import annotations

import time

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.core.cache import caches
from django.http import JsonResponse
from django.utils.connection import ConnectionProxy

from observability.metrics import metrics

from .net import client_ip

PREFIX = "fdr:rl"
WINDOW = 60
cache = ConnectionProxy(caches, "ratelimit")


def _limited(request) -> JsonResponse | None:
    limit = getattr(settings, "API_RATE_LIMIT_PER_MINUTE", 0)
    if not limit or not request.path.startswith("/api/"):
        return None
    ip = client_ip(request) or "desconhecido"
    now = time.time()
    window = int(now // WINDOW)
    key = f"{PREFIX}:{ip}:{window}"
    cache.add(key, 0, WINDOW + 5)
    try:
        count = cache.incr(key)
    except ValueError:  # expirou entre o add e o incr
        cache.set(key, 1, WINDOW + 5)
        count = 1
    if count <= limit:
        return None
    retry_after = max(1, int((window + 1) * WINDOW - now))
    metrics.inc("fdr_rate_limited_total", scope="api")
    response = JsonResponse(
        {
            "code": "rate_limited",
            "message": "Muitas requisições. Tente de novo em instantes.",
            "details": {"limit_per_minute": limit, "retry_after": retry_after},
        },
        status=429,
    )
    response["Retry-After"] = str(retry_after)
    response["Cache-Control"] = "no-store"
    return response


class RateLimitMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        return _limited(request) or self.get_response(request)

    async def __acall__(self, request):
        return _limited(request) or await self.get_response(request)
