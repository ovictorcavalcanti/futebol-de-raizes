"""Publicador do stream SSE: um hub por processo, atendendo todas as conexões.

Fluxo: a escrita grava a mudança e a mensagem do outbox na mesma transação
(`core.locks.locked_atomic` + `realtime.outbox.enqueue`). Depois do commit,
`enqueue` acorda o hub (`wake_threadsafe`); se a escrita veio de outro processo
(um script, por exemplo), o polling de segurança a encontra. O hub busca as
linhas novas com UMA consulta por lote, monta cada quadro SSE UMA vez (já em
bytes) e entrega o mesmo objeto a todas as filas.

Garantias para quem assina (`subscribe`):

* sem buracos e sem duplicatas: a fila do assinante é registrada ANTES de ler
  o estado do hub; o reenvio cobre `(after, last_id]` e a fila cobre o resto,
  descartando ids já enviados;
* a trava de escrita faz a ordem dos ids ser a ordem dos commits, então buscar
  `id > last_id` nunca deixa para trás uma linha que ainda vai aparecer;
* assinante lento (fila cheia) é desconectado; o navegador volta com
  `Last-Event-ID` e recebe o que perdeu;
* posição à frente do hub (cursor lido antes da publicação) vale: a fila
  descarta o que o cliente já tem; posição além do maior id do banco (banco
  recriado) vale como esse maior id, para o stream não ficar mudo.

Otimizações para muitos clientes:

* buffer circular com os últimos quadros publicados: a reconexão comum (celular
  que voltou do segundo plano, queda de rede) é atendida da memória, sem banco;
  o buffer segue a retenção do outbox (o que a limpeza apaga do banco sai da
  memória também), para nunca reenviar mensagem vencida;
* um lote vira um único `bytes` compartilhado; o assinante em dia escreve esse
  objeto direto, sem cópia nem serialização por conexão;
* todo acesso ao banco do hub roda numa thread própria, com UMA conexão
  persistente verificada a cada uso; as conexões SSE não seguram conexão de
  banco enquanto esperam;
* sem assinantes, o polling de segurança fica mais espaçado.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import time
from collections import Counter, deque
from collections.abc import AsyncGenerator, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from typing import Any, NamedTuple, TypeVar

from django.conf import settings
from django.db import InterfaceError, OperationalError, connections
from django.db.models import Count

from core.timeutils import iso_utc, now
from observability.logging import get_logger
from observability.metrics import metrics

from . import outbox
from .models import Outbox

log = get_logger("realtime")

T = TypeVar("T")

RETRY_FRAME = b"retry: 3000\n\n"
FETCH_BATCH = 500  # linhas por consulta, na publicação e no reenvio
REPLAY_CHUNK_BYTES = 64 * 1024  # tamanho máximo de cada escrita no reenvio
PURGE_EVERY = 3600.0  # segundos entre limpezas do outbox (24 h de retenção)
IDLE_POLL_INTERVAL = 30.0  # polling de segurança quando ninguém está conectado
DEFAULT_BUFFER_FRAMES = 2048  # REALTIME["REPLAY_BUFFER_FRAMES"] (opcional)
DEFAULT_BUFFER_BYTES = 8 * 1024 * 1024  # REALTIME["REPLAY_BUFFER_BYTES"] (opcional)


# --- Quadros SSE ----------------------------------------------------------------


def _dumps(data: Any) -> str:
    # Compacto e sem quebra de linha (o JSON escapa \n e \r dentro das strings).
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def format_frame(message_id: int, topic: str, payload: Any) -> bytes:
    """Quadro de uma mensagem do outbox: `id`, `event` (tópico) e `data` (JSON)."""
    return f"id: {message_id}\nevent: {topic}\ndata: {_dumps(payload)}\n\n".encode()


def format_ping(at: datetime | None = None) -> bytes:
    """Quadro `ping`: sem id e fora do outbox; leva a hora do servidor em UTC."""
    return f"event: ping\ndata: {_dumps({'server_time': iso_utc(at or now())})}\n\n".encode()


def _chunked(frames: Iterable[tuple[int, bytes]], limit: int = REPLAY_CHUNK_BYTES) -> Iterable[tuple[bytes, int]]:
    """Une quadros em blocos de até `limit` bytes (menos escritas no reenvio).

    Devolve pares (bloco, id do último quadro do bloco).
    """
    parts: list[bytes] = []
    size = 0
    last_id = 0
    for message_id, frame in frames:
        if parts and size + len(frame) > limit:
            yield b"".join(parts), last_id
            parts, size = [], 0
        parts.append(frame)
        size += len(frame)
        last_id = message_id
    if parts:
        yield (parts[0] if len(parts) == 1 else b"".join(parts)), last_id


# --- Acesso ao banco (sempre fora do laço de eventos) -----------------------------

Row = tuple[int, bytes, datetime, str]  # id, quadro, created_at, tópico


def _rows_to_frames(rows: Iterable[tuple[int, str, Any, datetime]]) -> list[Row]:
    return [(row_id, format_frame(row_id, topic, payload), created_at, topic) for row_id, topic, payload, created_at in rows]


def _fetch_frames(after_id: int, upto_id: int | None, limit: int) -> list[Row]:
    """Linhas com `after_id < id <= upto_id`, em ordem de id, já como quadros."""
    queryset = Outbox.objects.filter(id__gt=after_id)
    if upto_id is not None:
        queryset = queryset.filter(id__lte=upto_id)
    rows = queryset.order_by("id").values_list("id", "topic", "payload", "created_at")[:limit]
    return _rows_to_frames(rows)


def _fetch_and_mark(after_id: int, limit: int) -> list[Row]:
    """Lote a publicar (uma consulta) e marcação de `published_at` em bloco (um UPDATE)."""
    rows = _fetch_frames(after_id, None, limit)
    if rows:
        Outbox.objects.filter(id__gte=rows[0][0], id__lte=rows[-1][0], published_at__isnull=True).update(published_at=now())
    return rows


Buffered = tuple[int, bytes, datetime]  # id, quadro, created_at


class _Snapshot(NamedTuple):
    max_id: int
    frames: list[Buffered]
    complete: bool  # a tabela inteira coube no buffer
    newly_published: dict[str, int]  # por tópico: linhas que só agora ganharam published_at


def _load_recent(limit: int) -> _Snapshot:
    """Estado inicial do hub: maior id e as `limit` mensagens mais recentes.

    O maior id sai da MESMA consulta das linhas: uma segunda consulta poderia
    ver uma mensagem gravada no meio e dá-la como publicada sem tê-la entregue.

    As linhas até o maior id passam a valer como publicadas (quem tem posição
    as recebe pelo reenvio): `published_at` é marcado nas que ainda não tinham,
    como as gravadas com o processo parado ou antes do primeiro assinante.
    """
    if limit <= 0:
        max_id = outbox.current_cursor()
        frames: list[Buffered] = []
        complete = False
    else:
        rows = list(Outbox.objects.order_by("-id").values_list("id", "topic", "payload", "created_at")[:limit])
        rows.reverse()
        frames = [(row_id, frame, created_at) for row_id, frame, created_at, _ in _rows_to_frames(rows)]
        max_id = frames[-1][0] if frames else 0  # tabela vazia: o próximo ciclo pega tudo o que surgir
        complete = len(rows) < limit
    newly_published: dict[str, int] = {}
    if max_id:
        unpublished = Outbox.objects.filter(id__lte=max_id, published_at__isnull=True)
        newly_published = dict(unpublished.order_by().values_list("topic").annotate(n=Count("id")))
        if newly_published:
            unpublished.update(published_at=now())
    return _Snapshot(max_id, frames, complete, newly_published)


def _retention_cutoff() -> datetime:
    """Instante antes do qual a mensagem venceu (retenção do outbox, 24 h)."""
    return now() - timedelta(hours=settings.REALTIME["OUTBOX_RETENTION_HOURS"])


def _close_quietly(alias: str = "default") -> None:
    with contextlib.suppress(Exception):  # conexão já quebrada
        connections[alias].close()


def _call_with_healthy_connection(fn: Callable[..., T], *args: Any) -> T:
    """Roda `fn` na thread do hub, que mantém UMA conexão persistente.

    `close_old_connections()` fecharia a conexão a cada lote (CONN_MAX_AGE=0);
    aqui ela fica aberta e só é descartada quando deixa de funcionar. Depois de
    um erro, a conexão é testada; se a consulta falhar por conexão, ela é fechada
    e o próximo ciclo do hub reconecta.
    """
    conn = connections["default"]
    if conn.connection is not None and conn.errors_occurred:
        if conn.is_usable():
            conn.errors_occurred = False
        else:
            _close_quietly()
    try:
        return fn(*args)
    except (OperationalError, InterfaceError):
        _close_quietly()
        raise


def _call_and_close(fn: Callable[..., T], *args: Any) -> T:
    """Consulta avulsa que não deixa conexão aberta na thread (modo sem hub)."""
    try:
        return fn(*args)
    finally:
        _close_quietly()


async def _db_once(fn: Callable[..., T], *args: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, partial(_call_and_close, fn, *args))


# --- Assinantes -------------------------------------------------------------------


class _Batch(NamedTuple):
    """Lote entregue às filas. Um único objeto, compartilhado por todos."""

    first_id: int | None  # None: ping
    last_id: int | None
    data: bytes  # quadros do lote já unidos
    frames: tuple[tuple[int, bytes], ...]


class _Subscriber:
    """Fila limitada de lotes de uma conexão, com um único `Future` de espera."""

    __slots__ = ("_waiter", "closed", "limit", "pending")

    def __init__(self, limit: int):
        self.pending: deque[_Batch] = deque()
        self.limit = max(1, limit)
        self.closed = False
        self._waiter: asyncio.Future[None] | None = None

    def push(self, batch: _Batch) -> bool:
        if len(self.pending) >= self.limit:
            return False
        self.pending.append(batch)
        self._wake()
        return True

    def close(self) -> None:
        # O que não foi enviado é descartado: o navegador volta com Last-Event-ID.
        self.closed = True
        self.pending.clear()
        self._wake()

    def _wake(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def wait(self) -> None:
        if self.pending or self.closed:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiter = waiter
        try:
            await waiter
        finally:
            self._waiter = None


# --- Hub --------------------------------------------------------------------------


class Hub:
    """Publicador do processo. Inicia sozinho no laço de eventos, no primeiro assinante."""

    def __init__(self) -> None:
        self._reset_state()

    def _reset_state(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._ready: asyncio.Event | None = None
        self._publisher: asyncio.Task[None] | None = None
        self._pinger: asyncio.Task[None] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._subscribers: set[_Subscriber] = set()
        self._last_id = 0
        self._start_id = 0  # maior id no início do hub ("ao vivo" de quem chegou antes)
        # Buffer: todas as mensagens com id em (_buffer_floor, _last_id] estão aqui.
        self._buffer: deque[Buffered] = deque()
        self._buffer_bytes = 0
        self._buffer_floor = 0
        self._buffer_max_frames = DEFAULT_BUFFER_FRAMES
        self._buffer_max_bytes = DEFAULT_BUFFER_BYTES
        self._next_purge = 0.0

    # -- Estado público ---------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_id(self) -> int:
        """Maior id já publicado (0 antes de iniciar)."""
        return self._last_id

    @property
    def running(self) -> bool:
        return self._publisher is not None and not self._publisher.done()

    @property
    def ready(self) -> bool:
        """Retrato inicial feito: daqui em diante, mensagem nova sai pela publicação ao vivo."""
        return self.running and self._ready is not None and self._ready.is_set()

    def wake_threadsafe(self) -> None:
        """Acorda o hub; pode ser chamada de qualquer thread (on_commit do enqueue).

        Sem hub iniciado, não faz nada: as mensagens ficam no outbox.
        """
        loop, event = self._loop, self._wake_event
        if loop is None or event is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # laço encerrado entre a checagem e a chamada
            loop.call_soon_threadsafe(event.set)

    # -- Ciclo de vida ----------------------------------------------------------

    def _ensure_started(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            self._abandon()  # laço anterior encerrado (testes, recarga)
        if self._loop is None:
            config = settings.REALTIME
            self._loop = loop
            self._wake_event = asyncio.Event()
            self._ready = asyncio.Event()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fdr-hub-db")
            self._buffer_max_frames = max(0, int(config.get("REPLAY_BUFFER_FRAMES", DEFAULT_BUFFER_FRAMES)))
            self._buffer_max_bytes = max(0, int(config.get("REPLAY_BUFFER_BYTES", DEFAULT_BUFFER_BYTES)))
        # Tarefas em contexto limpo: não herdam id de requisição nem o
        # ThreadSensitiveContext da primeira conexão.
        if self._publisher is None or self._publisher.done():
            self._publisher = loop.create_task(self._run(), name="fdr-hub-publisher", context=contextvars.Context())
        if self._pinger is None or self._pinger.done():
            self._pinger = loop.create_task(self._ping_forever(), name="fdr-hub-ping", context=contextvars.Context())

    def _abandon(self) -> None:
        """Descarta o estado ligado a um laço que não existe mais."""
        executor = self._executor
        if executor is not None:
            executor.submit(connections.close_all)
            executor.shutdown(wait=False)
        self._reset_state()

    async def stop(self) -> None:
        """Para o hub: encerra assinantes, tarefas e a conexão de banco (testes)."""
        loop = self._loop
        if loop is None:
            return
        if loop is not asyncio.get_running_loop():
            self._abandon()
            return
        tasks = [task for task in (self._publisher, self._pinger) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for subscriber in list(self._subscribers):
            subscriber.close()
        if self._ready is not None:
            self._ready.set()  # libera quem esperava a partida; a fila já está fechada
        executor = self._executor
        if executor is not None:
            await loop.run_in_executor(executor, connections.close_all)
            executor.shutdown(wait=False)
        self._reset_state()
        metrics.set_gauge("fdr_sse_connections", 0)

    async def _db(self, fn: Callable[..., T], *args: Any) -> T:
        assert self._loop is not None
        return await self._loop.run_in_executor(self._executor, partial(_call_with_healthy_connection, fn, *args))

    async def _run(self) -> None:
        poll = float(settings.REALTIME["POLL_INTERVAL"])
        assert self._ready is not None
        # Recriada depois de iniciada, a tarefa NÃO refaz o retrato: ele
        # pularia mensagens que os assinantes já em modo ao vivo esperam.
        while not self._ready.is_set():
            try:
                await self._bootstrap()
            except Exception:
                log.exception("hub: falha ao iniciar; nova tentativa em seguida")
                await asyncio.sleep(poll)
        assert self._wake_event is not None
        while True:
            timeout = poll if self._subscribers else max(poll, IDLE_POLL_INTERVAL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), timeout)
            # Limpa ANTES de consultar: um aviso que chegue durante a consulta
            # garante mais uma volta.
            self._wake_event.clear()
            try:
                await self._publish_pending()
                await self._maybe_purge()
            except Exception:
                log.exception("hub: falha ao publicar; nova tentativa no próximo ciclo")
                await asyncio.sleep(poll)

    async def _bootstrap(self) -> None:
        # Limpa antes de carregar: o buffer nasce sem mensagem vencida.
        await self._purge()
        max_id, frames, complete, newly_published = await self._db(_load_recent, self._buffer_max_frames)
        for topic, count in newly_published.items():
            metrics.inc("fdr_stream_messages_published_total", count, topic=topic)
        self._last_id = self._start_id = max_id
        self._buffer = deque(frames)
        self._buffer_bytes = sum(len(item[1]) for item in frames)
        if complete:
            self._buffer_floor = 0  # a tabela inteira está no buffer
        elif frames:
            self._buffer_floor = frames[0][0] - 1
        else:
            self._buffer_floor = max_id
        self._trim_buffer()
        self._expire_buffer(_retention_cutoff())
        assert self._ready is not None
        self._ready.set()
        log.info("hub: iniciado", extra={"last_id": max_id, "buffered": len(self._buffer)})

    async def _publish_pending(self) -> None:
        while True:
            rows = await self._db(_fetch_and_mark, self._last_id, FETCH_BATCH)
            if not rows:
                return
            self._publish(rows)
            if len(rows) < FETCH_BATCH:
                return

    def _publish(self, rows: list[Row]) -> None:
        """Entrega um lote a todos (síncrono: nenhuma espera entre fila, buffer e last_id)."""
        frames = tuple((row_id, frame) for row_id, frame, _, _ in rows)
        data = frames[0][1] if len(frames) == 1 else b"".join(frame for _, frame in frames)
        self._broadcast(_Batch(frames[0][0], frames[-1][0], data, frames))
        for row_id, frame, created_at, _ in rows:
            self._buffer.append((row_id, frame, created_at))
            self._buffer_bytes += len(frame)
        self._last_id = frames[-1][0]
        self._trim_buffer()
        for topic, count in Counter(row[3] for row in rows).items():
            metrics.inc("fdr_stream_messages_published_total", count, topic=topic)
        metrics.set_gauge("fdr_outbox_lag_seconds", max(0.0, (now() - rows[-1][2]).total_seconds()))

    def _drop_oldest(self) -> None:
        old_id, old_frame, _ = self._buffer.popleft()
        self._buffer_bytes -= len(old_frame)
        self._buffer_floor = old_id

    def _trim_buffer(self) -> None:
        while self._buffer and (len(self._buffer) > self._buffer_max_frames or self._buffer_bytes > self._buffer_max_bytes):
            self._drop_oldest()

    def _expire_buffer(self, cutoff: datetime) -> None:
        """Tira do buffer o que venceu: a memória não reenvia o que o banco já apagou."""
        while self._buffer and self._buffer[0][2] < cutoff:
            self._drop_oldest()

    def _buffered_after(self, after_id: int) -> list[tuple[int, bytes]]:
        items: list[tuple[int, bytes]] = []
        for item in reversed(self._buffer):
            if item[0] <= after_id:
                break
            items.append((item[0], item[1]))
        items.reverse()
        return items

    async def _purge(self) -> None:
        """Apaga do outbox (e do buffer) as mensagens vencidas; uma falha só é registrada."""
        self._next_purge = time.monotonic() + PURGE_EVERY
        try:
            deleted = await self._db(outbox.purge_old)
        except Exception:
            log.exception("outbox: falha na limpeza; nova tentativa na próxima hora")
            return
        # Corte calculado DEPOIS da limpeza: o buffer perde no mínimo o que o banco perdeu.
        self._expire_buffer(_retention_cutoff())
        if deleted:
            log.info("outbox: mensagens antigas apagadas", extra={"deleted": deleted})

    async def _maybe_purge(self) -> None:
        if time.monotonic() >= self._next_purge:
            await self._purge()

    async def _ping_forever(self) -> None:
        interval = float(settings.REALTIME["PING_INTERVAL"])
        while True:
            await asyncio.sleep(interval)
            if self._subscribers:
                self._broadcast(_Batch(None, None, format_ping(), ()))

    # -- Assinantes ---------------------------------------------------------------

    def _broadcast(self, batch: _Batch) -> None:
        dropped = [subscriber for subscriber in self._subscribers if not subscriber.push(batch)]
        for subscriber in dropped:
            self._unregister(subscriber)
            subscriber.close()
        if dropped:
            log.warning("hub: assinantes lentos desconectados", extra={"dropped": len(dropped)})

    def _register(self, subscriber: _Subscriber) -> None:
        was_idle = not self._subscribers
        self._subscribers.add(subscriber)
        metrics.inc("fdr_sse_connections_total")
        metrics.set_gauge("fdr_sse_connections", len(self._subscribers))
        if was_idle and self._wake_event is not None:
            self._wake_event.set()  # saindo do polling espaçado: busca já

    def _unregister(self, subscriber: _Subscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.discard(subscriber)
            metrics.set_gauge("fdr_sse_connections", len(self._subscribers))

    async def subscribe(self, after_id: int | None = None) -> AsyncGenerator[bytes, None]:
        """Quadros SSE (bytes) a partir de `after_id`; None = só o que vier daqui em diante.

        Abre com um `ping`. Termina quando o assinante fica para trás (fila cheia)
        ou quando o reenvio falha; nos dois casos o navegador reconecta sozinho.
        """
        if not settings.REALTIME.get("HUB_ENABLED", True):
            async for chunk in _poll_stream(after_id):
                yield chunk
            return
        self._ensure_started()
        subscriber = _Subscriber(int(settings.REALTIME["SUBSCRIBER_QUEUE_SIZE"]))
        # 1) Registra a fila primeiro: tudo o que for publicado daqui em diante cai nela.
        self._register(subscriber)
        ready = self._ready
        assert ready is not None
        if after_id is None and ready.is_set():
            after_id = self._last_id  # sem posição: "ao vivo" vale a partir de agora
        try:
            yield format_ping()
            if not ready.is_set():
                await ready.wait()
            if after_id is None:
                # Chegou com o hub iniciando: "ao vivo" vale a partir do retrato
                # inicial; o que foi publicado enquanto esperava é reenviado.
                after_id = self._start_id
            # 2) Retrato síncrono do hub (sem await entre as leituras).
            upto = self._last_id
            last_sent = after_id
            if last_sent > upto:
                last_sent = await self._clamp_position(last_sent, upto)
                upto = self._last_id  # novo retrato, depois da espera
            if last_sent < upto:
                floor = self._buffer_floor
                buffered = self._buffered_after(last_sent)
                # 3) Reenvio: do banco o que já saiu do buffer, depois da memória.
                if last_sent < floor:
                    try:
                        async for chunk, last_id in self._replay_from_db(last_sent, floor):
                            yield chunk
                            last_sent = last_id
                    except Exception:
                        log.exception("hub: falha no reenvio; a conexão será refeita pelo navegador")
                        return
                for chunk, last_id in _chunked(buffered):
                    yield chunk
                    last_sent = last_id
            # 4) Ao vivo: descarta o que já foi enviado no reenvio.
            while True:
                parts: list[bytes] = []
                pending = subscriber.pending
                while pending:
                    batch = pending.popleft()
                    first, last = batch.first_id, batch.last_id
                    if first is None or last is None:  # ping
                        parts.append(batch.data)
                    elif first > last_sent:  # caso comum: o lote inteiro é novo
                        parts.append(batch.data)
                        last_sent = last
                    elif last > last_sent:  # lote em parte já enviado no reenvio
                        for message_id, frame in batch.frames:
                            if message_id > last_sent:
                                parts.append(frame)
                                last_sent = message_id
                if parts:
                    yield parts[0] if len(parts) == 1 else b"".join(parts)
                    continue
                if subscriber.closed:
                    return
                await subscriber.wait()
        finally:
            self._unregister(subscriber)

    async def _clamp_position(self, position: int, upto: int) -> int:
        """Posição à frente do hub: confere no banco se ela existe.

        O normal é a leitura (`cursor`) ter visto um commit que o hub ainda vai
        publicar: a posição vale e a fila descarta o que o cliente já tem. Se o
        id passa do maior id do banco, ele veio de outro banco (recriado, por
        exemplo): sem o ajuste, o cliente ficaria sem mensagem nenhuma até os
        ids novos alcançarem o dele, com os pings mantendo a conexão de pé.
        """
        try:
            db_max = await self._db(outbox.current_cursor)
        except Exception:
            log.exception("hub: falha ao conferir a posição; mantida a do cliente")
            return position
        if position <= db_max:
            return position
        log.warning("hub: posição maior que o maior id do outbox", extra={"position": position, "db_max": db_max})
        # Com o outbox vazio (tudo venceu), o maior id é 0 mas o hub já publicou até `upto`.
        return max(db_max, upto)

    async def _replay_from_db(self, after_id: int, upto_id: int) -> AsyncGenerator[tuple[bytes, int], None]:
        while after_id < upto_id:
            rows = await self._db(_fetch_frames, after_id, upto_id, FETCH_BATCH)
            if not rows:
                return
            for chunk, last_id in _chunked((row_id, frame) for row_id, frame, _, _ in rows):
                yield chunk, last_id
            after_id = rows[-1][0]
            if len(rows) < FETCH_BATCH:
                return


async def _poll_stream(after_id: int | None) -> AsyncGenerator[bytes, None]:
    """Modo sem hub (REALTIME["HUB_ENABLED"] = False): cada conexão consulta o banco.

    Só para depuração; em produção o hub atende todas as conexões com uma
    consulta por lote. Não marca `published_at`.
    """
    poll = float(settings.REALTIME["POLL_INTERVAL"])
    ping_every = float(settings.REALTIME["PING_INTERVAL"])
    yield format_ping()
    last_sent = after_id if after_id is not None else await _db_once(outbox.current_cursor)
    next_ping = time.monotonic() + ping_every
    while True:
        rows = await _db_once(_fetch_frames, last_sent, None, FETCH_BATCH)
        if rows:
            for chunk, _ in _chunked((row_id, frame) for row_id, frame, _, _ in rows):
                yield chunk
            last_sent = rows[-1][0]
            if len(rows) == FETCH_BATCH:
                continue
        if time.monotonic() >= next_ping:
            yield format_ping()
            next_ping = time.monotonic() + ping_every
        await asyncio.sleep(max(0.0, min(poll, next_ping - time.monotonic())))


hub = Hub()
