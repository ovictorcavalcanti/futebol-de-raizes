"""Django Admin das chaves da API pública (só o Administrador tem as permissões).

Inclusão: o formulário pede só nome e limite por minuto; ao salvar, a chave é
gerada por `ApiKey.generate` e o texto dela aparece UMA vez, na mensagem de
sucesso (só o hash fica no banco). Prefixo e hash são somente leitura.
"""

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.utils.html import format_html

from observability.admin import AuditedModelAdmin

from .models import ApiKey


def default_rate_limit() -> int:
    return settings.PUBLIC_API["DEFAULT_RATE_LIMIT_PER_MINUTE"]


class ApiKeyForm(forms.ModelForm):
    rate_limit_per_minute = forms.IntegerField(
        label="limite por minuto",
        min_value=1,
        initial=default_rate_limit,
        help_text="Requisições por minuto permitidas para esta chave.",
    )

    class Meta:
        model = ApiKey
        fields = ("name", "active", "rate_limit_per_minute")


@admin.register(ApiKey)
class ApiKeyAdmin(AuditedModelAdmin):
    form = ApiKeyForm
    list_display = ("name", "prefix", "active", "rate_limit_per_minute", "created_at", "last_used_at")
    list_filter = ("active",)
    search_fields = ("name", "prefix")
    actions = ("deactivate",)
    add_fields = ("name", "rate_limit_per_minute")
    change_fields = ("name", "prefix", "key_hash", "active", "rate_limit_per_minute", "created_at", "last_used_at")
    change_readonly = ("prefix", "key_hash", "created_at", "last_used_at")

    def get_fields(self, request, obj=None):
        return self.change_fields if obj else self.add_fields

    def get_readonly_fields(self, request, obj=None):
        return self.change_readonly if obj else ()

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            return
        created, raw = ApiKey.generate(obj.name, obj.rate_limit_per_minute)
        # O admin segue com `obj` (log da inclusão, redirecionamento): vira a chave gravada.
        for field in ("pk", "prefix", "key_hash", "active", "created_at"):
            setattr(obj, field, getattr(created, field))
        obj._state.adding = False
        obj._state.db = created._state.db
        request._fdr_raw_api_key = raw

    def response_add(self, request, obj, post_url_continue=None):
        raw = getattr(request, "_fdr_raw_api_key", None)
        if raw:
            messages.success(
                request,
                format_html(
                    "Chave de “{}”: <code>{}</code> — copie agora; ela não será mostrada de novo.",
                    obj.name,
                    raw,
                ),
            )
        return super().response_add(request, obj, post_url_continue)

    @admin.action(description="Desativar as chaves selecionadas", permissions=["change"])
    def deactivate(self, request, queryset):
        keys = list(queryset.filter(active=True))
        for api_key in keys:
            api_key.active = False
            api_key.save(update_fields=["active"])
            self.log_change(request, api_key, "Chave desativada (ação em lote).")  # → auditoria
        self.message_user(request, f"{len(keys)} chave(s) desativada(s).", messages.SUCCESS)
