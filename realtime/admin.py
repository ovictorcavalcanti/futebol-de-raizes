"""Django Admin do outbox: somente leitura, só para o Administrador (realtime.view_outbox).

Fica fora do índice (é detalhe técnico do tempo real); a lista continua em
/admin/realtime/outbox/ para quem tem a permissão."""

from django.contrib import admin

from competitions.admin import HiddenFromIndexMixin
from observability.admin import AuditedModelAdmin, ReadOnlyAdminMixin, pretty_json

from .models import Outbox


class PublishedFilter(admin.SimpleListFilter):
    title = "publicada"
    parameter_name = "publicada"

    def lookups(self, request, model_admin):
        return (("sim", "Sim"), ("nao", "Ainda não"))

    def queryset(self, request, queryset):
        if self.value() == "sim":
            return queryset.filter(published_at__isnull=False)
        if self.value() == "nao":
            return queryset.filter(published_at__isnull=True)
        return queryset


@admin.register(Outbox)
class OutboxAdmin(HiddenFromIndexMixin, ReadOnlyAdminMixin, AuditedModelAdmin):
    list_display = ("id", "topic", "created_at", "published_at")
    list_filter = ("topic", PublishedFilter)
    date_hierarchy = "created_at"
    fields = ("id", "topic", "created_at", "published_at", "payload_view")
    readonly_fields = fields
    show_full_result_count = False

    def get_queryset(self, request):
        # A lista não precisa do corpo inteiro (pode ser grande): só o resumo.
        queryset = super().get_queryset(request)
        if getattr(request.resolver_match, "url_name", "").endswith("_changelist"):
            queryset = queryset.defer("payload")
        return queryset

    @admin.display(description="mensagem")
    def payload_view(self, obj):
        return pretty_json(obj.payload)
