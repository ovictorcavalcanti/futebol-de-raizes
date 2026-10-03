from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


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
