"""Limites de acesso: bloqueio progressivo de login, limite por IP na /api/ e
teto de conexões do stream."""

import json
import threading
import time

import pytest
from django.contrib.auth.backends import ModelBackend
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory

from accounts import throttle
from accounts.backends import ThrottledModelBackend
from observability.metrics import metrics
from observability.models import AuditLog
from realtime import views as stream_views

pytestmark = pytest.mark.django_db

THROTTLE = {
    "ENABLED": True,
    "USER_FAILURES": 3,
    "BASE_LOCK": 60,
    "MAX_LOCK": 240,
    "STRIKE_MEMORY": 3600,
    "IP_FAILURES": 8,
    "IP_WINDOW": 900,
    "IP_LOCK": 600,
}


@pytest.fixture
def limits_on(settings):
    cache.clear()
    throttle._in_flight.clear()  # vaga presa por outro teste não vaza para este
    settings.LOGIN_THROTTLE = dict(THROTTLE)
    yield settings
    cache.clear()


class Clock:
    def __init__(self, monkeypatch, start=1_000_000.0):
        self.t = start
        monkeypatch.setattr(throttle.time, "time", lambda: self.t)

    def advance(self, seconds):
        self.t += seconds


def api_login(client, username, password, ip="10.0.0.1"):
    client.get("/api/auth/me", REMOTE_ADDR=ip)
    return client.post(
        "/api/auth/login",
        data=json.dumps({"username": username, "password": password}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        REMOTE_ADDR=ip,
    )


def test_lock_after_failures_then_429_with_retry_after(limits_on, operator_user, monkeypatch):
    Clock(monkeypatch)
    client = Client(enforce_csrf_checks=True)
    for _ in range(2):
        assert api_login(client, "operador", "errada").status_code == 401
    third = api_login(client, "operador", "errada")
    assert third.status_code == 429
    assert third.json()["code"] == "login_locked"
    assert third["Retry-After"] == "60"
    # Bloqueado, nem a senha certa entra (e não é conferida).
    blocked = api_login(client, "operador", "senha-forte-123")
    assert blocked.status_code == 429
    assert int(blocked["Retry-After"]) <= 60


def test_lock_expires_and_doubles_on_next_lock(limits_on, operator_user, monkeypatch):
    limits_on.LOGIN_THROTTLE = {**THROTTLE, "IP_FAILURES": 100}  # só o par usuário + IP aqui
    clock = Clock(monkeypatch)
    client = Client(enforce_csrf_checks=True)
    for _ in range(3):
        api_login(client, "operador", "errada")
    clock.advance(61)
    assert api_login(client, "operador", "senha-forte-123").status_code == 200
    client = Client(enforce_csrf_checks=True)
    for _ in range(2):
        assert api_login(client, "operador", "errada").status_code == 401
    second = api_login(client, "operador", "errada")
    assert second.status_code == 429 and second["Retry-After"] == "120"
    clock.advance(121)
    for _ in range(3):
        last = api_login(client, "operador", "errada")
    assert last["Retry-After"] == "240"  # teto (MAX_LOCK)
    clock.advance(241)
    for _ in range(3):
        last = api_login(client, "operador", "errada")
    assert last["Retry-After"] == "240"


def test_success_resets_failure_count(limits_on, operator_user, monkeypatch):
    Clock(monkeypatch)
    client = Client(enforce_csrf_checks=True)
    for _ in range(2):
        api_login(client, "operador", "errada")
    assert api_login(client, "operador", "senha-forte-123").status_code == 200
    client = Client(enforce_csrf_checks=True)
    for _ in range(2):
        assert api_login(client, "operador", "errada").status_code == 401


def test_lock_is_per_ip_so_attacker_cannot_lock_out_real_user(limits_on, operator_user, monkeypatch):
    Clock(monkeypatch)
    attacker = Client(enforce_csrf_checks=True)
    for _ in range(3):
        api_login(attacker, "operador", "errada", ip="203.0.113.9")
    assert api_login(attacker, "operador", "senha-forte-123", ip="203.0.113.9").status_code == 429
    real = Client(enforce_csrf_checks=True)
    assert api_login(real, "operador", "senha-forte-123", ip="10.0.0.2").status_code == 200


def test_ip_lock_when_trying_many_usernames(limits_on, operator_user, monkeypatch):
    Clock(monkeypatch)
    client = Client(enforce_csrf_checks=True)
    statuses = [api_login(client, f"usuario{n}", "x", ip="198.51.100.7").status_code for n in range(8)]
    assert statuses[:7] == [401] * 7 and statuses[7] == 429
    # Qualquer usuário, a partir desse IP, fica bloqueado.
    locked = api_login(client, "operador", "senha-forte-123", ip="198.51.100.7")
    assert locked.status_code == 429 and locked["Retry-After"] == "600"


def test_forwarded_header_cannot_dodge_the_lock(limits_on, operator_user, monkeypatch):
    Clock(monkeypatch)
    client = Client(enforce_csrf_checks=True)
    for n in range(3):
        client.get("/api/auth/me", REMOTE_ADDR="10.9.9.9")
        response = client.post(
            "/api/auth/login",
            data=json.dumps({"username": "operador", "password": "errada"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
            REMOTE_ADDR="10.9.9.9",
            HTTP_X_FORWARDED_FOR=f"1.2.3.{n}",
        )
    assert response.status_code == 429


def test_locked_attempts_are_audited(limits_on, operator_user, monkeypatch):
    Clock(monkeypatch)
    client = Client(enforce_csrf_checks=True)
    for _ in range(4):
        api_login(client, "operador", "errada")
    assert AuditLog.objects.filter(action="auth.login_failed").count() == 4


def test_admin_login_shows_lock_message(limits_on, operator_user, monkeypatch):
    Clock(monkeypatch)
    client = Client()
    for _ in range(3):
        client.post("/admin/login/", {"username": "operador", "password": "errada"}, REMOTE_ADDR="10.0.0.5")
    response = client.post("/admin/login/", {"username": "operador", "password": "senha-forte-123"}, REMOTE_ADDR="10.0.0.5")
    assert response.status_code == 200  # formulário de novo, sem sessão
    assert "Muitas tentativas de login" in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_throttle_disabled_by_setting(settings, operator_user):
    cache.clear()
    settings.LOGIN_THROTTLE = {**THROTTLE, "ENABLED": False}
    client = Client(enforce_csrf_checks=True)
    for _ in range(5):
        assert api_login(client, "operador", "errada").status_code == 401
    assert api_login(client, "operador", "senha-forte-123").status_code == 200


def concurrent_logins(monkeypatch, attempts, ip="10.0.0.9"):
    """`attempts` logins errados do "operador", do mesmo IP, ao mesmo tempo. A
    conferência da senha espera até todas estarem conferindo juntas (ou 0,5 s,
    se o bloqueio não deixar tantas chegarem lá): é a janela em que tentativas
    concorrentes se atropelam. Devolve (senhas conferidas, bloqueios recebidos)."""
    checked = []
    together = threading.Barrier(attempts, timeout=0.5)

    def slow_password_check(self, request, username=None, password=None, **kwargs):
        checked.append(username)
        try:
            together.wait()
        except threading.BrokenBarrierError:
            pass
        return None

    monkeypatch.setattr(ModelBackend, "authenticate", slow_password_check)
    backend = ThrottledModelBackend()
    start = threading.Barrier(attempts)
    locks = []

    def attempt():
        request = RequestFactory().post("/api/auth/login", REMOTE_ADDR=ip)
        start.wait()
        try:
            backend.authenticate(request, username="operador", password="errada")
        except PermissionDenied:
            pass
        if getattr(request, "login_lock", None) is not None:
            locks.append(request.login_lock)

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert not throttle._in_flight  # toda tentativa aberta foi encerrada
    return len(checked), locks


class SlowReads:
    """O cache de verdade, mas cada leitura demora um pouco: alarga a janela entre
    ler e gravar a contagem, como um cache fora do processo faria."""

    def __init__(self, real):
        self.real = real

    def get(self, *args, **kwargs):
        value = self.real.get(*args, **kwargs)
        time.sleep(0.01)
        return value

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_simultaneous_failures_all_count_and_lock(limits_on, monkeypatch):
    limits_on.LOGIN_THROTTLE = {**THROTTLE, "USER_FAILURES": 10, "IP_FAILURES": 10}
    monkeypatch.setattr(throttle, "cache", SlowReads(cache))
    user_locks = metrics.value("fdr_login_lockouts_total", scope="user_ip")
    ip_locks = metrics.value("fdr_login_lockouts_total", scope="ip")
    checked, locks = concurrent_logins(monkeypatch, 10)
    assert checked == 10  # cabem todas no limite: conferem a senha juntas
    # Nenhuma das 10 falhas se perde: as duas contagens chegam ao limite e bloqueiam.
    assert metrics.value("fdr_login_lockouts_total", scope="user_ip") == user_locks + 1
    assert metrics.value("fdr_login_lockouts_total", scope="ip") == ip_locks + 1
    assert len(locks) == 1
    other_user = RequestFactory().post("/api/auth/login", REMOTE_ADDR="10.0.0.9")
    assert throttle.check(other_user, "outro") == throttle.Lock(600)


def test_simultaneous_attempts_beyond_the_limit_never_reach_the_password(limits_on, monkeypatch):
    # Limite de 3 falhas (THROTTLE): de 8 tentativas simultâneas, só 3 conferem a
    # senha; as outras esperam por elas e já encontram o bloqueio.
    checked, locks = concurrent_logins(monkeypatch, 8)
    assert checked == 3
    assert len(locks) == 6  # o da 3ª falha + as 5 que esperavam
    assert all(0 < lock.retry_after <= 60 for lock in locks)


@pytest.fixture
def short_wait(monkeypatch):
    # Com uma vaga presa, a próxima tentativa esperaria WAIT_LIMIT e seria recusada:
    # espera curta para o teste falhar logo em vez de levar 30 s.
    monkeypatch.setattr(throttle, "WAIT_LIMIT", 0.3)


class BrokenWrites:
    """O cache de verdade, mas a gravação das chaves que contêm `fail_on` falha,
    como um Redis/Memcached que cai no meio da tentativa."""

    def __init__(self, real, fail_on):
        self.real = real
        self.fail_on = fail_on

    def set(self, key, *args, **kwargs):
        if self.fail_on in key:
            raise ConnectionError("cache fora do ar")
        return self.real.set(key, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.real, name)


def backend_login(password, ip="10.0.0.20"):
    request = RequestFactory().post("/api/auth/login", REMOTE_ADDR=ip)
    return ThrottledModelBackend().authenticate(request, username="operador", password=password)


def test_password_check_error_releases_the_attempt_once(limits_on, monkeypatch):
    def broken_password_check(self, request, username=None, password=None, **kwargs):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(ModelBackend, "authenticate", broken_password_check)
    other = RequestFactory().post("/api/auth/login", REMOTE_ADDR="10.0.0.20")
    assert throttle.begin(other, "operador") is None  # outra tentativa do par, em andamento
    with pytest.raises(RuntimeError):
        backend_login("errada")
    _ip, user_key, ip_key = throttle._keys(other, "operador")
    assert dict(throttle._in_flight) == {user_key: 1, ip_key: 1}  # só a vaga da outra
    throttle.release(other, "operador")
    assert not throttle._in_flight


def test_cache_error_on_failure_releases_the_attempt(limits_on, operator_user, monkeypatch, short_wait):
    limits_on.LOGIN_THROTTLE = {**THROTTLE, "USER_FAILURES": 2}
    monkeypatch.setattr(throttle, "cache", BrokenWrites(cache, fail_on=":ip:"))
    with pytest.raises(ConnectionError):
        backend_login("errada")  # a falha do par foi gravada; a do IP, não
    monkeypatch.setattr(throttle, "cache", cache)
    assert not throttle._in_flight
    # 1 falha de 2: com a vaga presa, esta esperaria e seria recusada.
    assert backend_login("senha-forte-123") == operator_user


def test_cache_error_on_success_releases_the_attempt(limits_on, operator_user, monkeypatch, short_wait):
    limits_on.LOGIN_THROTTLE = {**THROTTLE, "USER_FAILURES": 2}
    assert backend_login("errada") is None  # 1 falha de 2
    monkeypatch.setattr(throttle, "cache", BrokenWrites(cache, fail_on=":u:"))
    with pytest.raises(ConnectionError):
        backend_login("senha-forte-123")  # zerar a contagem falha
    monkeypatch.setattr(throttle, "cache", cache)
    assert not throttle._in_flight
    assert backend_login("senha-forte-123") == operator_user


@pytest.mark.parametrize("limit", ["USER_FAILURES", "IP_FAILURES"])
def test_zero_limit_counts_as_one(limits_on, operator_user, short_wait, limit):
    # Limite 0 vale 1: o login certo passa (sem esperar vaga) e a 1ª falha bloqueia.
    limits_on.LOGIN_THROTTLE = {**THROTTLE, limit: 0}
    assert api_login(Client(enforce_csrf_checks=True), "operador", "senha-forte-123").status_code == 200
    assert api_login(Client(enforce_csrf_checks=True), "operador", "errada").status_code == 429
    assert api_login(Client(enforce_csrf_checks=True), "operador", "senha-forte-123").status_code == 429
    assert not throttle._in_flight


# --- Limite de requisições por IP -------------------------------------------------


def test_api_rate_limit_per_ip(settings):
    cache.clear()
    settings.API_RATE_LIMIT_PER_MINUTE = 5
    client = Client()
    codes = [client.get("/api/competitions", REMOTE_ADDR="10.1.1.1").status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    blocked = client.get("/api/competitions", REMOTE_ADDR="10.1.1.1")
    assert blocked.json()["code"] == "rate_limited"
    assert 1 <= int(blocked["Retry-After"]) <= 60
    # Outro IP segue livre; páginas e /health não entram no limite.
    assert client.get("/api/competitions", REMOTE_ADDR="10.1.1.2").status_code == 200
    assert client.get("/health", REMOTE_ADDR="10.1.1.1").status_code == 200
    cache.clear()


def test_api_rate_limit_ignores_forwarded_header(settings):
    cache.clear()
    settings.API_RATE_LIMIT_PER_MINUTE = 2
    client = Client()
    codes = [
        client.get("/api/competitions", REMOTE_ADDR="10.2.2.2", HTTP_X_FORWARDED_FOR=f"9.9.9.{n}").status_code
        for n in range(3)
    ]
    assert codes == [200, 200, 429]
    cache.clear()


def test_large_body_rejected(settings, operator_client):
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 1024
    response = operator_client.post(
        "/api/auth/login", data=json.dumps({"username": "x" * 5000, "password": "y"}), content_type="application/json"
    )
    assert response.status_code in {400, 413}


# --- Teto de conexões do stream -----------------------------------------------------


def test_stream_limit_per_ip_and_total(settings, monkeypatch):
    settings.REALTIME = {**settings.REALTIME, "MAX_STREAMS_PER_IP": 2, "MAX_STREAMS": 3}
    monkeypatch.setattr(stream_views, "_open_by_ip", stream_views.Counter({"10.3.3.3": 2, "10.4.4.4": 1}))
    rf = RequestFactory()
    import asyncio

    def call(ip):
        return asyncio.run(stream_views.stream(rf.get("/api/stream", REMOTE_ADDR=ip)))

    too_many = call("10.3.3.3")
    assert too_many.status_code == 429
    assert json.loads(too_many.content)["code"] == "too_many_streams"
    full = call("10.5.5.5")
    assert full.status_code == 503 and full["Retry-After"] == "30"


def test_stream_counter_released_when_connection_ends(settings, monkeypatch):
    import asyncio

    monkeypatch.setattr(stream_views, "_open_by_ip", stream_views.Counter())

    async def fake_subscribe(after_id):
        yield b"data: 1\n\n"

    monkeypatch.setattr(stream_views.hub, "subscribe", fake_subscribe)

    async def run():
        gen = stream_views._event_stream(None, stream_views._StreamSlot("10.6.6.6"))
        await gen.__anext__()  # retry
        assert stream_views.open_streams("10.6.6.6") == 1
        await gen.aclose()

    asyncio.run(run())
    assert stream_views.open_streams("10.6.6.6") == 0
    assert stream_views.open_streams() == 0


def test_stream_slot_reserved_on_admission_and_released_once(settings, monkeypatch):
    """A vaga conta já na admissão (duas requisições juntas não passam pela mesma)
    e volta uma vez só: no fim do stream, na desconexão ou no close() da resposta."""
    import asyncio
    from contextlib import suppress

    from asgiref.sync import sync_to_async

    settings.REALTIME = {**settings.REALTIME, "MAX_STREAMS_PER_IP": 1, "MAX_STREAMS": 1}
    monkeypatch.setattr(stream_views, "_open_by_ip", stream_views.Counter())
    rf = RequestFactory()

    def admit(ip):
        return stream_views.stream(rf.get("/api/stream", REMOTE_ADDR=ip))

    async def run():
        streaming = asyncio.Event()

        async def fake_subscribe(after_id):
            streaming.set()
            await asyncio.Event().wait()  # stream aberto até o cliente cair
            yield b""

        monkeypatch.setattr(stream_views.hub, "subscribe", fake_subscribe)

        # Nenhum corpo começou a ser enviado e as duas chegam juntas: só uma entra.
        first, second = await asyncio.gather(admit("10.7.7.7"), admit("10.7.7.7"))
        assert sorted([first.status_code, second.status_code]) == [200, 429]
        assert (await admit("10.8.8.8")).status_code == 503  # teto do processo
        assert stream_views.open_streams() == 1
        admitted = first if first.status_code == 200 else second

        # Cancelada antes do primeiro byte: o Django só chama close(), noutra thread.
        await sync_to_async(admitted.close)()
        await asyncio.sleep(0)
        assert stream_views.open_streams() == 0

        # Stream aberto e o cliente cai: o handler ASGI cancela a leitura do corpo.
        response = await admit("10.7.7.7")
        assert response.status_code == 200

        async def consume():
            async for _ in response:
                pass

        task = asyncio.create_task(consume())
        await streaming.wait()
        assert stream_views.open_streams("10.7.7.7") == 1
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert stream_views.open_streams() == 0

        # A vaga é de outra conexão agora: o close() tardio da antiga não a devolve.
        again = await admit("10.7.7.7")
        assert again.status_code == 200
        await sync_to_async(response.close)()
        await asyncio.sleep(0)
        assert stream_views.open_streams("10.7.7.7") == 1

        # View chamada direto (sem a aplicação ASGI), resposta descartada sem
        # close(): a rede de segurança é o finalizer. O caso real, com a resposta
        # presa num ciclo, está no teste seguinte.
        del again
        await asyncio.sleep(0)
        assert stream_views.open_streams() == 0

    asyncio.run(run())
    assert stream_views.open_streams() == 0


def test_stream_slot_released_when_client_drops_during_middlewares(settings, monkeypatch):
    """O cliente cai enquanto a resposta ainda passa pelos middlewares: o Django
    descarta a resposta sem close() e o gerador nunca começa. A vaga volta quando
    a requisição termina na aplicação ASGI, sem depender do coletor de lixo."""
    import asyncio
    import gc

    from config.asgi import application

    settings.REALTIME = {**settings.REALTIME, "MAX_STREAMS_PER_IP": 1, "MAX_STREAMS": 1}
    monkeypatch.setattr(stream_views, "_open_by_ip", stream_views.Counter())
    started = []

    async def fake_subscribe(after_id):
        started.append(after_id)
        await asyncio.Event().wait()
        yield b""

    monkeypatch.setattr(stream_views.hub, "subscribe", fake_subscribe)

    async def run():
        admitted = asyncio.Event()

        class Slot(stream_views._StreamSlot):
            def __init__(self, ip):
                super().__init__(ip)
                admitted.set()

        monkeypatch.setattr(stream_views, "_StreamSlot", Slot)
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            if messages:
                return messages.pop(0)
            await admitted.wait()  # cai logo depois que a view reservou a vaga
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": "/api/stream", "raw_path": b"/api/stream", "query_string": b"",
            "root_path": "", "headers": [(b"host", b"testserver")],
            "client": ("10.9.9.9", 50000), "server": ("testserver", 80),
        }
        await application(scope, receive, send)
        assert admitted.is_set() and not started  # a view admitiu, o corpo nunca foi lido
        assert stream_views.open_streams() == 0

    gc.disable()  # a resposta descartada fica num ciclo: só a coleta completa a soltaria
    try:
        asyncio.run(run())
    finally:
        gc.enable()
