"""Gravação de mensagens no outbox, dentro da transação de escrita.

`enqueue` exige transação aberta com a trava de escrita (core.locks). Depois do
commit, acorda o hub do processo para publicar sem esperar o polling.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.locks import holds_write_lock
from observability.metrics import metrics

from .models import Outbox


def _wake_hub():
    try:
        from .hub import hub
    except Exception:  # pragma: no cover - hub ainda não carregado
        return
    hub.wake_threadsafe()


def enqueue(topic: str, payload: dict) -> Outbox:
    if not holds_write_lock():
        raise RuntimeError("enqueue() exige transação aberta com a trava de escrita (core.locks.locked_atomic)")
    row = Outbox.objects.create(topic=topic, payload=payload)
    metrics.inc("fdr_outbox_messages_total", topic=topic)
    transaction.on_commit(_wake_hub)
    return row


def current_cursor() -> int:
    """Maior id do outbox (0 se vazio). Lido ANTES do estado nas leituras."""
    return Outbox.objects.aggregate(m=Max("id"))["m"] or 0


async def acurrent_cursor() -> int:
    result = await Outbox.objects.aaggregate(m=Max("id"))
    return result["m"] or 0


def purge_old(hours: int | None = None) -> int:
    """Apaga mensagens com mais de `hours` horas (padrão: 24)."""
    hours = hours or settings.REALTIME["OUTBOX_RETENTION_HOURS"]
    deleted, _ = Outbox.objects.filter(created_at__lt=timezone.now() - timedelta(hours=hours)).delete()
    return deleted
