"""Tempo real (fase 6): hub, stream SSE com retomada, ping e limpeza do outbox.

As mensagens são gravadas pelo caminho de produção: transação com a trava de
escrita (`core.locks.locked_atomic`) e `realtime.outbox.enqueue`. Os testes do
hub usam o singleton de verdade, com commit real (transaction=True): o aviso
do `on_commit` acorda o hub sem esperar o polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection

from core.locks import locked_atomic
from observability.metrics import metrics
from realtime import hub as hub_module
from realtime import outbox
from realtime.hub import DEFAULT_BUFFER_FRAMES, RETRY_FRAME, Hub, format_frame, format_ping, hub
from realtime.models import Outbox
from realtime.views import InvalidPosition, parse_position

BASE_DIR = Path(__file__).resolve().parent.parent
STEP_TIMEOUT = 5.0  # limite de cada espera; nenhum teste pode travar


# --- Gravação e leitura (caminho de produção) -------------------------------------


def write_messages(*payloads: dict, topic: str = "match") -> list[int]:
    """Uma transação com a trava de escrita e uma linha do outbox por payload."""
    with locked_atomic():
        return [outbox.enqueue(topic, payload).id for payload in payloads]


def _in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    finally:
        connection.close()  # a thread auxiliar não deixa conexão aberta


async def adb(fn: Callable[..., Any], *args: Any) -> Any:
    """Roda código do ORM numa thread à parte (commit real, fora do laço)."""
    return await asyncio.get_running_loop().run_in_executor(None, _in_thread, fn, *args)


async def awrite(*payloads: dict, topic: str = "match") -> list[int]:
    return await adb(lambda: write_messages(*payloads, topic=topic))


def parse_block(block: bytes) -> dict:
    event: dict[str, Any] = {}
    for line in block.decode().split("\n"):
        field, _, value = line.partition(":")
        event[field] = value[1:] if value.startswith(" ") else value
    if "id" in event:
        event["id"] = int(event["id"])
    if "data" in event:
        event["data"] = json.loads(event["data"])
    return event


class SSEReader:
    """Lê um stream SSE numa tarefa à parte e entrega os eventos já separados.

    Fechar o leitor cancela a tarefa, como o Django faz quando o cliente cai.
    """

    def __init__(self, chunks: AsyncIterator[bytes]):
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.raw: list[bytes] = []
        self.ended = asyncio.Event()
        self.pings = 0
        self._task = asyncio.create_task(self._pump(chunks))

    async def _pump(self, chunks: AsyncIterator[bytes]) -> None:
        buffer = b""
        try:
            async for chunk in chunks:
                self.raw.append(chunk)
                buffer += chunk
                *blocks, buffer = buffer.split(b"\n\n")
                for block in blocks:
                    if block:
                        event = parse_block(block)
                        self.pings += event.get("event") == "ping"
                        self.events.put_nowait(event)
        finally:
            self.ended.set()

    async def next(self, timeout: float = STEP_TIMEOUT) -> dict:
        return await asyncio.wait_for(self.events.get(), timeout)

    async def next_message(self, timeout: float = STEP_TIMEOUT) -> dict:
        """Próxima mensagem do outbox (ignora `retry` e pings, que não têm id)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            event = await asyncio.wait_for(self.events.get(), max(0.01, deadline - loop.time()))
            if "id" in event:
                return event

    async def messages(self, count: int, timeout: float = STEP_TIMEOUT) -> list[dict]:
        return [await self.next_message(timeout) for _ in range(count)]

    def queued_messages(self) -> list[dict]:
        found = []
        while not self.events.empty():
            event = self.events.get_nowait()
            if "id" in event:
                found.append(event)
        return found

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def wait_until(predicate: Callable[[], bool], timeout: float = STEP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condição não atingida no tempo limite")
        await asyncio.sleep(0.01)


async def wait_until_async(read: Callable[[], Any], expected: Any, timeout: float = STEP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while (value := await read()) != expected:
        if time.monotonic() > deadline:
            raise AssertionError(f"esperado {expected!r}, obtido {value!r}")
        await asyncio.sleep(0.05)


def ids(events: list[dict]) -> list[int]:
    return [event["id"] for event in events]


@pytest.fixture
async def live_hub(transactional_db, settings):
    """Hub do processo, com polling lento: a entrega rápida prova o aviso do on_commit."""
    settings.REALTIME = {**settings.REALTIME, "POLL_INTERVAL": 5.0, "PING_INTERVAL": 30.0, "HUB_ENABLED": True}
    yield hub
    await hub.stop()


def realtime_settings(settings, **overrides: Any) -> None:
    settings.REALTIME = {**settings.REALTIME, **overrides}


# --- Quadros e parâmetros (sem banco) ---------------------------------------------


def test_frame_format_is_compact_utf8_and_has_no_raw_newlines():
    frame = format_frame(12, "match", {"stage_id": 3, "texto": "Gol do Náutico!\nVirada", "lista": [1, 2]})
    assert frame == 'id: 12\nevent: match\ndata: {"stage_id":3,"texto":"Gol do Náutico!\\nVirada","lista":[1,2]}\n\n'.encode()
    assert frame.count(b"\n") == 4  # três linhas e a linha em branco que fecha o quadro
    ping = format_ping(datetime(2026, 10, 3, 21, 0, tzinfo=dt_timezone.utc))
    assert ping == b'event: ping\ndata: {"server_time":"2026-10-03T21:00:00Z"}\n\n'
    assert RETRY_FRAME == b"retry: 3000\n\n"


@pytest.mark.parametrize(
    ("last_event_id", "after", "expected"),
    [
        (None, None, None),
        (None, "0", 0),
        (None, "42", 42),
        ("7", "3", 7),  # Last-Event-ID vale mais que after
        ("7", "lixo", 7),  # com o header válido, after nem é lido
        ("", "5", 5),  # header vazio é ignorado
        ("   ", None, None),
    ],
)
def test_parse_position_precedence(last_event_id, after, expected):
    assert parse_position(last_event_id, after) == expected


@pytest.mark.parametrize(
    ("last_event_id", "after", "field"),
    [
        (None, "-1", "after"),
        (None, "1.5", "after"),
        (None, "", "after"),
        (None, "١٢", "after"),  # dígitos não ASCII
        (None, "9" * 20, "after"),
        (None, str(2**63), "after"),
        ("abc", "5", "Last-Event-ID"),
    ],
)
def test_parse_position_rejects_invalid_values(last_event_id, after, field):
    with pytest.raises(InvalidPosition) as excinfo:
        parse_position(last_event_id, after)
    assert excinfo.value.field == field


def test_stream_rejects_invalid_position_and_other_methods(client):
    # Sem marca de banco: o parâmetro inválido é recusado antes de qualquer consulta.
    response = client.get("/api/stream?after=-1")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input"
    assert response.json()["details"] == {"field": "after"}

    response = client.get("/api/stream?after=1", headers={"Last-Event-ID": "x"})
    assert response.status_code == 400
    assert response.json()["details"] == {"field": "Last-Event-ID"}

    assert client.post("/api/stream").status_code == 405


def test_wake_is_a_noop_before_the_hub_starts():
    fresh = Hub()
    fresh.wake_threadsafe()  # chamada de qualquer thread, sem hub iniciado
    assert fresh.subscriber_count == 0 and not fresh.running


# --- Stream pela view (AsyncClient) ----------------------------------------------


async def test_stream_headers_retry_and_ping_on_open(async_client, live_hub):
    response = await async_client.get("/api/stream?after=0")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream; charset=utf-8"
    assert response["Cache-Control"] == "no-cache"
    assert response["X-Accel-Buffering"] == "no"
    reader = SSEReader(response.streaming_content)
    try:
        await reader.next()
        assert reader.raw[0] == RETRY_FRAME  # primeira linha do stream
        ping = await reader.next()
        assert ping["event"] == "ping" and "id" not in ping
        server_time = ping["data"]["server_time"]
        assert server_time.endswith("Z")
        sent_at = datetime.fromisoformat(server_time.replace("Z", "+00:00"))
        assert abs((datetime.now(dt_timezone.utc) - sent_at).total_seconds()) < 5
        assert live_hub.subscriber_count == 1
    finally:
        await reader.close()
    assert live_hub.subscriber_count == 0  # desconexão tira o assinante do hub


@pytest.mark.parametrize("buffer_frames", [DEFAULT_BUFFER_FRAMES, 0], ids=["memoria", "banco"])
async def test_replay_after_cursor(async_client, live_hub, settings, buffer_frames):
    realtime_settings(settings, REPLAY_BUFFER_FRAMES=buffer_frames)
    first, second, third = await awrite({"n": 1}, {"n": 2}, {"n": 3})
    [goals] = await awrite({"date": "2026-10-03", "changes": [], "latest_goals": []}, topic="goals")

    response = await async_client.get(f"/api/stream?after={first}")
    reader = SSEReader(response.streaming_content)
    try:
        received = await reader.messages(3)
        assert ids(received) == [second, third, goals]
        assert [event["event"] for event in received] == ["match", "match", "goals"]
        assert received[0]["data"] == {"n": 2}
        await asyncio.sleep(0.2)
        assert reader.queued_messages() == []
    finally:
        await reader.close()


async def test_last_event_id_beats_after(async_client, live_hub):
    _, second, third = await awrite({"n": 1}, {"n": 2}, {"n": 3})
    response = await async_client.get("/api/stream?after=0", headers={"Last-Event-ID": str(second)})
    reader = SSEReader(response.streaming_content)
    try:
        assert ids([await reader.next_message()]) == [third]
        await asyncio.sleep(0.2)
        assert reader.queued_messages() == []  # nada de after=0
    finally:
        await reader.close()


async def test_stream_without_cursor_starts_live(async_client, live_hub):
    await awrite({"antigo": True})
    response = await async_client.get("/api/stream")
    reader = SSEReader(response.streaming_content)
    try:
        await reader.next()  # ping de abertura
        await wait_until(lambda: live_hub.ready)
        [new] = await awrite({"novo": True})
        assert ids([await reader.next_message()]) == [new]
    finally:
        await reader.close()


# --- Hub -------------------------------------------------------------------------


async def test_two_subscribers_receive_the_same_message_exactly_once(live_hub):
    cursor = await adb(outbox.current_cursor)
    one, two = SSEReader(hub.subscribe(cursor)), SSEReader(hub.subscribe(cursor))
    try:
        assert (await one.next())["event"] == "ping" and (await two.next())["event"] == "ping"
        assert live_hub.subscriber_count == 2
        await wait_until(lambda: live_hub.ready)  # publicação ao vivo, não o retrato inicial
        started = time.monotonic()
        [goal] = await awrite({"gol": "Sport"})
        first_one, first_two = await one.next_message(), await two.next_message()
        assert time.monotonic() - started < 2.5  # o on_commit acordou o hub (polling = 5 s)
        assert first_one == first_two and first_one["id"] == goal

        batch = await awrite({"n": 2}, {"n": 3})
        assert ids(await one.messages(2)) == batch
        assert ids(await two.messages(2)) == batch
        assert one.raw[-1] is two.raw[-1]  # o mesmo bytes para todos, serializado uma vez
        await asyncio.sleep(0.3)
        assert one.queued_messages() == [] and two.queued_messages() == []
    finally:
        await one.close()
        await two.close()
    assert live_hub.subscriber_count == 0


@pytest.mark.parametrize("buffer_frames", [DEFAULT_BUFFER_FRAMES, 0], ids=["memoria", "banco"])
async def test_reconnect_with_last_event_id_has_no_loss_or_duplicates(async_client, live_hub, settings, buffer_frames):
    realtime_settings(settings, REPLAY_BUFFER_FRAMES=buffer_frames)
    cursor = await adb(outbox.current_cursor)
    watcher = SSEReader(hub.subscribe(cursor))  # outro cliente segue conectado
    first_conn = SSEReader((await async_client.get(f"/api/stream?after={cursor}")).streaming_content)
    try:
        [m1] = await awrite({"n": 1})
        assert ids([await first_conn.next_message()]) == [m1]
        assert ids([await watcher.next_message()]) == [m1]
        await first_conn.close()  # a conexão cai

        missed = await awrite({"n": 2}, {"n": 3})
        missed += await awrite({"n": 4})
        assert ids(await watcher.messages(3)) == missed

        # O navegador reconecta sozinho mandando Last-Event-ID (o after antigo fica na URL).
        response = await async_client.get(f"/api/stream?after={cursor}", headers={"Last-Event-ID": str(m1)})
        second_conn = SSEReader(response.streaming_content)
        try:
            assert ids(await second_conn.messages(3)) == missed
            [m5] = await awrite({"n": 5})
            assert ids([await second_conn.next_message()]) == [m5]
            assert ids([await watcher.next_message()]) == [m5]
            await asyncio.sleep(0.3)
            assert second_conn.queued_messages() == [] and watcher.queued_messages() == []
        finally:
            await second_conn.close()
    finally:
        await first_conn.close()
        await watcher.close()


@pytest.mark.parametrize("buffer_frames", [DEFAULT_BUFFER_FRAMES, 0], ids=["memoria", "banco"])
async def test_handoff_has_no_gaps_or_duplicates_under_concurrent_writes(live_hub, settings, buffer_frames):
    # Reconexões em sequência enquanto outro "processo" grava sem parar.
    realtime_settings(settings, REPLAY_BUFFER_FRAMES=buffer_frames, POLL_INTERVAL=0.05)
    cursor = await adb(outbox.current_cursor)
    written: list[int] = []

    async def writer() -> None:
        for n in range(30):
            written.extend(await awrite({"n": n}))
            await asyncio.sleep(0.003)

    writer_task = asyncio.create_task(writer())
    received: list[int] = []
    last = cursor
    pattern = [1, 3, 2, 5, 1, 4]
    turn = 0
    try:
        while not writer_task.done():
            reader = SSEReader(hub.subscribe(last))
            try:
                for _ in range(pattern[turn % len(pattern)]):
                    event = await reader.next_message(timeout=1.0)
                    received.append(event["id"])
                    last = event["id"]
            except TimeoutError:
                pass
            finally:
                await reader.close()  # o que ficou na fila do leitor volta pelo reenvio
            turn += 1
        await writer_task
        reader = SSEReader(hub.subscribe(last))
        try:
            while len(received) < len(written):
                event = await reader.next_message()
                received.append(event["id"])
        finally:
            await reader.close()
    finally:
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer_task
    assert received == written


async def test_periodic_ping(live_hub, settings):
    realtime_settings(settings, PING_INTERVAL=0.2)
    reader = SSEReader(hub.subscribe(None))
    try:
        assert (await reader.next())["event"] == "ping"  # ao abrir
        second = await reader.next(timeout=2.0)  # do relógio do hub
        assert second["event"] == "ping" and "id" not in second
        assert second["data"]["server_time"].endswith("Z")
    finally:
        await reader.close()


async def test_published_at_is_marked_and_metrics_are_updated(live_hub):
    published_before = metrics.value("fdr_stream_messages_published_total", topic="standings")
    connections_before = metrics.value("fdr_sse_connections_total")
    reader = SSEReader(hub.subscribe(await adb(outbox.current_cursor)))
    try:
        await reader.next()
        assert metrics.value("fdr_sse_connections") == 1
        assert metrics.value("fdr_sse_connections_total") == connections_before + 1
        await wait_until(lambda: live_hub.ready)  # publicação ao vivo, não retrato
        [row_id] = await awrite({"stage_id": 1}, topic="standings")
        assert (await reader.next_message())["id"] == row_id
        published_at = await adb(lambda: Outbox.objects.get(id=row_id).published_at)
        assert published_at is not None
        assert metrics.value("fdr_stream_messages_published_total", topic="standings") == published_before + 1
        assert 0 <= metrics.value("fdr_outbox_lag_seconds") < 5
    finally:
        await reader.close()
    assert metrics.value("fdr_sse_connections") == 0


async def test_slow_subscriber_is_disconnected_and_recovers_by_replay(live_hub, settings):
    realtime_settings(settings, SUBSCRIBER_QUEUE_SIZE=2)
    cursor = await adb(outbox.current_cursor)
    frames = hub.subscribe(cursor)
    try:
        assert b"event: ping" in await frames.__anext__()
        pending_read = asyncio.create_task(frames.__anext__())  # entra no modo ao vivo e espera
        [m1] = await awrite({"n": 1})
        assert f"id: {m1}\n".encode() in await asyncio.wait_for(pending_read, STEP_TIMEOUT)
        # Daqui em diante ninguém lê: cada lote ocupa uma vaga da fila (limite 2).
        later = []
        for n in (2, 3, 4):
            later += await awrite({"n": n})
            target = later[-1]
            await wait_until(lambda: hub.last_id == target)  # noqa: B023 - usada na hora
        assert hub.subscriber_count == 0  # desconectado ao estourar a fila
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(frames.__anext__(), STEP_TIMEOUT)  # o stream termina
    finally:
        await frames.aclose()

    reader = SSEReader(hub.subscribe(m1))  # volta com Last-Event-ID = m1
    try:
        assert ids(await reader.messages(3)) == later
    finally:
        await reader.close()


async def test_cursor_ahead_of_the_hub_skips_what_the_client_already_has(live_hub, settings, monkeypatch):
    # A leitura viu um commit que o hub ainda não publicou (gravação de outro
    # processo, antes do polling): o cliente já tem esse estado e não o recebe de novo.
    realtime_settings(settings, POLL_INTERVAL=60.0)
    watcher = SSEReader(hub.subscribe(None))
    try:
        await watcher.next()
        await wait_until(lambda: live_hub.ready)
        monkeypatch.setattr(hub, "wake_threadsafe", lambda: None)  # nada acorda o hub
        already = await awrite({"n": 1}, {"n": 2})
        cursor = await adb(outbox.current_cursor)
        assert cursor == already[-1] and hub.last_id < cursor
        reader = SSEReader(hub.subscribe(cursor))
        try:
            await reader.next()  # ping de abertura
            monkeypatch.undo()
            hub.wake_threadsafe()  # agora o hub publica o que estava pendente
            assert ids(await watcher.messages(2)) == already
            [new] = await awrite({"n": 3})
            assert ids([await reader.next_message()]) == [new]
            assert ids([await watcher.next_message()]) == [new]
        finally:
            await reader.close()
    finally:
        await watcher.close()


async def test_position_from_another_database_is_clamped(live_hub):
    # Last-Event-ID de um banco recriado (ids maiores que o maior id daqui): sem o
    # ajuste, o cliente ficaria sem mensagem nenhuma, só recebendo pings.
    cursor = await adb(outbox.current_cursor)
    reader = SSEReader(hub.subscribe(cursor + 10_000))
    try:
        await reader.next()
        await wait_until(lambda: live_hub.ready)
        [new] = await awrite({"n": 1})
        assert ids([await reader.next_message()]) == [new]
    finally:
        await reader.close()


async def test_hub_marks_rows_written_before_it_started_as_published(live_hub):
    assert not live_hub.running
    before = await adb(outbox.current_cursor)
    written = await awrite({"n": 1}, {"n": 2})  # sem hub: ninguém publica
    assert await adb(lambda: Outbox.objects.filter(id__in=written, published_at__isnull=False).count()) == 0
    published_before = metrics.value("fdr_stream_messages_published_total", topic="match")
    reader = SSEReader(hub.subscribe(before))
    try:
        assert ids(await reader.messages(2)) == written  # reenvio a partir do cursor
        assert await adb(lambda: Outbox.objects.filter(id__in=written, published_at__isnull=True).count()) == 0
        assert metrics.value("fdr_stream_messages_published_total", topic="match") == published_before + 2
    finally:
        await reader.close()


async def test_hub_purges_expired_rows_on_start_and_never_replays_them(live_hub):
    before = await adb(outbox.current_cursor)
    old, fresh = await awrite({"n": "velha"}, {"n": "nova"})
    expired_at = datetime.now(dt_timezone.utc) - timedelta(hours=25)
    await adb(lambda: Outbox.objects.filter(id=old).update(created_at=expired_at))
    reader = SSEReader(hub.subscribe(before))
    try:
        assert ids([await reader.next_message()]) == [fresh]
        await asyncio.sleep(0.2)
        assert reader.queued_messages() == []
        assert not await adb(lambda: Outbox.objects.filter(id=old).exists())
    finally:
        await reader.close()


async def test_periodic_purge_also_expires_the_memory_buffer(live_hub, settings, monkeypatch, caplog):
    # Sem isso, a memória reenviaria o que o banco já apagou (com o outbox vazio,
    # o cursor da leitura é 0 e o reenvio traria mensagens de ontem: alertas de gol velhos).
    caplog.set_level(logging.INFO, logger="fdr.realtime")
    realtime_settings(settings, POLL_INTERVAL=0.1)
    monkeypatch.setattr(hub_module, "PURGE_EVERY", 0.3)  # a limpeza "de hora em hora", acelerada
    before = await adb(outbox.current_cursor)
    old, fresh = await awrite({"n": "vencendo"}, {"n": "nova"})
    margin = 2.0
    almost_expired = datetime.now(dt_timezone.utc) - timedelta(hours=24) + timedelta(seconds=margin)
    await adb(lambda: Outbox.objects.filter(id=old).update(created_at=almost_expired))
    watcher = SSEReader(hub.subscribe(None))  # com alguém conectado, o ciclo do hub segue girando
    try:
        reader = SSEReader(hub.subscribe(before))
        try:
            assert ids(await reader.messages(2)) == [old, fresh]  # ainda dentro da retenção: vem da memória
        finally:
            await reader.close()
        await wait_until(
            lambda: any("mensagens antigas apagadas" in record.getMessage() for record in caplog.records),
            timeout=margin + STEP_TIMEOUT,
        )
        assert not await adb(lambda: Outbox.objects.filter(id=old).exists())
        reader = SSEReader(hub.subscribe(before))
        try:
            assert ids([await reader.next_message()]) == [fresh]
            await asyncio.sleep(0.2)
            assert reader.queued_messages() == []
        finally:
            await reader.close()
    finally:
        await watcher.close()


async def test_hub_survives_a_database_error(live_hub, settings, monkeypatch, caplog):
    realtime_settings(settings, POLL_INTERVAL=0.1)
    real_fetch = hub_module._fetch_and_mark
    failures = []

    def flaky_fetch(after_id, limit):
        if not failures:
            failures.append(after_id)
            raise OperationalError("conexão perdida (simulada)")
        return real_fetch(after_id, limit)

    monkeypatch.setattr(hub_module, "_fetch_and_mark", flaky_fetch)
    reader = SSEReader(hub.subscribe(await adb(outbox.current_cursor)))
    try:
        await reader.next()
        await wait_until(lambda: live_hub.ready)  # a mensagem tem de sair pela publicação, que falha uma vez
        [row_id] = await awrite({"n": 1})
        assert (await reader.next_message())["id"] == row_id
        assert failures and live_hub.running
        assert any("falha ao publicar" in record.getMessage() for record in caplog.records)
    finally:
        await reader.close()


async def test_stream_without_hub_polls_the_database(async_client, transactional_db, settings):
    realtime_settings(settings, HUB_ENABLED=False, POLL_INTERVAL=0.05, PING_INTERVAL=0.2)
    cursor = await adb(outbox.current_cursor)
    response = await async_client.get(f"/api/stream?after={cursor}")
    reader = SSEReader(response.streaming_content)
    try:
        assert await reader.next() == {"retry": "3000"}
        assert (await reader.next())["event"] == "ping"
        [row_id] = await awrite({"n": 1})
        assert (await reader.next_message())["id"] == row_id
        assert (await reader.next(timeout=2.0))["event"] == "ping"
        assert not hub.running
    finally:
        await reader.close()


# --- Limpeza do outbox ------------------------------------------------------------


@pytest.mark.django_db
def test_purge_removes_rows_older_than_retention():
    Outbox.objects.all().delete()  # isola de sobras de um banco reaproveitado (desfeito no fim do teste)
    old, recent = write_messages({"n": "velha"}, {"n": "nova"})
    Outbox.objects.filter(id=old).update(created_at=datetime.now(dt_timezone.utc) - timedelta(hours=25))
    assert outbox.purge_old() == 1  # padrão: 24 h
    assert list(Outbox.objects.values_list("id", flat=True)) == [recent]


@pytest.mark.django_db
def test_purge_outbox_command():
    Outbox.objects.all().delete()  # isola de sobras de um banco reaproveitado (desfeito no fim do teste)
    old, recent = write_messages({"n": 1}, {"n": 2})
    Outbox.objects.filter(id=old).update(created_at=datetime.now(dt_timezone.utc) - timedelta(hours=3))
    out = StringIO()
    call_command("purge_outbox", stdout=out)  # 24 h: nada a apagar
    assert Outbox.objects.count() == 2
    call_command("purge_outbox", "--hours", "2", stdout=out)
    assert list(Outbox.objects.values_list("id", flat=True)) == [recent]
    assert "1 mensagem(ns)" in out.getvalue()
    with pytest.raises(CommandError):
        call_command("purge_outbox", "--hours", "0")


# --- Ponta a ponta: uvicorn de verdade, HTTP de verdade --------------------------

SERVER_APP_NAME = "fdr-e2e-server"  # application_name das conexões do servidor (PGAPPNAME)
METRICS_TOKEN = "e2e-metrics-token"


@dataclass
class Server:
    process: subprocess.Popen
    url: str
    log_path: Path

    def threads(self) -> int | None:
        try:
            with open(f"/proc/{self.process.pid}/status") as status:
                for line in status:
                    if line.startswith("Threads:"):
                        return int(line.split()[1])
        except OSError:
            return None
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def uvicorn_server(transactional_db, tmp_path):
    """Um processo ASGI só (uvicorn --workers 1) apontado para o banco de teste."""
    port = _free_port()
    env = {
        **os.environ,
        "DB_NAME": connection.settings_dict["NAME"],
        "DJANGO_DEBUG": "1",
        "REALTIME_HUB_ENABLED": "1",
        "REALTIME_PING_INTERVAL": "0.5",
        "REALTIME_POLL_INTERVAL": "0.2",
        "LOG_LEVEL": "WARNING",
        "PGAPPNAME": SERVER_APP_NAME,
        "METRICS_TOKEN": METRICS_TOKEN,
        "PYTHONUNBUFFERED": "1",
    }
    log_path = tmp_path / "uvicorn.log"
    command = [
        sys.executable, "-m", "uvicorn", "config.asgi:application",
        "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
        "--timeout-graceful-shutdown", "1", "--log-level", "warning",
    ]  # fmt: skip
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(command, cwd=BASE_DIR, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    server = Server(process, f"http://127.0.0.1:{port}", log_path)
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                pytest.fail(f"uvicorn saiu com código {process.returncode}:\n{log_path.read_text()}")
            try:
                if httpx.get(f"{server.url}/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"uvicorn não respondeu em 30 s:\n{log_path.read_text()}")
            time.sleep(0.1)
        yield server
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def open_http_stream(client: httpx.AsyncClient, path: str, headers: dict | None = None) -> SSEReader:
    loop = asyncio.get_running_loop()
    opened: asyncio.Future[httpx.Response] = loop.create_future()

    async def chunks() -> AsyncIterator[bytes]:
        async with client.stream("GET", path, headers=headers) as response:
            opened.set_result(response)
            async for chunk in response.aiter_raw():
                yield chunk

    reader = SSEReader(chunks())
    done, _ = await asyncio.wait({opened, reader._task}, timeout=STEP_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
    if opened not in done:
        await reader.close()
        raise AssertionError(f"o stream {path} não abriu")
    response = opened.result()
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    first = await reader.next()
    assert first == {"retry": "3000"}
    assert (await reader.next())["event"] == "ping"
    return reader


async def server_sse_connections(client: httpx.AsyncClient) -> int:
    """Conexões SSE registradas no hub do servidor (gauge de GET /metrics)."""
    response = await client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
    assert response.status_code == 200
    for line in response.text.splitlines():
        if line.startswith("fdr_sse_connections "):
            return int(float(line.split()[1]))
    raise AssertionError("fdr_sse_connections ausente em /metrics")


def _server_db_connections() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", [SERVER_APP_NAME])
        return cursor.fetchone()[0]


async def test_end_to_end_over_http_with_two_clients(uvicorn_server):
    """Dois clientes recebem o mesmo evento; derrubar a conexão não perde nem duplica."""
    cursor = await adb(outbox.current_cursor)
    readers: list[SSEReader] = []
    async with httpx.AsyncClient(base_url=uvicorn_server.url, timeout=httpx.Timeout(10.0)) as client:
        try:
            one = await open_http_stream(client, f"/api/stream?after={cursor}")
            two = await open_http_stream(client, f"/api/stream?after={cursor}")
            readers += [one, two]

            # Gravado neste processo: o hub do servidor só descobre pelo polling.
            [m1] = await awrite({"gol": "Náutico 1 x 0 Santa Cruz"})
            got_one, got_two = await one.next_message(), await two.next_message()
            assert got_one["id"] == got_two["id"] == m1
            assert got_one["data"] == got_two["data"] == {"gol": "Náutico 1 x 0 Santa Cruz"}
            assert await adb(lambda: Outbox.objects.get(id=m1).published_at) is not None

            assert await server_sse_connections(client) == 2
            await two.close()  # derruba a conexão do segundo cliente
            # O servidor percebe a queda e tira o assinante do hub.
            await wait_until_async(lambda: server_sse_connections(client), 1)
            missed = await awrite({"n": 2}, {"n": 3})
            missed += await awrite({"n": 4}, topic="goals")
            assert ids(await one.messages(3)) == missed

            two = await open_http_stream(client, "/api/stream?after=0", headers={"Last-Event-ID": str(m1)})
            readers.append(two)
            assert ids(await two.messages(3)) == missed

            [m5] = await awrite({"n": 5})
            assert ids([await one.next_message()]) == [m5]
            assert ids([await two.next_message()]) == [m5]
            await asyncio.sleep(1.2)  # mais que o polling e que o intervalo de ping
            assert one.queued_messages() == [] and two.queued_messages() == []
            assert one.pings >= 2  # ping periódico do hub do servidor

            # Muitas conexões: nenhuma thread nem conexão de banco por cliente.
            threads_before = uvicorn_server.threads()
            extra = [await open_http_stream(client, "/api/stream") for _ in range(20)]
            readers += extra
            if threads_before is not None:  # sem /proc, só o banco é conferido
                # Sem a liberação da thread da requisição seriam +20 e não cairia.
                await wait_until(lambda: uvicorn_server.threads() - threads_before <= 3)
            await wait_until_async(lambda: adb(_server_db_connections), 1)  # só a conexão do hub
            assert await server_sse_connections(client) == 2 + len(extra)
            [m6] = await awrite({"n": 6})
            for reader in (one, two, *extra):
                assert ids([await reader.next_message()]) == [m6]
        finally:
            for reader in readers:
                await reader.close()
