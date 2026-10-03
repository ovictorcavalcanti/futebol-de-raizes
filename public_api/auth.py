"""Chave da API pública: header `X-API-Key`.

A chave em texto nunca é guardada: a busca é pelo hash SHA-256
(`public_api.models.hash_key`). Chave ausente, desconhecida ou desativada → a
autenticação falha e a API responde `401 {"code": "invalid_api_key"}`.

`last_used_at` é gravado no máximo uma vez por minuto por chave (UPDATE
condicional: duas requisições simultâneas não gravam duas vezes).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Q
from ninja.security import APIKeyHeader

from core import timeutils

from .models import ApiKey, hash_key

HEADER = "X-API-Key"
# Chaves geradas têm ~45 caracteres; nada maior que isso merece o hash.
MAX_KEY_LENGTH = 200
TOUCH_INTERVAL = timedelta(minutes=1)


def find_key(raw: str | None) -> ApiKey | None:
    """Chave ativa correspondente ao texto recebido (ou None)."""
    if not raw or len(raw) > MAX_KEY_LENGTH:
        return None
    return ApiKey.objects.filter(key_hash=hash_key(raw), active=True).first()


def touch(api_key: ApiKey, now: datetime | None = None) -> bool:
    """Grava `last_used_at` se o último registro tem um minuto ou mais. True se gravou."""
    now = now or timeutils.now()
    threshold = now - TOUCH_INTERVAL
    if api_key.last_used_at is not None and api_key.last_used_at > threshold:
        return False
    updated = (
        ApiKey.objects.filter(pk=api_key.pk)
        .filter(Q(last_used_at__isnull=True) | Q(last_used_at__lte=threshold))
        .update(last_used_at=now)
    )
    if updated:
        api_key.last_used_at = now
    return bool(updated)


class ApiKeyAuth(APIKeyHeader):
    """Autenticação do Ninja: devolve a `ApiKey` (vira `request.auth`) ou None (401)."""

    param_name = HEADER
    openapi_description = "Chave da API pública, criada pelo Administrador (Django Admin ou `create_api_key`)."

    def authenticate(self, request, key: str | None) -> ApiKey | None:
        api_key = find_key(key)
        if api_key is not None:
            touch(api_key)
        return api_key
