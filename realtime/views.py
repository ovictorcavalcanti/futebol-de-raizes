"""Stream SSE único da aplicação: `GET /api/stream?after=N`.

Todas as competições num stream só (um por página). A retomada usa o header
`Last-Event-ID` (reconexão automática do navegador) ou, na primeira conexão e
quando a página recria o `EventSource`, o parâmetro `after` (o `cursor` da
leitura ou o id da última mensagem recebida). Se vierem os dois, vale o header.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing

from django.db import connections
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

from .hub import RETRY_FRAME, hub

MAX_MESSAGE_ID = 2**63 - 1  # bigint do Postgres


class InvalidPosition(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _parse_id(field: str, raw: str) -> int:
    value = raw.strip()
    if not (value.isascii() and value.isdigit()) or len(value) > 19 or int(value) > MAX_MESSAGE_ID:
        raise InvalidPosition(field, f"{field} deve ser um inteiro não negativo.")
    return int(value)


def parse_position(last_event_id: str | None, after: str | None) -> int | None:
    """Id a partir do qual reenviar; None quando não há posição (só ao vivo).

    `Last-Event-ID` vazio é ignorado (o navegador só o envia quando tem um id);
    `after` presente precisa ser um inteiro não negativo.
    """
    if last_event_id is not None and last_event_id.strip():
        return _parse_id("Last-Event-ID", last_event_id)
    if after is not None:
        return _parse_id("after", after)
    return None


def _release_request_thread() -> None:
    """Libera a thread que o Django reserva para cada requisição ASGI.

    O handler ASGI abre um `ThreadSensitiveContext` por requisição, e o código
    síncrono dos middlewares ganha um executor de uma thread que só termina com
    a resposta. Numa conexão SSE, que dura horas, essa thread ficaria parada o
    tempo todo: mil conexões, mil threads. Quando o corpo começa a ser enviado
    os middlewares já rodaram; se o Django precisar de novo (fechamento da
    resposta), o asgiref cria outro executor sob demanda. Uma eventual conexão
    de banco aberta nessa thread é fechada antes de ela sair.
    """
    try:
        from asgiref.sync import SyncToAsync

        context = SyncToAsync.thread_sensitive_context.get(None)
        executor = SyncToAsync.context_to_thread_executor.pop(context, None) if context is not None else None
    except Exception:  # pragma: no cover - API interna do asgiref mudou: só não otimiza
        return
    if executor is not None:
        executor.submit(connections.close_all)
        executor.shutdown(wait=False)


async def _event_stream(after_id: int | None) -> AsyncGenerator[bytes, None]:
    _release_request_thread()
    yield RETRY_FRAME
    # Ao desconectar, o Django cancela este gerador; `aclosing` garante que o
    # assinante sai do hub também quando o fechamento vem pelo `yield`.
    async with aclosing(hub.subscribe(after_id)) as frames:
        async for chunk in frames:
            yield chunk


@require_GET
async def stream(request):
    try:
        after_id = parse_position(request.headers.get("Last-Event-ID"), request.GET.get("after"))
    except InvalidPosition as exc:
        return JsonResponse(
            {"code": "invalid_input", "message": str(exc), "details": {"field": exc.field}},
            status=400,
        )
    response = StreamingHttpResponse(_event_stream(after_id), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # proxies como o nginx não seguram o stream
    return response
