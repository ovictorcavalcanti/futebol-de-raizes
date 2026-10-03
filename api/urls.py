from django.urls import path

from .main import api

urlpatterns = [path("api/", api.urls)]
