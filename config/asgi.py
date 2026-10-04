"""Ponto de entrada ASGI: um processo uvicorn serve API, páginas e o stream SSE."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
