"""Django Admin simplificado: navegação por competição, lances dentro da partida,
punições em pontos e o fim do cadastro de jogadores.

* Índice só com os pontos de entrada (Competições, Times; usuários, perfis, chaves e
  auditoria para quem pode); o resto se abre descendo a hierarquia, com a trilha
  Início › Competição › Temporada › Fase › Rodada › Jogo.
* Rodada: jogos (e confrontos, no mata-mata) cadastrados ali, pelo mesmo caminho de escrita.
* Partida: "Cancelar lançamento" por linha chama `services.void_event`; link para a tela
  do operador.
* Fase: punição/bonificação recalcula e publica a classificação (API e API pública mostram).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from competitions.models import Competition, Group, GroupTeam, Round, Season, Stage, Team
from matches.models import Match, MatchEvent, MatchLineup, Tie
from observability.models import AuditLog
from public_api.models import ApiKey
from realtime.models import Outbox
from standings.models import PointAdjustment, Standing
from tests.factories import make_knockout, make_league, make_match, make_stage, make_tie_matches
from tests.test_admin import (
    Op,
    change_url,
    finish,
    last_outbox_id,
    messages_text,
    post_data,
    set_formset_rows,
    url,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def league(db):
    return make_league(
        n_teams=4,
        team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"],
        zones=[{"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 2}],
    )


def inline_prefix(response, model):
    return next(item.formset.prefix for item in response.context["inline_admin_formsets"] if item.formset.model is model)


# --- Índice e hierarquia ------------------------------------------------------------------------


def test_admin_index_shows_only_top_level_entries(admin_client_fdr, operator_client):
    hidden = (Season, Stage, Group, Round, Match, Tie, MatchLineup, Outbox)
    page = admin_client_fdr.get(reverse("admin:index")).content.decode()
    for model in (Competition, Team, get_user_model(), AuditLog, ApiKey):
        assert url(model) in page, model
    assert reverse("admin:auth_group_changelist") in page
    for model in hidden:
        assert url(model) not in page, model
    assert "Comece por" in page

    page = operator_client.get(reverse("admin:index")).content.decode()
    assert url(Competition) in page and url(Team) in page
    for model in (*hidden, get_user_model(), AuditLog, ApiKey):
        assert url(model) not in page, model
    # As páginas escondidas continuam com endereço e permissão.
    assert operator_client.get(url(Match)).status_code == 200
    assert operator_client.get(reverse("admin:app_list", args=["matches"])).status_code == 200


@pytest.fixture
def tree(league, operator_user):
    """Liga com jogo encerrado e escalação, fase de grupos e mata-mata com confronto e jogo."""
    sport, nautico, santa, retro = league["teams"]
    match = make_match(league["stage"], sport, nautico, round=league["rounds"][0])
    finish(match, operator_user, 1, 0)
    lineup = MatchLineup.objects.create(match=match, team=sport, formation="4-3-3")
    groups_stage = make_stage(league["season"], Stage.Format.GROUPS, name="Grupos", position=2)
    group = Group.objects.create(stage=groups_stage, name="Grupo A")
    GroupTeam.objects.create(group=group, team=sport)
    Round.objects.create(stage=groups_stage, number=1, name="Rodada 1")
    knockout = make_knockout(league["season"], legs=1, extra_time=False, team_a=santa, team_b=retro, position=3)
    (ko_match,) = make_tie_matches(knockout["tie"])
    return {**league, "match": match, "lineup": lineup, "groups_stage": groups_stage, "group_a": group, "knockout": knockout, "ko_match": ko_match}


@pytest.mark.parametrize("profile", ["operator", "admin"])
def test_drill_down_pages_load_for_both_profiles(profile, tree, operator_user, admin_user_fdr):
    client = Client()
    client.force_login(operator_user if profile == "operator" else admin_user_fdr)
    competition = tree["competition"]
    page = client.get(change_url(competition)).content.decode()
    assert change_url(tree["season"]) in page and change_url(tree["stage"]) in page  # painel das fases
    assert f'{url(Stage, "add")}?season={tree["season"].pk}' in page  # "+ adicionar fase" já com a temporada
    page = client.get(change_url(tree["season"])).content.decode()
    assert change_url(tree["stage"]) in page and "abrir" in page
    page = client.get(change_url(tree["stage"])).content.decode()
    assert change_url(tree["rounds"][0]) in page and "Jogos da rodada (1)" in page
    assert change_url(tree["group"]) in page and "Times do grupo (4)" in page
    assert "a ao vivo inclui" in page  # de onde vem a classificação
    page = client.get(change_url(tree["groups_stage"])).content.decode()
    assert change_url(tree["group_a"]) in page
    page = client.get(change_url(tree["knockout"]["stage"])).content.decode()
    assert "Confrontos e jogos (1 confrontos, 1 jogos)" in page
    page = client.get(change_url(tree["rounds"][0])).content.decode()
    assert change_url(tree["match"]) in page and "1 × 0" in page
    response = client.get(change_url(tree["knockout"]["round"]))
    assert {"ties", "matches"} <= {item.formset.prefix for item in response.context["inline_admin_formsets"]}
    for obj in (tree["group"], tree["match"], tree["knockout"]["tie"], tree["ko_match"], tree["lineup"]):
        assert client.get(change_url(obj)).status_code == 200, obj
    assert client.get(url(Stage, "add") + f"?season={tree['season'].pk}").status_code == 200


def breadcrumb_text(page: str) -> str:
    start = page.index('<div class="breadcrumbs"')
    block = page[start : page.index("</div>", start)]
    import re

    return " ".join(re.sub(r"<[^>]+>", " ", block).split())


def test_breadcrumbs_follow_the_hierarchy(admin_client_fdr, tree):
    page = admin_client_fdr.get(change_url(tree["match"])).content.decode()
    competition = tree["competition"]
    match = tree["match"]
    game = f"Jogo {match.home_team.short_name} × {match.away_team.short_name}"
    expected = f"Início › {competition.name} › Temporada 2026 › 1ª fase › Rodada 1 › {game}"
    assert breadcrumb_text(page).replace("&rsaquo;", "›") == expected
    for obj in (competition, tree["season"], tree["stage"], tree["rounds"][0]):
        assert f'href="{change_url(obj)}"' in page
    page = admin_client_fdr.get(change_url(tree["lineup"])).content.decode()
    assert breadcrumb_text(page).replace("&rsaquo;", "›").endswith(f"Rodada 1 › {game} › Escalação: Sport")
    page = admin_client_fdr.get(change_url(tree["knockout"]["tie"])).content.decode()
    assert breadcrumb_text(page).replace("&rsaquo;", "›").endswith("Mata-mata › Final › Confronto: Santa Cruz × Retrô")
    page = admin_client_fdr.get(url(Round, "add") + f"?stage={tree['stage'].pk}").content.decode()
    assert breadcrumb_text(page).replace("&rsaquo;", "›").endswith("1ª fase › Adicionar rodada")


def test_save_returns_to_the_level_above(admin_client_fdr, tree):
    match = tree["match"]
    response = admin_client_fdr.post(change_url(match), post_data(admin_client_fdr.get(change_url(match))))
    assert response.status_code == 302 and response["Location"] == change_url(tree["rounds"][0])
    rnd = tree["rounds"][0]
    response = admin_client_fdr.post(change_url(rnd), post_data(admin_client_fdr.get(change_url(rnd))))
    assert response.status_code == 302 and response["Location"] == change_url(tree["stage"])
    data = post_data(admin_client_fdr.get(change_url(rnd)))
    data["_continue"] = "1"
    assert admin_client_fdr.post(change_url(rnd), data)["Location"] == change_url(rnd)


# --- Rodada: jogos e confrontos -------------------------------------------------------------------


def test_round_page_adds_league_match_through_write_path(admin_client_fdr, league):
    sport, nautico = league["teams"][:2]
    rnd = league["rounds"][1]
    response = admin_client_fdr.get(change_url(rnd))
    prefix = inline_prefix(response, Match)
    assert "group" not in response.context["inline_admin_formsets"][0].formset.form.base_fields  # grupo único automático
    data = post_data(response)
    set_formset_rows(
        data,
        prefix,
        [{"home_team": sport.pk, "away_team": nautico.pk, "kickoff_at_0": "2026-10-10", "kickoff_at_1": "16:00:00", "venue": "Ilha do Retiro", "city": "Recife, PE"}],
    )
    mark = last_outbox_id()
    response = admin_client_fdr.post(change_url(rnd), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    match = Match.objects.get(round=rnd)
    assert (match.stage_id, match.group_id, match.venue) == (league["stage"].pk, league["group"].pk, "Ilha do Retiro")
    message = Outbox.objects.filter(id__gt=mark, topic="match").get().payload
    assert message["match"]["id"] == match.pk and message["match"]["round"]["id"] == rnd.pk
    assert AuditLog.objects.filter(action="match.edit", match_id=match.pk).exists()
    assert AuditLog.objects.filter(action="admin.change", object_type="competitions.round", object_id=str(rnd.pk)).exists()


def test_round_page_rejects_teams_outside_the_group_and_events_deletion(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    outsider = Team.objects.create(name="Central", short_name="CEN")
    rnd = league["rounds"][0]
    match = make_match(league["stage"], sport, nautico, round=rnd)
    finish(match, operator_user, 1, 0)
    response = admin_client_fdr.get(change_url(rnd))
    prefix = inline_prefix(response, Match)
    data = post_data(response)
    data[f"{prefix}-0-away_team"] = str(outsider.pk)
    response = admin_client_fdr.post(change_url(rnd), data)
    assert response.status_code == 200
    assert "Central não está no grupo “Tabela”" in response.content.decode()
    data = post_data(admin_client_fdr.get(change_url(rnd)))
    data[f"{prefix}-0-DELETE"] = "on"
    response = admin_client_fdr.post(change_url(rnd), data)
    assert response.status_code == 200
    assert "Partida com lançamentos não se apaga" in response.content.decode()
    assert Match.objects.filter(pk=match.pk).exists()


def test_knockout_round_page_adds_tie_and_its_match(admin_client_fdr, league):
    santa, retro = league["teams"][2:]
    stage = make_stage(league["season"], Stage.Format.KNOCKOUT, name="Mata-mata", position=2)
    rnd = Round.objects.create(stage=stage, number=1, name="Final")
    other_round = Round.objects.create(stage=stage, number=2, name="Outra")
    foreign = Tie.objects.create(stage=stage, round=other_round, position=1, legs=1, extra_time=False, team_a=league["teams"][0], team_b=league["teams"][1])
    response = admin_client_fdr.get(change_url(rnd))
    data = post_data(response)
    set_formset_rows(data, "ties", [{"position": 1, "legs": 1, "extra_time": "False", "team_a": santa.pk, "team_b": retro.pk}])
    response = admin_client_fdr.post(change_url(rnd), {**data, "_continue": "1"})
    assert response.status_code == 302, response.content.decode()[:3000]
    tie = Tie.objects.get(round=rnd)
    assert tie.stage_id == stage.pk

    response = admin_client_fdr.get(change_url(rnd))
    match_field = next(item for item in response.context["inline_admin_formsets"] if item.formset.model is Match).formset.form.base_fields
    assert list(match_field["tie"].queryset) == [tie]  # só os confrontos da rodada
    assert foreign not in match_field["tie"].queryset
    data = post_data(response)
    data["ties-0-extra_time"] = "False"  # o select envia "False" (post_data omite booleanos falsos)
    set_formset_rows(
        data, "matches",
        [{"tie": tie.pk, "leg": 1, "home_team": santa.pk, "away_team": retro.pk, "kickoff_at_0": "2026-10-10", "kickoff_at_1": "16:00:00", "venue": "Arruda", "city": "Recife, PE"}],
    )
    mark = last_outbox_id()
    response = admin_client_fdr.post(change_url(rnd), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    match = Match.objects.get(tie=tie)
    assert (match.round_id, match.leg, match.group_id) == (rnd.pk, 1, None)
    assert Outbox.objects.filter(id__gt=mark, topic="match").last().payload["match"]["tie"]["id"] == tie.pk


# --- Partida: lances e cancelamento --------------------------------------------------------------


def void_via_match_page(client, match, events, follow=True):
    """Marca "Cancelar lançamento" nas linhas de `events` e salva a página da partida."""
    response = client.get(change_url(match))
    prefix = inline_prefix(response, MatchEvent)
    data = post_data(response)
    ids = {str(event.pk) for event in events}
    for index in range(int(data[f"{prefix}-TOTAL_FORMS"])):
        if data.get(f"{prefix}-{index}-id") in ids:
            data[f"{prefix}-{index}-void"] = "on"
    return client.post(change_url(match), data, follow=follow)


def test_match_page_lists_visible_events_and_links_to_operator(admin_client_fdr, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    op = Op(match, operator_user)
    op.post("match_start")
    wrong = op.post("goal", minute=10, team_id=sport.id, payload={"player": "Lance Errado"}).event
    op.post("goal", minute=12, team_id=sport.id, payload={"player": "Zé Roberto", "origin": "penalty"})
    from matches import services

    services.void_event(match.id, wrong.id, operator_user)
    response = admin_client_fdr.get(change_url(match))
    page = response.content.decode()
    assert "Zé Roberto · Pênalti" in page and "Lance Errado" not in page  # cancelado não aparece
    assert "Cancelar lançamento" in page
    operator = f"/operator.html?date=2026-10-03&amp;match={match.pk}"
    assert page.count(operator) == 2  # botão no topo e no aviso
    assert "Lançar lances na tela do operador" in page


def test_void_through_match_inline_uses_the_service(operator_client, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    op = Op(match, operator_user)
    op.post("match_start")
    goal = op.post("goal", minute=10, team_id=sport.id, payload={"player": "Zé"}).event
    start = MatchEvent.objects.get(match=match, type="match_start")
    mark = last_outbox_id()
    response = void_via_match_page(operator_client, match, [goal, start])
    assert response.redirect_chain[-1][0] == change_url(match)  # fica na partida
    goal.refresh_from_db()
    start.refresh_from_db()
    assert goal.voided_at is not None and goal.voided_by == operator_user
    assert start.voided_at is not None  # do mais novo ao mais antigo: o início cai depois do gol
    match.refresh_from_db()
    assert (match.home_score, match.status) == (0, "scheduled")
    assert "2 lançamento(s) cancelado(s)" in messages_text(response)
    assert AuditLog.objects.filter(action="event.void", match_id=match.id).count() == 2
    assert Outbox.objects.filter(id__gt=mark, topic="match").exists()
    assert MatchEvent.objects.filter(match=match).count() == 2  # nada apagado


def test_void_through_match_inline_reports_domain_error(operator_client, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    op = Op(match, operator_user)
    op.post("match_start")
    op.post("goal", minute=10, team_id=sport.id, payload={"player": "Zé"})
    start = MatchEvent.objects.get(match=match, type="match_start")
    response = void_via_match_page(operator_client, match, [start])
    start.refresh_from_db()
    assert start.voided_at is None
    assert "Cancelar este lançamento deixa a sequência inválida" in messages_text(response)


def test_void_yellow_and_its_automatic_red_together(operator_client, league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    op = Op(match, operator_user)
    op.post("match_start")
    op.post("yellow_card", minute=10, team_id=sport.id, payload={"player": "Zé"})
    second = op.post("yellow_card", minute=20, team_id=sport.id, payload={"player": "Zé"})
    red = second.derived[0]
    response = void_via_match_page(operator_client, match, [second.event, red])
    text = messages_text(response)
    assert "1 lançamento(s) cancelado(s)" in text and "Não deu" not in text
    assert MatchEvent.objects.filter(pk__in=[second.event.pk, red.pk], voided_at__isnull=False).count() == 2


def test_void_needs_the_permission(league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico)
    op = Op(match, operator_user)
    op.post("match_start")
    User = get_user_model()
    editor = User.objects.create_user("editor", password="senha-forte-123", is_staff=True)
    editor.user_permissions.add(*Permission.objects.filter(codename__in=["view_match", "change_match", "view_matchevent"]))
    client = Client()
    client.force_login(editor)
    response = client.get(change_url(match))
    page = response.content.decode()
    assert response.status_code == 200 and "Início de jogo" in page
    assert "events-0-void" not in page and "Lançar lances na tela do operador" not in page
    data = post_data(response)
    data["events-0-void"] = "on"  # forjado: o formulário sem permissão não tem a caixa
    client.post(change_url(match), data)
    assert not MatchEvent.objects.filter(match=match, voided_at__isnull=False).exists()


# --- Punição em pontos ------------------------------------------------------------------------


def test_point_adjustment_end_to_end(admin_client_fdr, league, operator_user, settings):
    sport, nautico, santa, retro = league["teams"]
    stage = league["stage"]
    finish(make_match(stage, santa, retro), operator_user, 1, 0)  # Santa Cruz 3 pts
    finish(make_match(stage, sport, nautico), operator_user, 0, 0)  # Sport 1 pt
    assert Standing.objects.get(group=league["group"], kind="official", team=santa).position == 1
    response = admin_client_fdr.get(change_url(stage))
    assert "point_adjustments" in {item.formset.prefix for item in response.context["inline_admin_formsets"]}
    team_field = next(item for item in response.context["inline_admin_formsets"] if item.formset.model is PointAdjustment).formset.form.base_fields["team"]
    assert set(team_field.queryset) == set(league["teams"])
    data = post_data(response)

    outsider = Team.objects.create(name="Central", short_name="CEN")
    set_formset_rows(data, "point_adjustments", [{"team": outsider.pk, "points": -3, "reason": "escalação irregular"}])
    assert admin_client_fdr.post(change_url(stage), data).status_code == 200  # fora da fase: recusado
    set_formset_rows(data, "point_adjustments", [{"team": santa.pk, "points": 0, "reason": "nada"}])
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200 and "diferente de zero" in response.content.decode()
    assert not PointAdjustment.objects.exists()

    mark = last_outbox_id()
    set_formset_rows(data, "point_adjustments", [{"team": santa.pk, "points": -3, "reason": "escalação irregular"}])
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 302, response.content.decode()[:3000]
    row = Standing.objects.get(group=league["group"], kind="official", team=santa)
    assert (row.points, row.adjustment, row.position) == (0, -3, 3)  # a punição muda a ordem
    message = Outbox.objects.filter(id__gt=mark, topic="standings").last().payload
    rows = {item["team"]["name"]: item for item in message["standings"]["groups"][0]["rows"]}
    assert (rows["Santa Cruz"]["points"], rows["Santa Cruz"]["points_adjustment"]) == (0, -3)
    assert rows["Sport"]["points_adjustment"] == 0
    assert message["standings"]["adjustments"] == [
        {"team": message["standings"]["adjustments"][0]["team"], "points": -3, "reason": "escalação irregular"}
    ]
    assert message["standings"]["adjustments"][0]["team"]["name"] == "Santa Cruz"
    assert AuditLog.objects.filter(action="admin.change", object_type="competitions.stage", object_id=str(stage.pk)).exists()

    body = Client().get(f"/api/stages/{stage.pk}/standings?live=1").json()
    assert body["adjustments"][0]["points"] == -3
    assert {item["team"]["name"]: item["points_adjustment"] for item in body["groups"][0]["rows"]}["Santa Cruz"] == -3
    _, raw = ApiKey.generate("Parceiro", 1000)
    public = Client().get(f"/public/v1/stages/{stage.pk}/standings", HTTP_X_API_KEY=raw).json()
    assert public["adjustments"][0]["reason"] == "escalação irregular"
    assert {item["team"]["name"]: item["points_adjustment"] for item in public["groups"][0]["rows"]}["Santa Cruz"] == -3

    # Apagar a punição devolve os pontos (e publica de novo).
    mark = last_outbox_id()
    response = admin_client_fdr.get(change_url(stage))
    data = post_data(response)
    data["point_adjustments-0-DELETE"] = "on"
    assert admin_client_fdr.post(change_url(stage), data).status_code == 302
    assert Standing.objects.get(group=league["group"], kind="official", team=santa).points == 3
    message = Outbox.objects.filter(id__gt=mark, topic="standings").last().payload
    assert message["standings"]["adjustments"] == []


def test_stage_with_adjustments_cannot_become_knockout(admin_client_fdr, league):
    stage = league["stage"]
    PointAdjustment.objects.create(stage=stage, team=league["teams"][0], points=-1, reason="W.O.")
    GroupTeam.objects.filter(group=league["group"]).delete()
    data = post_data(admin_client_fdr.get(change_url(stage)))
    data["format"] = "knockout"
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200
    assert "punições em pontos" in response.content.decode()
