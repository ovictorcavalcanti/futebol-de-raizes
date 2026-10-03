"""Sessão, CSRF e permissões das rotas da API.

Login por sessão do Django (cookie) com proteção CSRF: o front lê o cookie
`csrftoken` e manda o header `X-CSRFToken` em todo POST. As views do Ninja são
`csrf_exempt` para o middleware; a checagem é feita aqui, explicitamente.

Ordem das checagens nas rotas de operação (antes de validar o corpo):
1. sem sessão → 401 `not_authenticated` (mesmo sem token CSRF: o front sabe
   que precisa de login, não de token novo);
2. token CSRF ausente ou vencido → 403 `csrf_failed`;
3. perfil sem a permissão da rota → 403 `permission_denied`.
"""

from __future__ import annotations

from django.http import HttpRequest
from ninja.security import SessionAuth
from ninja.utils import check_csrf

from .errors import ApiError

POST_EVENT = "matches.post_event"
VOID_EVENT = "matches.void_event"
CHANGE_STATUS = "matches.change_status"
MANAGE_USERS = "accounts.change_user"
OPS_PERMISSIONS = (POST_EVENT, VOID_EVENT, CHANGE_STATUS)

CSRF_FAILED_MESSAGE = "Sessão expirada ou token de segurança inválido. Recarregue e tente de novo."


def require_csrf(request: HttpRequest) -> None:
    """Confere o token CSRF (header `X-CSRFToken` + cookie) → 403 `csrf_failed`.
    Métodos seguros (GET, HEAD...) passam, como no middleware do Django."""
    if getattr(request, "_ninja_csrf_exempt", False):
        return
    if check_csrf(request) is not None:
        raise ApiError(403, "csrf_failed", CSRF_FAILED_MESSAGE)


class OperatorAuth(SessionAuth):
    """Sessão do Django + CSRF + permissão da rota (basta uma de `any_of`).

    Herda de `SessionAuth` para o OpenAPI mostrar o cookie de sessão; a ordem das
    checagens é a do docstring do módulo (o `SessionAuth` do Ninja confere o CSRF
    antes de saber se há sessão, o que daria 403 a quem só precisa de login).
    """

    def __init__(self, *any_of: str):
        super().__init__(csrf=True)
        self.any_of = any_of

    def __call__(self, request: HttpRequest):
        user = request.user
        if not user.is_authenticated:
            return None  # o Ninja responde 401 (AuthenticationError)
        require_csrf(request)
        if self.any_of and not any(user.has_perm(permission) for permission in self.any_of):
            raise ApiError(
                403,
                "permission_denied",
                "Seu perfil não tem permissão para esta ação.",
                {"required_any": list(self.any_of)},
            )
        return user


def permissions_of(user) -> dict[str, bool]:
    """Booleans de `MeUser.permissions` (a tela do operador mostra só o que pode)."""
    return {
        "post_event": user.has_perm(POST_EVENT),
        "void_event": user.has_perm(VOID_EVENT),
        "change_status": user.has_perm(CHANGE_STATUS),
        "manage_users": user.has_perm(MANAGE_USERS),
        "admin_site": bool(user.is_active and user.is_staff),
    }
