from django.conf import settings
from django.contrib import admin
from django.urls import path

from accounts.forms import ThrottledAdminAuthenticationForm
from api.urls import urlpatterns as api_urlpatterns
from core import views as core_views
from observability import views as observability_views
from public_api.urls import urlpatterns as public_urlpatterns
from realtime.views import stream

admin.site.site_header = f"{settings.BRAND['name']} · Administração"  # BRAND_NAME
admin.site.site_title = settings.BRAND["name"]
admin.site.index_title = "Cadastros e regras"
admin.site.login_form = ThrottledAdminAuthenticationForm  # avisa o bloqueio de login
# Navegação por competição: sem a barra lateral com todas as listas (o índice é o ponto de partida).
admin.site.enable_nav_sidebar = False
ADMIN_APP_ORDER = ("competitions", "accounts", "auth", "public_api", "observability")


def _ordered_app_list(request, app_label=None, _original=admin.site.get_app_list):
    """Índice com Competições (e Times) primeiro; depois usuários, perfis, chaves e auditoria."""
    apps = _original(request, app_label)
    rank = {label: n for n, label in enumerate(ADMIN_APP_ORDER)}
    return sorted(apps, key=lambda app: (rank.get(app["app_label"], len(rank)), app["name"]))


admin.site.get_app_list = _ordered_app_list

urlpatterns = [
    path("", core_views.home_page, name="home"),
    path("index.html", core_views.home_page),
    path("competition.html", core_views.competition_page, name="competition"),
    path("operator.html", core_views.operator_page, name="operator"),
    path("styleguide.html", core_views.styleguide_page, name="styleguide"),
    path("health", core_views.health, name="health"),
    path("metrics", observability_views.metrics, name="metrics"),
    path("api/stream", stream, name="stream"),
    *api_urlpatterns,
    *public_urlpatterns,  # /public/v1/ (+ 404/405 em JSON)
    path("admin/", admin.site.urls),
]
