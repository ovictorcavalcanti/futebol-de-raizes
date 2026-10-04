"""Django Admin da classificação gravada: somente leitura.

A tabela é um cache recalculado a partir das partidas (standings/services.py);
ninguém a edita à mão. Regras (pontuação, critérios, zonas) ficam na fase.
"""

from django.contrib import admin

from observability.admin import AuditedModelAdmin, ReadOnlyAdminMixin

from .models import Standing


@admin.register(Standing)
class StandingAdmin(ReadOnlyAdminMixin, AuditedModelAdmin):
    list_display = (
        "position",
        "team",
        "points",
        "played",
        "won",
        "drawn",
        "lost",
        "goals_for",
        "goals_against",
        "goal_difference_display",
        "yellow_cards",
        "red_cards",
        "tied",
        "kind",
        "group",
    )
    list_display_links = ("position", "team")
    list_filter = ("kind", "group__stage__season__competition", "group__stage")
    search_fields = ("team__name", "team__short_name", "group__name")
    list_select_related = ("team", "group__stage__season__competition")
    ordering = ("group", "kind", "position")
    show_full_result_count = False

    @admin.display(description="SG")
    def goal_difference_display(self, obj):
        return obj.goal_difference
