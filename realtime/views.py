"""Stream SSE único da aplicação: `GET /api/stream?after=N`.

Todas as competições num stream só (um por página). A retomada usa o header
`Last-Event-ID` (reconexão automática do navegador) ou, na primeira conexão e
quando a página recria o `EventSource`, o parâmetro `after` (o `cursor` da
leitura ou o id da última mensagem recebida). Se vierem os dois, vale o header.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncGenerator
from contextlib import aclosing

from collections import Counter

from django.conf import settings
from django.db import connections
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

from core.net import client_ip
from observability.metrics import metrics

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


# Conexões SSE abertas por IP, neste processo (o único: uvicorn --workers 1).
# Só o event loop mexe no contador (o que vem de outra thread é agendado nele),
# então não há concorrência entre threads.
_open_by_ip: Counter[str] = Counter()


class _StreamSlot:
    """Vaga de uma conexão SSE: reservada na admissão, devolvida uma vez só.

    A reserva acontece na view, sem `await` entre a checagem do teto e a
    contagem, para que requisições simultâneas não passem todas pela mesma
    vaga. A devolução vem do fim do gerador (stream encerrado ou cliente que
    caiu); se o corpo nem começou a ser lido (cancelamento antes do primeiro
    byte), do `close()` da resposta; se a resposta foi descartada sem `close()`
    (cancelada ainda nos middlewares), do coletor de lixo.
    """

    def __init__(self, ip: str):
        self.ip = ip
        self._loop = asyncio.get_running_loop()
        self._held = True
        _open_by_ip[ip] += 1

    def release(self) -> None:
        """Devolve a vaga (idempotente). Roda no event loop."""
        if not self._held:
            return
        self._held = False
        _open_by_ip[self.ip] -= 1
        if _open_by_ip[self.ip] <= 0:
            del _open_by_ip[self.ip]

    def close(self) -> None:
        """Closer da resposta e do coletor de lixo: pode rodar fora do event loop
        (o Django chama `close()` via `sync_to_async`), então só agenda a devolução nele."""
        if not self._held:
            return
        try:
            self._loop.call_soon_threadsafe(self.release)
        except RuntimeError:  # loop já fechado: ninguém mais disputa o contador
            self.release()


def open_streams(ip: str | None = None) -> int:
    return _open_by_ip[ip] if ip is not None else sum(_open_by_ip.values())


def _limit_error(ip: str) -> JsonResponse | None:
    """429 se o IP já tem conexões demais; 503 se o processo está no teto."""
    cfg = settings.REALTIME
    per_ip, total = cfg.get("MAX_STREAMS_PER_IP", 0), cfg.get("MAX_STREAMS", 0)
    if per_ip and _open_by_ip[ip] >= per_ip:
        status, code, message = 429, "too_many_streams", "Conexões ao vivo demais a partir deste endereço."
    elif total and open_streams() >= total:
        status, code, message = 503, "stream_capacity", "Ao vivo lotado agora. Tente de novo em instantes."
    else:
        return None
    metrics.inc("fdr_rate_limited_total", scope="stream")
    response = JsonResponse({"code": code, "message": message, "details": {}}, status=status)
    response["Retry-After"] = "30"
    return response


async def _event_stream(after_id: int | None, slot: _StreamSlot) -> AsyncGenerator[bytes, None]:
    try:
        _release_request_thread()
        yield RETRY_FRAME
        # Ao desconectar, o Django cancela este gerador; `aclosing` garante que o
        # assinante sai do hub também quando o fechamento vem pelo `yield`.
        async with aclosing(hub.subscribe(after_id)) as frames:
            async for chunk in frames:
                yield chunk
    finally:
        slot.release()


@require_GET
async def stream(request):
    try:
        after_id = parse_position(request.headers.get("Last-Event-ID"), request.GET.get("after"))
    except InvalidPosition as exc:
        return JsonResponse(
            {"code": "invalid_input", "message": str(exc), "details": {"field": exc.field}},
            status=400,
        )
    ip = client_ip(request) or "desconhecido"
    limited = _limit_error(ip)
    if limited is not None:
        return limited
    slot = _StreamSlot(ip)  # reservada já aqui, sem await desde a checagem acima
    response = StreamingHttpResponse(_event_stream(after_id, slot), content_type="text/event-stream; charset=utf-8")
    response._resource_closers.append(slot.close)
    weakref.finalize(response, slot.close)
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # proxies como o nginx não seguram o stream
    return response
