"""Django Admin: base comum dos ModelAdmin do projeto e auditoria (somente leitura)."""

import json

from django.contrib import admin
from django.utils.html import format_html

from .models import AuditLog
from .signals import record_bulk_entries


class AuditedModelAdmin(admin.ModelAdmin):
    """Base dos ModelAdmin do projeto.

    Inclusões, alterações e exclusões avulsas chegam à auditoria pelo sinal do
    `LogEntry` (observability/signals.py). A exclusão em lote grava o log com
    `bulk_create`, sem sinal: aqui cada entrada vai para a auditoria.
    """

    def log_deletions(self, request, queryset):
        entries = super().log_deletions(request, queryset)
        record_bulk_entries(entries)
        return entries

    log_deletions.fdr_audited = True


class ReadOnlyAdminMixin:
    """Somente leitura: ninguém inclui, altera nem exclui (só quem pode ver, vê)."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def pretty_json(value) -> str:
    return format_html("<pre style=\"white-space:pre-wrap;margin:0\">{}</pre>", json.dumps(value, ensure_ascii=False, indent=2))


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, AuditedModelAdmin):
    """Auditoria: só o Administrador vê (observability.view_auditlog)."""

    list_display = ("created_at", "actor_username", "action", "object_type", "object_id", "match_id", "ip")
    list_filter = ("action", "actor", "object_type")
    search_fields = ("actor_username", "action", "object_id", "request_id")
    date_hierarchy = "created_at"
    list_select_related = ("actor",)
    fields = ("created_at", "actor", "actor_username", "action", "object_type", "object_id", "match_id", "data_view", "ip", "request_id")
    readonly_fields = fields
    show_full_result_count = False

    @admin.display(description="dados")
    def data_view(self, obj):
        return pretty_json(obj.data)
