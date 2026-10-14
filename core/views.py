from django.conf import settings
from django.db import connection
from django.views.static import serve
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from core import share, timeutils
from matches import selectors
from matches.models import Match

PAGE_CACHE = "public, max-age=300"


def _page(request, template, **context):
    response = render(request, template, context)
    response["Cache-Control"] = PAGE_CACHE
    return response


@require_safe
def home_page(request):
    """Home; com ?jogo=<id> (link compartilhado), abre no dia do jogo com o card aberto e traz o
    placar e os autores dos gols na prévia do link (WhatsApp, redes). Cache curto nesse caso."""
    focus = _shared_match(request)
    response = _page(request, "index.html", page="home", **(focus or {}))
    if focus:
        response["Cache-Control"] = "public, max-age=60"
    return response


def _shared_match(request) -> dict | None:
    value = request.GET.get("jogo") or ""
    if not value.isdigit() or len(value) > 9:  # id inválido: a home de sempre
        return None
    match = Match.objects.select_related("home_team", "away_team", "stage__season__competition", "group", "round").filter(pk=int(value)).first()
    if match is None:
        return None
    data = selectors.serialize_match(match)
    title, description = share.match_preview(data)
    return {
        "share_title": title,
        "share_description": description,
        "share_url": request.build_absolute_uri(),
        "focus_day": timeutils.local_today(match.kickoff_at).isoformat() if match.kickoff_at else "",
    }


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
