"""Admin de usuários e de perfis (grupos do Django).

* Usuários: só quem tem as permissões de `accounts.user` (Administrador) vê. Entrar
  num perfil do sistema (Operador/Administrador) já marca "Membro da equipe"
  (`accounts.signals`): o perfil sozinho define o que a pessoa faz, inclusive abrir
  o Django Admin para os cadastros.
* Perfis: os dois perfis do sistema (`accounts.roles.ROLE_PERMISSIONS`) vêm do
  código e são reaplicados a cada `migrate` (`sync_roles`). No admin eles ficam
  somente leitura (nome e permissões), sem exclusão: uma edição seria desfeita em
  silêncio na próxima atualização — perigoso quando ela tirava uma permissão. Nível
  de acesso personalizado = um grupo novo, que fica totalmente editável.
"""

from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.db.models import Count
from django.utils.html import format_html, format_html_join

from . import roles
from .models import User
from .signals import grant_staff_for_roles

SYSTEM_ROLE_NOTE = (
    "Perfil do sistema: as permissões vêm do código e são reaplicadas a cada atualização. "
    "Para um nível de acesso personalizado, crie um novo grupo."
)
GROUPS_HELP = (
    "Operador e Administrador já dão acesso ao Django Admin: “Membro da equipe” é marcado "
    "sozinho ao salvar."
)


def is_system_role(group) -> bool:
    return group is not None and group.pk is not None and group.name in roles.ROLE_PERMISSIONS


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Gestão de usuários: só quem tem as permissões de `accounts.user` (Administrador) vê."""

    list_display = ("username", "get_full_name", "email", "is_active", "is_staff", "role_names")
    list_filter = ("is_active", "is_staff", "groups")

    @admin.display(description="Perfis")
    def role_names(self, obj):
        return ", ".join(group.name for group in obj.groups.all()) or "—"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name == "groups" and field is not None:
            field.help_text = f"{field.help_text} {GROUPS_HELP}".strip()
        return field

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Perfil do sistema sem "Membro da equipe" (caixa desmarcada com os perfis já
        # gravados, sem `post_add`): o perfil manda, o acesso ao admin volta.
        grant_staff_for_roles(form.instance)


admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin):
    """Perfis. Os do sistema (Operador, Administrador) ficam somente leitura e não se
    apagam nem se renomeiam (o `migrate` criaria outro com o nome original)."""

    list_display = ("name", "kind", "members")

    @admin.display(description="tipo")
    def kind(self, obj):
        return "Perfil do sistema" if is_system_role(obj) else "Personalizado"

    @admin.display(description="usuários", ordering="member_count")
    def members(self, obj):
        return obj.member_count if hasattr(obj, "member_count") else obj.user_set.count()

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(member_count=Count("user", distinct=True))

    @admin.display(description="permissões")
    def permissions_list(self, obj):
        perms = obj.permissions.select_related("content_type").order_by("content_type__app_label", "codename")
        items = ((perm.name, perm.content_type.app_label, perm.codename) for perm in perms)
        return format_html(
            "<ul style='margin:0;padding-left:1.2em'>{}</ul>",
            format_html_join("", "<li>{} <code>{}.{}</code></li>", items),
        )

    def get_fieldsets(self, request, obj=None):
        if is_system_role(obj):
            return ((None, {"fields": ("name", "permissions_list"), "description": SYSTEM_ROLE_NOTE}),)
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if is_system_role(obj):
            return ("name", "permissions", "permissions_list")
        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        if is_system_role(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_deleted_objects(self, objs, request):
        """Exclusão em lote: perfil do sistema na seleção → a exclusão é recusada inteira
        (a página de confirmação lista o motivo e o POST responde 403)."""
        deleted, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        system = sorted(group.name for group in objs if is_system_role(group))
        if system:
            perms_needed = set(perms_needed) | {f"perfil do sistema “{name}”" for name in system}
        return deleted, model_count, perms_needed, protected
