"""Auditoria: registra cada ação de operador com autor, horário e dados.

Chamada pelos serviços de escrita dentro da mesma transação, para que a ação e
o registro sejam gravados (ou descartados) juntos.
"""

from .logging import get_logger, request_id_var
from .metrics import metrics

log = get_logger("audit")


def client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def record(action: str, *, actor=None, obj=None, object_type: str = "", object_id="", match_id=None, data=None, request=None):
    from .models import AuditLog

    if obj is not None:
        object_type = object_type or obj._meta.label_lower
        object_id = object_id or str(obj.pk)
    user = actor if actor is not None and getattr(actor, "is_authenticated", False) else None
    entry = AuditLog.objects.create(
        actor=user,
        actor_username=getattr(actor, "username", "") or "",
        action=action,
        object_type=object_type,
        object_id=str(object_id or ""),
        match_id=match_id,
        data=data or {},
        ip=client_ip(request),
        request_id=request_id_var.get()[:64],  # o middleware já valida; segunda trava (varchar 64)
    )
    metrics.inc("fdr_audit_records_total", action=action)
    log.info("audit", extra={"action": action, "actor": entry.actor_username, "object_type": object_type, "object_id": entry.object_id, "match_id": match_id})
    return entry
