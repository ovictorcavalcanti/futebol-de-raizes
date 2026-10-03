"""Auditoria das ações no Django Admin.

Cada `LogEntry` gravado pelo admin (incluir, alterar, excluir) vira um registro
em `AuditLog`, com autor, objeto e a mensagem de alteração. Conectado em
`ObservabilityConfig.ready()`.

O admin grava exclusões em lote (ação "excluir selecionados" com mais de um
objeto) com `bulk_create`, que não dispara `post_save`: os ModelAdmin do projeto
herdam `observability.admin.AuditedModelAdmin`, que registra esses casos com
`record_bulk_entries`; os que o projeto não declara (usuários, grupos do Django)
recebem o mesmo tratamento em `audit_bulk_deletions(admin.site)`, chamado no
`ready()` (depois do autodiscover do admin).
"""

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.db.models.signals import post_save

from . import audit

ADMIN_ACTIONS = {
    ADDITION: "admin.add",
    CHANGE: "admin.change",
    DELETION: "admin.delete",
}
MATCH_OBJECT_TYPE = "matches.match"


def record_log_entry(entry: LogEntry):
    """Grava na auditoria uma entrada do log do admin. Devolve o AuditLog (ou None)."""
    action = ADMIN_ACTIONS.get(entry.action_flag)
    if action is None:
        return None
    content_type = entry.content_type
    object_type = f"{content_type.app_label}.{content_type.model}" if content_type else ""
    match_id = None
    if object_type == MATCH_OBJECT_TYPE and str(entry.object_id or "").isdigit():
        match_id = int(entry.object_id)
    return audit.record(
        action,
        actor=entry.user,
        object_type=object_type,
        object_id=entry.object_id or "",
        match_id=match_id,
        data={
            "object_repr": entry.object_repr,
            "message": entry.get_change_message(),
            "change_message": entry.change_message,
        },
    )


def record_bulk_entries(entries) -> None:
    """Entradas gravadas em lote pelo admin (`bulk_create`, sem `post_save`)."""
    if isinstance(entries, list) and len(entries) > 1:
        for entry in entries:
            record_log_entry(entry)


def audit_bulk_deletions(site) -> None:
    """Exclusão em lote auditada em todo ModelAdmin registrado no `site`, inclusive nos
    que não herdam `AuditedModelAdmin` (ex.: usuários e grupos)."""
    for model_admin in site._registry.values():
        original = model_admin.log_deletions
        if getattr(original, "fdr_audited", False):
            continue

        def log_deletions(request, queryset, _original=original):
            entries = _original(request, queryset)
            record_bulk_entries(entries)
            return entries

        log_deletions.fdr_audited = True
        model_admin.log_deletions = log_deletions


def log_entry_saved(sender, instance: LogEntry, created: bool, raw: bool = False, **kwargs):
    if created and not raw:
        record_log_entry(instance)


def connect():
    post_save.connect(log_entry_saved, sender=LogEntry, dispatch_uid="fdr_audit_admin_log_entry")
