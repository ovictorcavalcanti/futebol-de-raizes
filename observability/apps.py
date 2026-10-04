from django.apps import AppConfig


class ObservabilityConfig(AppConfig):
    name = "observability"
    verbose_name = "Operação e auditoria"

    def ready(self):
        # Ações do Django Admin (LogEntry), sessões (login/logout/falha) e troca de senha
        # entram na auditoria. O admin já fez o autodiscover (vem antes em
        # INSTALLED_APPS): todo ModelAdmin está registrado.
        from django.contrib import admin

        from . import signals

        signals.connect()
        signals.audit_bulk_deletions(admin.site)
        signals.audit_password_change(admin.site)
