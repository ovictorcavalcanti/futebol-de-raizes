"""Id de requisição, log de acesso estruturado e métricas HTTP.

Funciona em modo síncrono e assíncrono (o stream SSE é uma view async e não
pode ser forçado para uma thread).
"""

import time
import uuid

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.utils.functional import empty

from .logging import get_logger, request_id_var, user_var
from .metrics import metrics

log = get_logger("http")

_SKIP_LOG_PREFIXES = ("/static/", "/health", "/metrics")


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
    match = getattr(request, "resolver_match", None)
    if match is not None and match.route:
        return "/" + match.route.lstrip("/")
    return "unmatched"


class RequestContextMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def _start(self, request):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
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
