"""Limite de uso da API pública: requisições por chave por minuto.

Janela fixa de 60 s no cache `default` do Django (o limite por IP e o bloqueio de
login ficam em caches à parte, ver config/settings.py): um contador por chave e por janela,
criado com `cache.add` e somado com `cache.incr` (atômicos no LocMem, Redis e
Memcached). O limite é `ApiKey.rate_limit_per_minute` (0 = o padrão de
`settings.PUBLIC_API["DEFAULT_RATE_LIMIT_PER_MINUTE"]`).

Toda requisição autenticada conta, inclusive as respondidas com 304 ou com erro
de validação. Os headers `X-RateLimit-Limit`, `X-RateLimit-Remaining` e
`X-RateLimit-Reset` (instante Unix, em segundos, em que a janela reabre) saem em
toda resposta de uma chave válida; passou do limite → `429 rate_limited` com
`Retry-After` (segundos).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from ninja.throttling import BaseThrottle

from observability.metrics import metrics

WINDOW_SECONDS = 60
CACHE_PREFIX = "fdr:public_api:rate"


def clock() -> float:
    """Instante atual em segundos Unix (ponto único para os testes)."""
    return time.time()


@dataclass(frozen=True)
class RateLimit:
    """Situação da chave na janela corrente, depois de contar esta requisição."""

    limit: int
    count: int
    reset_at: int  # instante Unix (s) em que a janela reabre
    retry_after: int  # segundos até lá (mínimo 1)

    @property
    def allowed(self) -> bool:
        return self.count <= self.limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)


def limit_for(api_key) -> int:
    return api_key.rate_limit_per_minute or settings.PUBLIC_API["DEFAULT_RATE_LIMIT_PER_MINUTE"]


def hit(api_key) -> RateLimit:
    """Conta uma requisição da chave na janela corrente e devolve a situação."""
    now = clock()
    window = int(now // WINDOW_SECONDS)
    reset_at = (window + 1) * WINDOW_SECONDS
    retry_after = max(1, math.ceil(reset_at - now))
    key = f"{CACHE_PREFIX}:{api_key.pk}:{window}"
    timeout = retry_after + 5  # folga: o contador some sozinho depois da janela
    cache.add(key, 0, timeout=timeout)
    try:
        count = cache.incr(key)
    except ValueError:  # expirou entre o add e o incr (fim exato da janela)
        cache.set(key, 1, timeout=timeout)
        count = 1
    return RateLimit(limit=limit_for(api_key), count=count, reset_at=reset_at, retry_after=retry_after)


def apply_headers(response, state: RateLimit | None) -> None:
    """X-RateLimit-* na resposta (nada sem chave válida)."""
    if state is None:
        return
    response["X-RateLimit-Limit"] = str(state.limit)
    response["X-RateLimit-Remaining"] = str(state.remaining)
    response["X-RateLimit-Reset"] = str(state.reset_at)


class ApiKeyThrottle(BaseThrottle):
    """Throttle do Ninja (roda depois da autenticação, antes da rota).

    A situação fica em `request.rate_limit` (para os headers e o `Retry-After`
    do 429); `wait()` não é usado porque a instância é compartilhada entre
    requisições simultâneas.
    """

    def allow_request(self, request) -> bool:
        api_key = getattr(request, "auth", None)
        if api_key is None:  # pragma: no cover - a autenticação vem antes
            return True
        state = hit(api_key)
        request.rate_limit = state
        if not state.allowed:
            metrics.inc("fdr_public_api_throttled_total")
        return state.allowed
