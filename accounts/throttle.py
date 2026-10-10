"""Bloqueio progressivo de login contra força bruta.

Vale para todo login com senha (`/api/auth/login` e `/admin/login/`), porque é
aplicado no backend de autenticação (accounts/backends.py).

Duas contagens, ambas por IP do cliente (core.net.client_ip):

* usuário + IP: a cada `USER_FAILURES` falhas seguidas, bloqueia por
  `BASE_LOCK` segundos, dobrando a cada novo bloqueio até `MAX_LOCK`. O histórico
  de bloqueios dura `STRIKE_MEMORY`. Login certo zera a contagem desse par.
* só IP: `IP_FAILURES` falhas em `IP_WINDOW` segundos, com qualquer usuário,
  bloqueiam o IP por `IP_LOCK` segundos (quem testa muitos nomes).

Não há bloqueio só por usuário: um atacante conseguiria trancar fora o operador
de verdade errando a senha dele de outro lugar.

Bloqueado, a senha nem é conferida (sem custo de hash e sem dar pista se estava
certa).

Tentativas simultâneas (cada requisição síncrona roda na sua thread): uma trava
do processo serializa o ler-modificar-gravar das contagens, e cada tentativa
aberta (`begin`) conta como uma falha possível até terminar. Se as abertas,
somadas às falhas já contadas, puderem chegar ao limite, a próxima espera por
elas antes de conferir a senha — e encontra o bloqueio se elas o dispararem.
Assim, N falhas simultâneas contam N, como em sequência.

O estado fica no cache `default` do processo e a trava vale para o processo,
suficiente com um processo ASGI só; com mais processos, troque o cache por
Redis/Memcached e a trava por uma do próprio cache (ex.: lock do Redis).
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from core.net import client_ip
from observability.logging import get_logger
from observability.metrics import metrics

log = get_logger("security")

PREFIX = "fdr:login"
WAIT_LIMIT = 30  # s esperando tentativas em andamento; passou disso, recusa

# Trava das contagens e tentativas em andamento (senha sendo conferida) por chave
# do cache, neste processo.
_guard = threading.Condition()
_in_flight: Counter[str] = Counter()


def _cfg() -> dict:
    return settings.LOGIN_THROTTLE


def _user_key(username: str, ip: str) -> str:
    digest = hashlib.sha256(username.strip().casefold().encode()).hexdigest()[:32]
    return f"{PREFIX}:u:{digest}:{ip}"


def _ip_key(ip: str) -> str:
    return f"{PREFIX}:ip:{ip}"


@dataclass(frozen=True)
class Lock:
    retry_after: int  # segundos até liberar

    @property
    def minutes(self) -> int:
        return max(1, math.ceil(self.retry_after / 60))


def _remaining(until: float | None, now: float) -> int:
    return math.ceil(until - now) if until and until > now else 0


def _wait(user_state: dict, ip_state: dict, now: float) -> int:
    return max(_remaining(user_state.get("until"), now), _remaining(ip_state.get("until"), now))


def enabled() -> bool:
    return bool(_cfg().get("ENABLED", True))


def _keys(request, username: str) -> tuple[str, str, str]:
    ip = client_ip(request) or "desconhecido"
    return ip, _user_key(username or "", ip), _ip_key(ip)


def _release(user_key: str, ip_key: str) -> None:
    """Encerra uma tentativa aberta e acorda quem esperava por ela (com `_guard`)."""
    for key in (user_key, ip_key):
        _in_flight[key] -= 1
        if _in_flight[key] <= 0:
            del _in_flight[key]
    _guard.notify_all()


def check(request, username: str) -> Lock | None:
    """Bloqueio em vigor para este usuário/IP, ou None (só consulta)."""
    if not enabled():
        return None
    _ip, user_key, ip_key = _keys(request, username)
    wait = _wait(cache.get(user_key) or {}, cache.get(ip_key) or {}, time.time())
    return Lock(wait) if wait else None


def begin(request, username: str) -> Lock | None:
    """Abre uma tentativa de login: devolve o bloqueio em vigor ou, sem bloqueio,
    reserva a vez dela (esperando as em andamento, se preciso). Toda tentativa
    aberta termina com `record_failure`, `record_success` ou `release`."""
    if not enabled():
        return None
    cfg = _cfg()
    _ip, user_key, ip_key = _keys(request, username)
    deadline = time.monotonic() + WAIT_LIMIT
    with _guard:
        while True:
            now = time.time()
            user_state = cache.get(user_key) or {}
            ip_state = cache.get(ip_key) or {}
            wait = _wait(user_state, ip_state, now)
            if wait:
                return Lock(wait)
            hits = [t for t in ip_state.get("hits", []) if t > now - cfg["IP_WINDOW"]]
            if (
                user_state.get("failures", 0) + _in_flight[user_key] < cfg["USER_FAILURES"]
                and len(hits) + _in_flight[ip_key] < cfg["IP_FAILURES"]
            ):
                _in_flight[user_key] += 1
                _in_flight[ip_key] += 1
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                return Lock(WAIT_LIMIT)
            _guard.wait(left)


def release(request, username: str) -> None:
    """Encerra a tentativa aberta sem resultado (erro ao conferir a senha)."""
    if not enabled():
        return
    _ip, user_key, ip_key = _keys(request, username)
    with _guard:
        _release(user_key, ip_key)


def record_failure(request, username: str) -> Lock | None:
    """Conta uma falha e encerra a tentativa; devolve o bloqueio se ela disparou um."""
    if not enabled():
        return None
    ip, key, ip_key = _keys(request, username)
    with _guard:
        lock = _count_failure(ip, key, ip_key)
        _release(key, ip_key)
    return lock


def _count_failure(ip: str, key: str, ip_key: str) -> Lock | None:
    cfg = _cfg()
    now = time.time()
    lock = None

    state = cache.get(key) or {"failures": 0, "strikes": 0, "until": None}
    state["failures"] += 1
    if state["failures"] >= cfg["USER_FAILURES"]:
        seconds = min(cfg["BASE_LOCK"] * 2 ** state["strikes"], cfg["MAX_LOCK"])
        state.update(failures=0, strikes=state["strikes"] + 1, until=now + seconds)
        lock = Lock(int(seconds))
        metrics.inc("fdr_login_lockouts_total", scope="user_ip")
        log.warning("login bloqueado", extra={"scope": "user_ip", "ip": ip, "seconds": seconds, "strikes": state["strikes"]})
    cache.set(key, state, cfg["STRIKE_MEMORY"])

    ip_state = cache.get(ip_key) or {"hits": [], "until": None}
    window_start = now - cfg["IP_WINDOW"]
    ip_state["hits"] = [t for t in ip_state["hits"] if t > window_start] + [now]
    if len(ip_state["hits"]) >= cfg["IP_FAILURES"] and not _remaining(ip_state.get("until"), now):
        ip_state.update(hits=[], until=now + cfg["IP_LOCK"])
        lock = Lock(int(cfg["IP_LOCK"]))
        metrics.inc("fdr_login_lockouts_total", scope="ip")
        log.warning("login bloqueado", extra={"scope": "ip", "ip": ip, "seconds": cfg["IP_LOCK"]})
    cache.set(ip_key, ip_state, max(cfg["IP_WINDOW"], cfg["IP_LOCK"]))
    return lock


def record_success(request, username: str) -> None:
    """Login certo: zera as falhas do par usuário/IP (o histórico de bloqueios fica)
    e encerra a tentativa."""
    if not enabled():
        return
    _ip, key, ip_key = _keys(request, username)
    with _guard:
        state = cache.get(key)
        if state:
            state.update(failures=0, until=None)
            cache.set(key, state, _cfg()["STRIKE_MEMORY"])
        _release(key, ip_key)


def lock_message(lock: Lock) -> str:
    return f"Muitas tentativas de login. Tente de novo em {lock.minutes} min."
