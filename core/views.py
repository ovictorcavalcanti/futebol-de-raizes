from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

PAGE_CACHE = "public, max-age=300"


def _page(request, template, **context):
    response = render(request, template, context)
    response["Cache-Control"] = PAGE_CACHE
    return response


@require_GET
def home_page(request):
    return _page(request, "index.html", page="home")


@require_GET
def competition_page(request):
    return _page(request, "competition.html", page="competition")


@require_GET
def operator_page(request):
    response = _page(request, "operator.html", page="operator")
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def health(request):
    """Healthcheck: responde 200 quando o processo e o banco estão de pé."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # pragma: no cover - depende do banco cair
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})
