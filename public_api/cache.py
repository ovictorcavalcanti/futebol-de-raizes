"""Cache HTTP da API pública.

Resposta 200: ETag forte (SHA-256 do corpo), `Cache-Control: public,
max-age=<settings.PUBLIC_API["CACHE_MAX_AGE"]>` e `Vary: X-API-Key`. Pedido com
`If-None-Match` igual ao ETag atual → `304 Not Modified` sem corpo (a
requisição conta no limite de uso do mesmo jeito). Erros saem com
`Cache-Control: no-store`.

Por isso as respostas públicas não trazem `server_time`: o corpo só muda quando
o dado muda, e o ETag se repete entre requisições.
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.http import HttpResponseNotModified
from django.utils.cache import patch_vary_headers
from django.utils.http import parse_etags

from .auth import HEADER

NO_STORE = "no-store"


def cache_control() -> str:
    return f"public, max-age={settings.PUBLIC_API['CACHE_MAX_AGE']}"


def etag_for(content: bytes) -> str:
    """ETag forte: muda sempre que um byte do corpo muda."""
    return f'"{hashlib.sha256(content).hexdigest()[:32]}"'


def etag_matches(header: str | None, etag: str) -> bool:
    """If-None-Match usa comparação fraca (RFC 9110 §13.1.2): `W/` é ignorado."""
    if not header:
        return False
    tags = parse_etags(header)
    return "*" in tags or any(tag.removeprefix("W/") == etag for tag in tags)


def _cacheable(response, etag: str):
    response["ETag"] = etag
    response["Cache-Control"] = cache_control()
    patch_vary_headers(response, (HEADER,))
    return response


def finalize(request, response):
    """Aplica o cache HTTP à resposta da rota; devolve a própria resposta ou um 304."""
    if response.status_code != 200 or getattr(response, "streaming", False):
        response["Cache-Control"] = NO_STORE
        return response
    etag = etag_for(response.content)
    if etag_matches(request.headers.get("If-None-Match"), etag):
        return _cacheable(HttpResponseNotModified(), etag)
    return _cacheable(response, etag)
