"""Limites de acesso: bloqueio progressivo de login, limite por IP na /api/ e
teto de conexões do stream."""

import json

import pytest
from django.core.cache import cache
from django.test import Client, RequestFactory

from accounts import throttle
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
        gen = stream_views._event_stream(None, "10.6.6.6")
        await gen.__anext__()  # retry
        assert stream_views.open_streams("10.6.6.6") == 1
        await gen.aclose()

    asyncio.run(run())
    assert stream_views.open_streams("10.6.6.6") == 0
    assert stream_views.open_streams() == 0
