"""Formato único de erro da API (docs/CONTRACT.md §4).

Todo erro sai como `{"code", "message", "details"}` (+ `"warnings"` em
`confirmation_required`):

* `400 invalid_input` — formato (validação do Ninja, corpo ilegível, header,
  filtro inválido, `services.InvalidInput`);
* `401 not_authenticated` — sem sessão;
* `403 permission_denied` — perfil sem a permissão da rota; `403 csrf_failed` —
  token CSRF ausente ou vencido (o front renova em `GET /api/auth/me`);
* `404 not_found` — partida, lançamento, competição, fase ou rodada inexistente;
* `405 method_not_allowed` — método que a rota não aceita (`details.allowed`);
* `422 <regra>` — `matches.domain.DomainError` (código, detalhes e avisos do
  domínio) e `standings.domain.ConfigError`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from functools import wraps
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from django.views.decorators.csrf import csrf_exempt
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, AuthorizationError, HttpError, ValidationError

from competitions.models import Competition, Round, Stage
from matches.domain import DomainError
from matches.models import Match, MatchEvent
from matches.services import InvalidInput
from standings.domain import ConfigError

from .responses import respond

STATUS_CODES = {
    400: "invalid_input",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    429: "too_many_requests",
}
STATUS_MESSAGES = {
    400: "Formato inválido: confira os dados enviados.",
    401: "Faça login para continuar.",
    403: "Seu perfil não tem permissão para esta ação.",
    404: "Não encontrado.",
    405: "Método não permitido.",
    429: "Muitas requisições: tente de novo em instantes.",
}
NOT_FOUND_MESSAGES: tuple[tuple[type[ObjectDoesNotExist], str], ...] = (
    (Match.DoesNotExist, "Partida não encontrada."),
    (MatchEvent.DoesNotExist, "Lançamento não encontrado nesta partida."),
    (Competition.DoesNotExist, "Competição não encontrada."),
    (Stage.DoesNotExist, "Fase não encontrada."),
    (Round.DoesNotExist, "Rodada não encontrada."),
)
# Origem do erro de validação do Ninja → prefixo que sai do `field`.
_PARAM_SOURCES = {"body", "query", "path", "header", "cookie", "form", "file"}


class ApiError(Exception):
    """Erro levantado pelas rotas: vira a resposta `{"code","message","details"}`."""

    def __init__(self, status: int, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})


def error_body(code: str, message: str, details: Mapping[str, Any] | None = None, warnings: Iterable = ()) -> dict:
    body: dict[str, Any] = {"code": code, "message": message, "details": dict(details or {})}
    items = [{"code": warning.code, "message": warning.message} for warning in warnings]
    if items:  # só em confirmation_required
        body["warnings"] = items
    return body


def error_response(status: int, code: str, message: str, details: Mapping[str, Any] | None = None, warnings: Iterable = ()):
    return respond(error_body(code, message, details, warnings), status=status)


def invalid_input(field: str, message: str) -> ApiError:
    return ApiError(400, "invalid_input", message, {"field": field})


def _field(loc: Sequence) -> str:
    """("body", "data", "payload", "minute") → "payload.minute"; ("query", "roundId") → "roundId"."""
    parts = [str(part) for part in loc]
    if parts and parts[0] in _PARAM_SOURCES:
        source, parts = parts[0], parts[1:]
        # corpo: o 1º nível é o nome do parâmetro da view (ex.: "data")
        if source == "body" and len(parts) > 1:
            parts = parts[1:]
    return ".".join(parts) or "body"


def _validation_details(errors: Sequence[Mapping]) -> dict:
    items = [{"field": _field(error.get("loc", ())), "message": error.get("msg", ""), "type": error.get("type", "")} for error in errors]
    details: dict[str, Any] = {"errors": items}
    if items:
        details["field"] = items[0]["field"]
    return details


# --- Tratadores ------------------------------------------------------------------------


def _on_api_error(request, exc: ApiError):
    return error_response(exc.status, exc.code, exc.message, exc.details)


def _on_domain_error(request, exc: DomainError):
    return error_response(422, exc.code, exc.message, exc.details, exc.warnings)


def _on_config_error(request, exc: ConfigError):
    return error_response(422, exc.code, exc.message)


def _on_invalid_input(request, exc: InvalidInput):
    return error_response(400, "invalid_input", exc.message, {"field": exc.field})


def _on_validation_error(request, exc: ValidationError):
    details = _validation_details(exc.errors)
    fields = sorted({item["field"] for item in details["errors"]})
    message = f"Dados inválidos: confira o campo {fields[0]}." if len(fields) == 1 else "Dados inválidos: confira os campos."
    return error_response(400, "invalid_input", message, details)


def _on_not_found(request, exc: ObjectDoesNotExist):
    message = next((text for kind, text in NOT_FOUND_MESSAGES if isinstance(exc, kind)), STATUS_MESSAGES[404])
    return error_response(404, "not_found", message)


def _on_http404(request, exc: Http404):
    return error_response(404, "not_found", STATUS_MESSAGES[404])


def _on_authentication_error(request, exc: AuthenticationError):
    return error_response(401, "not_authenticated", STATUS_MESSAGES[401])


def _on_authorization_error(request, exc: AuthorizationError):
    return error_response(403, "permission_denied", STATUS_MESSAGES[403])


def _on_http_error(request, exc: HttpError):
    status = exc.status_code
    message = STATUS_MESSAGES.get(status, str(exc))
    details: dict[str, Any] = {"detail": str(exc)} if str(exc) and str(exc) != message else {}
    if status == 400:
        details.setdefault("field", "body")  # corpo ilegível (JSON inválido): o único 400 do Ninja
    return error_response(status, STATUS_CODES.get(status, "http_error"), message, details)


def json_method_not_allowed(view):
    """Envolve a view de um caminho do Ninja: método não aceito (405, texto puro no Ninja)
    sai no formato de erro da API, com o header `Allow`."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if response.status_code == 405 and "application/json" not in response.get("Content-Type", ""):
            allowed = response.get("Allow", "")
            methods = [method.strip() for method in allowed.split(",") if method.strip()]
            json_response = error_response(405, "method_not_allowed", STATUS_MESSAGES[405], {"allowed": methods})
            json_response["Allow"] = allowed
            return json_response
        return response

    return wrapper


@csrf_exempt
def not_found_view(request, rest: str = ""):
    """Rota inexistente sob `/api/`: 404 no formato de erro da API (não a página HTML)."""
    return error_response(404, "not_found", "Rota da API não encontrada.", {"path": request.path})


def register(api: NinjaAPI) -> None:
    """Liga os tratadores ao NinjaAPI (o mais específico vale, pela MRO)."""
    api.add_exception_handler(ApiError, _on_api_error)
    api.add_exception_handler(DomainError, _on_domain_error)
    api.add_exception_handler(ConfigError, _on_config_error)
    api.add_exception_handler(InvalidInput, _on_invalid_input)
    api.add_exception_handler(ValidationError, _on_validation_error)
    api.add_exception_handler(ObjectDoesNotExist, _on_not_found)
    api.add_exception_handler(Http404, _on_http404)
    api.add_exception_handler(AuthenticationError, _on_authentication_error)
    api.add_exception_handler(AuthorizationError, _on_authorization_error)
    api.add_exception_handler(HttpError, _on_http_error)
