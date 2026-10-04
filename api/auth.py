"""Autenticação por sessão: `/api/auth/login`, `/api/auth/logout`, `/api/auth/me`.

* `login` é anônimo, mas protegido por CSRF (conferido explicitamente): o front
  chama `GET /api/auth/me` antes, que sempre seta o cookie `csrftoken`. Usa o
  `login` do Django: troca a chave da sessão e o token CSRF. Senha errada e
  usuário inativo respondem igual (401 `invalid_credentials`).
* `logout` encerra a sessão (POST com CSRF; sem sessão também responde `ok`).
* Auditoria: `auth.login`, `auth.logout` e `auth.login_failed` (com o usuário digitado)
  vêm dos sinais de autenticação do Django (`observability.signals`), os mesmos do
  login do admin: aqui nada é gravado à mão (senão sairia em dobro).
* Força bruta: o backend (accounts/backends.py) bloqueia de forma progressiva por
  usuário + IP e por IP; bloqueado, responde 429 `login_locked` com `Retry-After`.
"""

from __future__ import annotations

from django.contrib import auth as django_auth
from django.middleware.csrf import get_token
from ninja import Router

from accounts import throttle
from core.timeutils import iso_utc, now

from .errors import error_response
from .responses import respond
from .schemas import ErrorOut, LoginIn, LoginOut, MeOut, OkOut
from .security import permissions_of, require_csrf

router = Router(tags=["Autenticação"])


def me_user(user) -> dict:
    """MeUser: id, nome, perfis (grupos) e as permissões que a tela usa."""
    return {
        "id": user.pk,
        "username": user.get_username(),
        "name": user.get_full_name() or user.get_username(),
        "roles": list(user.groups.order_by("name").values_list("name", flat=True)),
        "permissions": permissions_of(user),
    }


@router.post(
    "/login",
    response={200: LoginOut, 400: ErrorOut, 401: ErrorOut, 403: ErrorOut, 429: ErrorOut},
    summary="Abre a sessão do operador",
)
def login(request, data: LoginIn):
    """Exige o header `X-CSRFToken` (cookie de `GET /api/auth/me`)."""
    require_csrf(request)
    # Falha → sinal `user_login_failed` (auditoria `auth.login_failed`).
    user = django_auth.authenticate(request, username=data.username, password=data.password)
    lock = getattr(request, "login_lock", None)
    if user is None and lock is not None:
        response = error_response(429, "login_locked", throttle.lock_message(lock), {"retry_after": lock.retry_after})
        response["Retry-After"] = str(lock.retry_after)
        return response
    if user is None:  # senha errada, usuário inexistente ou inativo
        return error_response(401, "invalid_credentials", "Usuário ou senha incorretos.")
    django_auth.login(request, user)  # nova chave de sessão e novo token CSRF (+ `auth.login`)
    return respond({"user": me_user(user), "csrf_token": get_token(request)})


@router.post("/logout", response={200: OkOut, 400: ErrorOut, 403: ErrorOut}, summary="Encerra a sessão")
def logout(request):
    require_csrf(request)
    django_auth.logout(request)  # com sessão → sinal `user_logged_out` (auditoria `auth.logout`)
    return respond({"ok": True})


@router.get("/me", response=MeOut, summary="Usuário logado e suas permissões")
def me(request):
    """Sempre seta o cookie CSRF (o front lê o token dele para os POSTs). `server_time`
    acerta o relógio da tela do operador já no login."""
    user = request.user
    authenticated = bool(user.is_authenticated)
    return respond(
        {
            "authenticated": authenticated,
            "user": me_user(user) if authenticated else None,
            "csrf_token": get_token(request),
            "server_time": iso_utc(now()),
        }
    )
