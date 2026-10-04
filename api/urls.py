from django.urls import path

from .errors import json_method_not_allowed, not_found_view
from .main import api

_patterns, _app_name, _namespace = api.urls
for _pattern in _patterns:
    # 405 do Ninja (texto puro) → formato de erro da API. A view já é csrf_exempt
    # (o CSRF é conferido nas rotas, ver api/security.py); `wraps` mantém o atributo.
    _pattern.callback = json_method_not_allowed(_pattern.callback)

urlpatterns = [
    path("api/", (_patterns, _app_name, _namespace)),
    # Por último: qualquer outra rota sob /api/ responde 404 em JSON.
    path("api/<path:rest>", not_found_view),
]
