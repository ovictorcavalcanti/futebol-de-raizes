from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Auditoria (fase 11): toda ação de operador, com autor e horário."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="autor")
    actor_username = models.CharField("usuário", max_length=150, blank=True)
    action = models.CharField("ação", max_length=40, db_index=True)
    object_type = models.CharField("objeto", max_length=60, blank=True)
    object_id = models.CharField("id do objeto", max_length=40, blank=True)
    match_id = models.BigIntegerField("partida", null=True, blank=True, db_index=True)
    data = models.JSONField("dados", default=dict, blank=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)
    request_id = models.CharField("requisição", max_length=64, blank=True)
    created_at = models.DateTimeField("quando", auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-created_at", "-id"]
        permissions = [("view_metrics", "Pode ver as métricas")]
        verbose_name = "registro de auditoria"
        verbose_name_plural = "auditoria"

    def __str__(self):
        return f"{self.created_at:%d/%m %H:%M} {self.actor_username} {self.action}"
