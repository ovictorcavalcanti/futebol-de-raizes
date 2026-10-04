"""Páginas do admin "Jogos": escolhe a competição e lista só os jogos dela."""

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

from competitions.models import Competition

from .models import Match


def _check(request):
    if not request.user.has_perm("matches.view_match"):
        raise PermissionDenied


def games_index(request):
    _check(request)
    context = {**admin.site.each_context(request), "title": "Jogos", "competitions": Competition.objects.all()}
    return render(request, "admin/fdr/games_index.html", context)


def games_competition(request, slug):
    _check(request)
    competition = get_object_or_404(Competition, slug=slug)
    matches = (
        Match.objects.filter(stage__season__competition=competition)
        .select_related("stage__season", "round", "group", "home_team", "away_team")
        .order_by("-stage__season__year", "stage__position", "round__number", "kickoff_at", "id")
    )
    context = {
        **admin.site.each_context(request),
        "title": f"Jogos · {competition.name}",
        "competition": competition,
        "matches": matches,
    }
    return render(request, "admin/fdr/games_competition.html", context)
