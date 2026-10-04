from django.conf import settings
from django.db import connection
from django.views.static import serve
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

PAGE_CACHE = "public, max-age=300"


def _page(request, template, **context):
    response = render(request, template, context)
    response["Cache-Control"] = PAGE_CACHE
    return response


@require_safe
def home_page(request):
    return _page(request, "index.html", page="home")


@require_safe
def competition_page(request):
    return _page(request, "competition.html", page="competition")


@require_safe
def operator_page(request):
    response = _page(request, "operator.html", page="operator")
    response["Cache-Control"] = "no-store"
    return response


@require_safe
def styleguide_page(request):
    """Guia de estilo para QA visual: só existe com DEBUG ligado."""
    if not settings.DEBUG:
        raise Http404
    response = render(request, "styleguide.html", {"page": "styleguide"})
    response["Cache-Control"] = "no-store"
    return response


@require_safe
def health(request):
    """Healthcheck: responde 200 quando o processo e o banco estão de pé (GET ou HEAD)."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # pragma: no cover - depende do banco cair
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})


@require_safe
def media(request, path):
    """Arquivos enviados (escudos), lidos de MEDIA_ROOT a cada requisição."""
    response = serve(request, path, document_root=settings.MEDIA_ROOT)
    response["Cache-Control"] = "public, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    return response
