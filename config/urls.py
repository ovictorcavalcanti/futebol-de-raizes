from django.contrib import admin
from django.urls import path

from api.urls import urlpatterns as api_urlpatterns
from core import views as core_views
from observability import views as observability_views
from public_api.api import public_api
from realtime.views import stream

admin.site.site_header = "Futebol de Raízes · Administração"
admin.site.site_title = "Futebol de Raízes"
admin.site.index_title = "Cadastros e regras"

urlpatterns = [
    path("", core_views.home_page, name="home"),
    path("index.html", core_views.home_page),
    path("competition.html", core_views.competition_page, name="competition"),
    path("operator.html", core_views.operator_page, name="operator"),
    path("health", core_views.health, name="health"),
    path("metrics", observability_views.metrics, name="metrics"),
    path("api/stream", stream, name="stream"),
    *api_urlpatterns,
    path("public/v1/", public_api.urls),
    path("admin/", admin.site.urls),
]
