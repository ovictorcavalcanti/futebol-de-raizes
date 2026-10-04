"""Páginas do admin "Jogos": escolhe a competição e lista só os jogos dela."""

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

from competitions.models import Competition, Stage

from .models import Match


LIVE_GROUP = {Match.Status.LIVE, Match.Status.DELAYED, Match.Status.SUSPENDED}
UPCOMING_GROUP = {Match.Status.SCHEDULED, Match.Status.POSTPONED}


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
    rows = (
        Match.objects.filter(stage__season__competition=competition)
        .select_related("stage__season", "round", "group", "home_team", "away_team")
    )
    # Ao vivo (e atrasado/suspenso) primeiro, depois os agendados, encerrados por último;
    # dentro de cada bloco, pelo horário (encerrados do mais recente para o mais antigo).
    live, upcoming, done = [], [], []
    for match in rows:
        if match.status in LIVE_GROUP:
            live.append(match)
        elif match.status in UPCOMING_GROUP:
            upcoming.append(match)
        else:
            done.append(match)
    by_kickoff = lambda m: (m.kickoff_at, m.id)  # noqa: E731
    matches = sorted(live, key=by_kickoff) + sorted(upcoming, key=by_kickoff) + sorted(done, key=by_kickoff, reverse=True)
    # "Adicionar jogo": abre o cadastro já na fase atual da temporada mais recente
    # (a primeira com jogo não encerrado, senão a última).
    latest = Stage.objects.filter(season__competition=competition).order_by("-season__year").values_list("season_id", flat=True).first()
    stages = list(Stage.objects.filter(season_id=latest).order_by("position", "id")) if latest else []
    open_stage_ids = {m.stage_id for m in live + upcoming}
    add_stage = next((st for st in stages if st.id in open_stage_ids), stages[-1] if stages else None)
    context = {
        **admin.site.each_context(request),
        "add_stage": add_stage,
        "can_add": request.user.has_perm("matches.add_match"),
        "title": f"Jogos · {competition.name}",
        "competition": competition,
        "matches": matches,
    }
    return render(request, "admin/fdr/games_competition.html", context)
