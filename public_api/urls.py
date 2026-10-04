"""Rotas da API pública (`/public/v1/`), com erros sempre no formato JSON da API.

* Caminho inexistente sob `/public/v1/` (inclusive a raiz) → `404 not_found`.
* Método que a rota não aceita (o Ninja responde 405 em texto puro) → `405
  method_not_allowed` com `details.allowed` e o header `Allow`.
"""

from django.urls import path, re_path

from .api import json_errors, not_found_view, public_api

_patterns, _app_name, _namespace = public_api.urls
for _pattern in _patterns:
    _pattern.callback = json_errors(_pattern.callback)

urlpatterns = [
    path("public/v1/", (_patterns, _app_name, _namespace)),
    # Por último: qualquer outro caminho sob /public/v1/ responde 404 em JSON.
    re_path(r"^public/v1/(?P<rest>.*)$", not_found_view),
]
