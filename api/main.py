"""API do Futebol de Raízes (Django Ninja), montada em `/api/`.

Três grupos de rotas sobre o mesmo núcleo (docs/CONTRACT.md §4): autenticação
(`/api/auth`), operação (`/api/ops`, sessão + CSRF + permissão) e leitura (sem
login). O stream SSE (`/api/stream`) é uma view assíncrona à parte
(`realtime.views.stream`). Documentação interativa em `/api/docs` (e o esquema em
`/api/openapi.json`) só para a conta de administrador: ela lista as rotas de operação e de
login. Para qualquer outro, inclusive sem login, responde 404 (sem levar ao login nem
revelar que existe); o link fica no índice do admin, também só para o administrador. A
documentação aberta é a da API pública (`/public/v1/docs`), só leitura.
"""

from __future__ import annotations

import json
from functools import wraps

from django.http import Http404
from ninja import NinjaAPI, Swagger
from ninja.openapi.docs import render_template

from accounts.roles import is_administrator

from . import errors
from .auth import router as auth_router
from .ops import router as ops_router
from .read import router as read_router


def administrator_only(view):
    """Docs e openapi.json da API interna: só a conta de administrador; o resto, 404."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_administrator(request.user):
            raise Http404
        return view(request, *args, **kwargs)

    return wrapped


class CsrfSwagger(Swagger):
    """Swagger que manda o token CSRF em todo pedido "Try it out": o login e as
    rotas de operação exigem o header (depois do login, recarregue a página: o
    login troca o token)."""

    def render_page(self, request, api, **kwargs):
        self.settings["url"] = self.get_openapi_url(api, kwargs)
        context = {"swagger_settings": json.dumps(self.settings, indent=1), "api": api, "add_csrf": True}
        return render_template(request, self.template, self.template_cdn, context)


api = NinjaAPI(
    title="Futebol de Raízes · API",
    version="1.0.0",
    description=(
        "Autenticação por sessão (cookie) com CSRF: chame `GET /api/auth/me` (seta o cookie "
        "`csrftoken`) e mande o header `X-CSRFToken` nos POSTs. Erros: "
        '`{"code", "message", "details"}` — 400 invalid_input, 401 not_authenticated, '
        "403 permission_denied/csrf_failed, 404 not_found, 422 regra do jogo."
    ),
    docs=CsrfSwagger(),
    docs_url="/docs",
    docs_decorator=administrator_only,
    urls_namespace="api",
)
errors.register(api)
api.add_router("/auth", auth_router)
api.add_router("/ops", ops_router)
api.add_router("", read_router)
