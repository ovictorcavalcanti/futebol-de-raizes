"""Id de requisição, log de acesso estruturado e métricas HTTP.

O id vem do header `X-Request-ID` (ex.: do proxy) só quando tem 1 a 64 caracteres
de `[A-Za-z0-9._:-]`; senão é gerado (uuid4 hex). Ele volta no header da resposta,
vai em cada linha de log e em `AuditLog.request_id`.

Funciona em modo síncrono e assíncrono (o stream SSE é uma view async e não
pode ser forçado para uma thread).
"""

import re
import time
import uuid

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.utils.functional import empty

from .logging import get_logger, request_id_var, user_var
from .metrics import metrics

log = get_logger("http")

_SKIP_LOG_PREFIXES = ("/static/", "/health", "/metrics")
# X-Request-ID aceito do cliente/proxy: até 64 caracteres seguros (cabe em
# AuditLog.request_id e não injeta nada no header de resposta nem nos logs).
# Fora disso, o id é gerado aqui.
REQUEST_ID_MAX_LENGTH = 64
_VALID_RID = re.compile(rf"[A-Za-z0-9._:-]{{1,{REQUEST_ID_MAX_LENGTH}}}")


def request_id_from(value: str | None) -> str:
    """Id da requisição: o header recebido, se válido; senão um uuid4 hex novo."""
    if value and _VALID_RID.fullmatch(value):
        return value
    return uuid.uuid4().hex


def _username(request) -> str:
    """Usuário da requisição, sem disparar consulta: só lê se a view já carregou."""
    user = getattr(request, "user", None)
    if user is None:
        return ""
    wrapped = getattr(user, "_wrapped", user)
    if wrapped is empty:
        return ""
    return wrapped.username if getattr(wrapped, "is_authenticated", False) else ""


def _route(request) -> str:
    """Rótulo de rota das métricas e do log: o padrão da URL, nunca o caminho concreto.

    A página inicial (`path("")`) vira "/"; arquivos estáticos, servidos pelo WhiteNoise
    antes da resolução de URL, viram "/static/*"; "unmatched" fica só para o que não
    casou com nenhuma rota (404 de verdade).
    """
    static_url = settings.STATIC_URL or ""
    if static_url.startswith("/") and request.path.startswith(static_url):
        return static_url + "*"
    match = getattr(request, "resolver_match", None)
    if match is not None:
        return "/" + (match.route or "").lstrip("/")
    return "unmatched"


class RequestContextMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def _start(self, request):
        rid = request_id_from(request.headers.get("X-Request-ID"))
        request.request_id = rid
        token = request_id_var.set(rid)
        return rid, token, time.perf_counter()

    def _finish(self, request, response, rid, token, started):
        elapsed = time.perf_counter() - started
        response["X-Request-ID"] = rid
        route = _route(request)
        status = getattr(response, "status_code", 0)
        metrics.inc("fdr_http_requests_total", method=request.method, route=route, status=status)
        if not getattr(response, "streaming", False):
            metrics.observe("fdr_http_request_duration_seconds", elapsed, route=route)
        if not request.path.startswith(_SKIP_LOG_PREFIXES):
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.path,
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                    "user": _username(request),
                },
            )
        request_id_var.reset(token)
        user_var.set("")
        return response

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        rid, token, started = self._start(request)
        response = self.get_response(request)
        return self._finish(request, response, rid, token, started)

    async def __acall__(self, request):
        rid, token, started = self._start(request)
        response = await self.get_response(request)
        return self._finish(request, response, rid, token, started)
