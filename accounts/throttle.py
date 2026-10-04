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
certa). O estado fica no cache `default` do processo, suficiente com um processo
ASGI só; com mais processos, troque o cache por Redis/Memcached.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from core.net import client_ip
from observability.logging import get_logger
from observability.metrics import metrics

log = get_logger("security")

PREFIX = "fdr:login"


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


def enabled() -> bool:
    return bool(_cfg().get("ENABLED", True))


def check(request, username: str) -> Lock | None:
    """Bloqueio em vigor para este usuário/IP, ou None."""
    if not enabled():
        return None
    ip = client_ip(request) or "desconhecido"
    now = time.time()
    user_state = cache.get(_user_key(username or "", ip)) or {}
    ip_state = cache.get(_ip_key(ip)) or {}
    wait = max(_remaining(user_state.get("until"), now), _remaining(ip_state.get("until"), now))
    return Lock(wait) if wait else None


def record_failure(request, username: str) -> Lock | None:
    """Conta uma falha; devolve o bloqueio se ela disparou um."""
    if not enabled():
        return None
    cfg = _cfg()
    ip = client_ip(request) or "desconhecido"
    now = time.time()
    lock = None

    key = _user_key(username or "", ip)
    state = cache.get(key) or {"failures": 0, "strikes": 0, "until": None}
    state["failures"] += 1
    if state["failures"] >= cfg["USER_FAILURES"]:
        seconds = min(cfg["BASE_LOCK"] * 2 ** state["strikes"], cfg["MAX_LOCK"])
        state.update(failures=0, strikes=state["strikes"] + 1, until=now + seconds)
        lock = Lock(int(seconds))
        metrics.inc("fdr_login_lockouts_total", scope="user_ip")
        log.warning("login bloqueado", extra={"scope": "user_ip", "ip": ip, "seconds": seconds, "strikes": state["strikes"]})
    cache.set(key, state, cfg["STRIKE_MEMORY"])

    ip_state = cache.get(_ip_key(ip)) or {"hits": [], "until": None}
    window_start = now - cfg["IP_WINDOW"]
    ip_state["hits"] = [t for t in ip_state["hits"] if t > window_start] + [now]
    if len(ip_state["hits"]) >= cfg["IP_FAILURES"] and not _remaining(ip_state.get("until"), now):
        ip_state.update(hits=[], until=now + cfg["IP_LOCK"])
        lock = Lock(int(cfg["IP_LOCK"]))
        metrics.inc("fdr_login_lockouts_total", scope="ip")
        log.warning("login bloqueado", extra={"scope": "ip", "ip": ip, "seconds": cfg["IP_LOCK"]})
    cache.set(_ip_key(ip), ip_state, max(cfg["IP_WINDOW"], cfg["IP_LOCK"]))
    return lock


def record_success(request, username: str) -> None:
    """Login certo: zera as falhas do par usuário/IP (o histórico de bloqueios fica)."""
    if not enabled():
        return
    ip = client_ip(request) or "desconhecido"
    key = _user_key(username or "", ip)
    state = cache.get(key)
    if state:
        state.update(failures=0, until=None)
        cache.set(key, state, _cfg()["STRIKE_MEMORY"])


def lock_message(lock: Lock) -> str:
    return f"Muitas tentativas de login. Tente de novo em {lock.minutes} min."
