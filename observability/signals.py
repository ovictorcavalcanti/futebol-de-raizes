"""Auditoria das ações no Django Admin e das sessões (login/logout).

Cada `LogEntry` gravado pelo admin (incluir, alterar, excluir) vira um registro
em `AuditLog`, com autor, objeto e a mensagem de alteração. Conectado em
`ObservabilityConfig.ready()`.

Sessões: os sinais de autenticação do Django gravam `auth.login`, `auth.logout` e
`auth.login_failed` (`data.username`, o usuário digitado; a senha nunca) em
qualquer porta de entrada — `/api/auth/*` e `/admin/login/`/`/admin/logout/`.
A troca da própria senha em `/admin/password_change/` grava `auth.password_change`
(`audit_password_change(admin.site)`, também chamado no `ready()`); a troca da
senha de outro usuário pelo admin de usuários já vira `LogEntry`.

O admin grava exclusões em lote (ação "excluir selecionados" com mais de um
objeto) com `bulk_create`, que não dispara `post_save`: os ModelAdmin do projeto
herdam `observability.admin.AuditedModelAdmin`, que registra esses casos com
`record_bulk_entries`; os que o projeto não declara (usuários, grupos do Django)
recebem o mesmo tratamento em `audit_bulk_deletions(admin.site)`, chamado no
`ready()` (depois do autodiscover do admin).
"""

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
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


# --- Sessões ---------------------------------------------------------------------------

USERNAME_MAX_LENGTH = 150  # o mesmo do campo do usuário: nada maior vai para a auditoria


def logged_in(sender, request, user, **kwargs):
    audit.record("auth.login", actor=user, obj=user, request=request)


def logged_out(sender, request, user, **kwargs):
    if user is None:  # logout sem sessão: nada a registrar
        return
    audit.record("auth.logout", actor=user, obj=user, request=request)


def login_failed(sender, credentials, request=None, **kwargs):
    # O Django já mascara senha, token e chaves em `credentials`; só o usuário digitado é gravado.
    username = credentials.get(get_user_model().USERNAME_FIELD, "") if isinstance(credentials, dict) else ""
    audit.record(
        "auth.login_failed",
        object_type="accounts.user",
        data={"username": str(username or "")[:USERNAME_MAX_LENGTH]},
        request=request,
    )


def audit_password_change(site) -> None:
    """Troca da própria senha no admin (`/admin/password_change/`) entra na auditoria:
    POST respondido com redirecionamento = formulário válido, senha trocada."""
    original = site.password_change
    if getattr(original, "fdr_audited", False):
        return

    def password_change(request, extra_context=None):
        response = original(request, extra_context)
        if request.method == "POST" and response.status_code == 302 and request.user.is_authenticated:
            audit.record("auth.password_change", actor=request.user, obj=request.user, request=request)
        return response

    password_change.fdr_audited = True
    # Atributo da instância: `AdminSite.get_urls()` (montado depois do `ready()`) usa este.
    site.password_change = password_change


def connect():
    post_save.connect(log_entry_saved, sender=LogEntry, dispatch_uid="fdr_audit_admin_log_entry")
    user_logged_in.connect(logged_in, dispatch_uid="fdr_audit_user_logged_in")
    user_logged_out.connect(logged_out, dispatch_uid="fdr_audit_user_logged_out")
    user_login_failed.connect(login_failed, dispatch_uid="fdr_audit_user_login_failed")
