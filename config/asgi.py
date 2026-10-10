"""Ponto de entrada ASGI: um processo uvicorn serve API, páginas e o stream SSE."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()

# Depois do setup do Django: devolve as vagas do stream SSE no fim de cada requisição.
from realtime.views import release_stream_slots  # noqa: E402

application = release_stream_slots(application)
