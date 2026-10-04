"""Classificação (standings/services.py): recálculo do cache nas duas visões, cartões
contados pelos eventos visíveis, StageStandingsOut (critérios, legenda, zona,
`playing`), cálculo na hora sem cache e regras da fase (validação, recálculo, publicação)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from competitions.models import GroupTeam, StageCriterion, StandingZone
from core import timeutils
from core.locks import locked_atomic
from matches.models import Match
from realtime.models import Outbox
from standings import services
from standings.domain import ConfigError
from standings.models import Standing
from tests.factories import make_knockout, make_league, make_match, make_stage, make_team
from tests.test_services import Op

pytestmark = pytest.mark.django_db

ROW_KEYS = {
    "position", "team", "played", "won", "drawn", "lost", "goals_for", "goals_against", "goal_difference",
    "points", "yellow_cards", "red_cards", "tied", "zone", "playing",
}
ZONES = [
    {"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 2},
    {"name": "Rebaixados", "color": "#B3261E", "position_from": 4, "position_to": 4},
]


def finish(match, home, away):
    """Partida encerrada direto no cache (o recálculo lê status e placar)."""
    Match.objects.filter(pk=match.pk).update(status="finished", home_score=home, away_score=away)


@pytest.fixture
def league(db):
    return make_league(n_teams=4, team_names=["Sport", "Náutico", "Santa Cruz", "Íbis"], zones=ZONES)


def test_recompute_group_writes_official_and_live(league, operator_user):
    sport, nautico, santa, ibis = league["teams"]
    finish(make_match(league["stage"], sport, nautico), 2, 0)
    live = make_match(league["stage"], santa, ibis, kickoff_at=timeutils.now() - timedelta(minutes=30))
    op = Op(live, operator_user)
    op.post("match_start")
    op.goal(ibis, 5)

    tables = services.recompute_group(league["group"])
    assert set(tables) == {"official", "live"}
    official = {row.team_id: row for row in Standing.objects.filter(group=league["group"], kind="official")}
    live_rows = {row.team_id: row for row in Standing.objects.filter(group=league["group"], kind="live")}
    assert len(official) == len(live_rows) == 4
    assert official[sport.id].points == 3 and official[ibis.id].played == 0
    assert live_rows[ibis.id].points == 3 and live_rows[ibis.id].played == 1
    assert [row.team_id for row in Standing.objects.filter(group=league["group"], kind="live").order_by("position")][:2] == [sport.id, ibis.id]


def test_cards_counted_from_visible_events_only(league, operator_user):
    sport, nautico = league["teams"][:2]
    match = make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now() - timedelta(minutes=30))
    op = Op(match, operator_user)
    op.post("match_start")
    op.post("yellow_card", team_id=sport.id, minute=3, payload={"player": "A"})
    wrong = op.post("yellow_card", team_id=sport.id, minute=4, payload={"player": "B"})
    op.post("red_card", team_id=nautico.id, minute=5, payload={"player": "C"})
    op.void(wrong.event.id)
    row = Standing.objects.get(group=league["group"], kind="live", team=sport)
    assert (row.yellow_cards, row.red_cards) == (1, 0)
    assert Standing.objects.get(group=league["group"], kind="live", team=nautico).red_cards == 1
    assert Standing.objects.get(group=league["group"], kind="official", team=sport).yellow_cards == 0


def test_stage_standings_shape_criteria_legend_zones_and_playing(league, operator_user):
    sport, nautico, santa, ibis = league["teams"]
    finish(make_match(league["stage"], sport, nautico), 3, 1)
    finish(make_match(league["stage"], santa, ibis), 1, 1)
    playing = make_match(league["stage"], nautico, santa, kickoff_at=timeutils.now() - timedelta(minutes=10))
    Op(playing, operator_user).post("match_start")

    table = services.stage_standings(league["stage"])
    assert set(table) == {"stage_id", "stage_name", "kind", "points", "criteria", "legend", "groups"}
    assert table["kind"] == "live" and table["stage_name"] == "1ª fase"
    assert table["points"] == {"win": 3, "draw": 1, "loss": 0}
    assert table["criteria"] == [
        {"key": "points", "label": "Pontos"},
        {"key": "wins", "label": "Vitórias"},
        {"key": "goal_difference", "label": "Saldo de gols"},
        {"key": "goals_for", "label": "Gols pró"},
        {"key": "head_to_head", "label": "Confronto direto"},
    ]
    assert table["legend"] == [
        {"name": "Classificados", "color": "#1B7F3B", "from": 1, "to": 2},
        {"name": "Rebaixados", "color": "#B3261E", "from": 4, "to": 4},
    ]
    (group,) = table["groups"]
    assert group == {"id": league["group"].id, "name": "Tabela", "rows": group["rows"]}
    rows = group["rows"]
    assert all(set(row) == ROW_KEYS for row in rows)
    assert [row["team"]["name"] for row in rows] == ["Sport", "Santa Cruz", "Íbis", "Náutico"]
    assert rows[0]["zone"] == {"name": "Classificados", "color": "#1B7F3B"} and rows[0]["goal_difference"] == 2
    assert rows[2]["zone"] is None and rows[3]["zone"] == {"name": "Rebaixados", "color": "#B3261E"}
    assert {row["team"]["name"] for row in rows if row["playing"]} == {"Náutico", "Santa Cruz"}
    assert rows[0]["team"]["short_name"] == sport.short_name

    official = services.stage_standings(league["stage"], live=False)
    assert official["kind"] == "official"
    # oficial: só os encerrados contam; o jogo em andamento não entra (mas `playing` continua)
    assert [row["played"] for row in official["groups"][0]["rows"]] == [1, 1, 1, 1]
    assert {row["team"]["name"] for row in official["groups"][0]["rows"] if row["playing"]} == {"Náutico", "Santa Cruz"}


def test_rows_computed_on_the_fly_without_cache_or_with_stale_cache(league):
    assert not Standing.objects.exists()
    table = services.stage_standings(league["stage"])
    rows = table["groups"][0]["rows"]
    assert [row["team"]["name"] for row in rows] == ["Íbis", "Náutico", "Santa Cruz", "Sport"]  # sem acento na ordem
    assert all(row["tied"] and row["played"] == 0 for row in rows)
    assert not Standing.objects.exists()  # leitura não grava

    services.recompute_group(league["group"])
    newcomer = make_team("Afogados")
    GroupTeam.objects.create(group=league["group"], team=newcomer)
    names = [row["team"]["name"] for row in services.stage_standings(league["stage"])["groups"][0]["rows"]]
    assert "Afogados" in names and len(names) == 5


def test_groups_stage_with_two_groups_and_lot_order():
    comp = make_league(n_teams=2)
    stage = make_stage(comp["season"], "groups", name="Grupos", position=2, criteria=["points", "drawing_of_lots"], zones=[ZONES[0]])
    group_a, group_b = stage.groups.create(name="Grupo A"), stage.groups.create(name="Grupo B")
    teams = [make_team(f"Time {letter}") for letter in "ABCD"]
    GroupTeam.objects.create(group=group_a, team=teams[0], lot_order=2)
    GroupTeam.objects.create(group=group_a, team=teams[1], lot_order=1)
    GroupTeam.objects.create(group=group_b, team=teams[2])
    GroupTeam.objects.create(group=group_b, team=teams[3])
    services.recompute_stage(stage)
    table = services.stage_standings(stage)
    assert [group["name"] for group in table["groups"]] == ["Grupo A", "Grupo B"]
    a_rows, b_rows = table["groups"][0]["rows"], table["groups"][1]["rows"]
    assert [row["team"]["name"] for row in a_rows] == ["Time B", "Time A"] and not a_rows[0]["tied"]
    assert all(row["tied"] for row in b_rows)  # sorteio ainda não feito
    assert a_rows[0]["zone"]["name"] == b_rows[0]["zone"]["name"] == "Classificados"
    assert table["criteria"] == [{"key": "points", "label": "Pontos"}, {"key": "drawing_of_lots", "label": "Sorteio"}]


def test_stages_standings_bounded_queries(league, django_assert_max_num_queries):
    other = make_league(n_teams=6)
    services.recompute_group(league["group"])
    services.recompute_group(other["group"])
    with django_assert_max_num_queries(6):
        tables = services.stages_standings([league["stage"], other["stage"]])
    assert set(tables) == {league["stage"].id, other["stage"].id}
    assert len(tables[other["stage"].id]["groups"][0]["rows"]) == 6


def test_invalid_criteria_fall_back_to_defaults(league, caplog):
    StageCriterion.objects.filter(stage=league["stage"]).delete()
    with caplog.at_level("WARNING", logger="fdr.standings"):
        rules = services.stage_rules(league["stage"])
    assert rules.criteria == services.DEFAULT_CRITERIA
    assert any("critérios inválidos" in record.getMessage() for record in caplog.records)
    StageCriterion.objects.create(stage=league["stage"], position=1, key="moral")
    services.recompute_group(league["group"])  # não quebra
    assert [item["key"] for item in services.stage_standings(league["stage"])["criteria"]] == list(services.DEFAULT_CRITERIA)


def test_on_stage_rules_changed_validates_recalculates_and_publishes(league):
    sport, nautico = league["teams"][:2]
    finish(make_match(league["stage"], sport, nautico), 1, 1)
    services.recompute_group(league["group"])
    stage = league["stage"]

    stage.points_draw = 2
    stage.save()
    mark = Outbox.objects.order_by("-id").values_list("id", flat=True).first() or 0
    services.on_stage_rules_changed(stage, recalc=False)  # só zona/cor: não recalcula, mas publica
    (msg,) = Outbox.objects.filter(id__gt=mark)
    assert msg.topic == "standings" and msg.payload["standings"]["points"]["draw"] == 2
    assert Standing.objects.get(group=league["group"], kind="official", team=sport).points == 1

    services.on_stage_rules_changed(stage, recalc=True)
    assert Standing.objects.get(group=league["group"], kind="official", team=sport).points == 2
    assert Outbox.objects.filter(id__gt=mark, topic="standings").count() == 2

    StandingZone.objects.create(stage=stage, name="Sobreposta", color="#000000", position_from=2, position_to=3)
    count = Outbox.objects.count()
    with pytest.raises(ConfigError) as info:
        services.on_stage_rules_changed(stage)
    assert info.value.code == "zones_overlap"
    assert Outbox.objects.count() == count
    # A sobreposição também é recusada pelo banco no commit (`zone_no_overlap`, DEFERRED):
    # a zona inválida sai antes do fim do teste.
    StandingZone.objects.filter(name="Sobreposta").delete()

    ko = make_knockout(league["season"])
    services.on_stage_rules_changed(ko["stage"])  # mata-mata não tem tabela: nada a publicar
    assert Outbox.objects.count() == count


def test_standings_message_shape(league):
    with locked_atomic():
        message = services.standings_message(league["stage"])
    assert message["stage_id"] == league["stage"].id and message["standings"]["kind"] == "live"
