"""Home (`selectors.home_payload`): o dia segue o horário de Brasília, o jogo da
véspera que passa da meia-noite fica até 2 h depois do fim, só competições com
jogo no dia (em `position`), últimos gols válidos e consultas em número fixo."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from matches import selectors
from matches.models import Match
from realtime.models import Outbox
from tests.factories import make_knockout, make_league, make_match, make_stage, make_tie_matches
from tests.test_services import Op

BRT = ZoneInfo("America/Sao_Paulo")
pytestmark = pytest.mark.django_db

LATEST_GOAL_KEYS = {
    "event_id", "match_id", "team_id", "team_side", "player", "origin", "period", "minute", "stoppage",
    "minute_label", "score_after", "created_at", "match", "team",
}


def brt(day, hour, minute=0, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=BRT)


def home_ids(payload) -> list[int]:
    return [match["id"] for comp in payload["competitions"] for stage in comp["stages"] for match in stage["matches"]]


def play_first_half(match, user, start):
    op = Op(match, user, start=start - timedelta(minutes=1))
    op.post("match_start")
    return op


def test_day_boundary_in_brasilia_with_match_passing_midnight(operator_user):
    lg = make_league(n_teams=6)
    t = lg["teams"]
    # 02/10 22:30 BRT = 03/10 01:30 UTC: começa na véspera e passa da meia-noite
    overnight = make_match(lg["stage"], t[0], t[1], kickoff_at=brt(2, 22, 30))
    # 02/10 22:00 BRT, nunca começou: é só da véspera (mesmo sendo 03/10 em UTC)
    late_scheduled = make_match(lg["stage"], t[4], t[5], kickoff_at=brt(2, 22, 0))
    # 02/10 18:00 BRT, terminou antes da meia-noite
    early = make_match(lg["stage"], t[2], t[3], kickoff_at=brt(2, 18, 0))
    after_midnight = make_match(lg["stage"], t[2], t[4], kickoff_at=brt(3, 0, 30))
    night = make_match(lg["stage"], t[3], t[5], kickoff_at=brt(3, 23, 30))  # 04/10 02:30 UTC

    op_early = play_first_half(early, operator_user, brt(2, 18, 0))
    op_early.post("half_time", after=46)
    op_early.post("second_half_start", after=15)
    op_early.post("match_end", after=50)  # ~19:52 BRT
    op = play_first_half(overnight, operator_user, brt(2, 22, 30))
    op.post("half_time", after=46)
    op.post("second_half_start", after=15)  # ~23:32 BRT

    at_0020 = brt(3, 0, 20)
    today = selectors.home_payload(now=at_0020)
    assert today["date"] == "2026-10-03"
    assert home_ids(today) == [overnight.id, after_midnight.id, night.id]
    yesterday = selectors.home_payload(day=date(2026, 10, 2), now=at_0020)
    assert home_ids(yesterday) == [overnight.id, early.id, late_scheduled.id]  # ao vivo > encerrado > agendado
    assert home_ids(selectors.home_payload(day="2026-10-04", now=at_0020)) == []

    # termina às 00:40 BRT: fica na home de hoje até 02:40
    op.clock = brt(3, 0, 39)
    end = op.post("match_end")
    assert end.match.finished_at == brt(3, 0, 40)
    assert overnight.id in home_ids(selectors.home_payload(now=brt(3, 2, 39)))
    assert overnight.id not in home_ids(selectors.home_payload(now=brt(3, 2, 41)))
    # outra data que não hoje: só o jogo encerrado com menos de 2 h conta
    assert overnight.id in home_ids(selectors.home_payload(day=date(2026, 10, 3), now=brt(3, 1, 0)))


def test_overnight_match_still_live_stays_only_while_today(operator_user):
    lg = make_league(n_teams=2)
    match = make_match(lg["stage"], lg["teams"][0], lg["teams"][1], kickoff_at=brt(2, 23, 0))
    play_first_half(match, operator_user, brt(2, 23, 0)).status("suspend", after=50)
    assert match.id in home_ids(selectors.home_payload(now=brt(3, 1, 0)))
    # vendo o dia 03 a partir do dia 05, o jogo suspenso da véspera não entra
    assert match.id not in home_ids(selectors.home_payload(day=date(2026, 10, 3), now=brt(5, 12, 0)))


def test_only_competitions_with_games_in_position_order_with_stages():
    third = make_league(name="Série A2", slug="a2", position=3, n_teams=2)
    first = make_league(name="Pernambucano", slug="pe", position=1, n_teams=4)
    idle = make_league(name="Sub-20", slug="sub20", position=2, n_teams=2)
    ko = make_knockout(first["season"], legs=1, extra_time=True, team_a=first["teams"][0], team_b=first["teams"][1], name="Mata-mata", position=5)
    groups_stage = make_stage(first["season"], "groups", name="Fase de grupos", position=3)
    group = groups_stage.groups.create(name="Grupo A")
    for team in first["teams"][2:]:
        group.group_teams.create(team=team)

    make_match(idle["stage"], idle["teams"][0], idle["teams"][1], kickoff_at=brt(4, 16))  # outro dia
    a2 = make_match(third["stage"], third["teams"][0], third["teams"][1], kickoff_at=brt(3, 15))
    league_late = make_match(first["stage"], first["teams"][2], first["teams"][3], kickoff_at=brt(3, 21))
    league_early = make_match(first["stage"], first["teams"][0], first["teams"][3], kickoff_at=brt(3, 16))
    (ko_match,) = make_tie_matches(ko["tie"], kickoff=brt(3, 18))
    grp = make_match(groups_stage, first["teams"][2], first["teams"][3], kickoff_at=brt(3, 19), group=group)

    payload = selectors.home_payload(now=brt(3, 12))
    assert set(payload) == {"date", "server_time", "timezone", "cursor", "competitions", "latest_goals"}
    assert payload["timezone"] == "America/Sao_Paulo" and payload["server_time"].endswith("Z")
    assert [comp["slug"] for comp in payload["competitions"]] == ["pe", "a2"]
    pe = payload["competitions"][0]
    assert {k: pe[k] for k in ("id", "name", "slug", "short_name", "position")} == {
        "id": first["competition"].id, "name": "Pernambucano", "slug": "pe", "short_name": "", "position": 1,
    }
    assert [stage["name"] for stage in pe["stages"]] == ["1ª fase", "Fase de grupos", "Mata-mata"]
    league_block, groups_block, ko_block = pe["stages"]
    assert set(league_block) == {"id", "name", "format", "matches", "standings"}
    assert [m["id"] for m in league_block["matches"]] == [league_early.id, league_late.id]
    assert league_block["standings"]["stage_id"] == first["stage"].id and league_block["standings"]["kind"] == "live"
    assert [g["name"] for g in groups_block["standings"]["groups"]] == ["Grupo A"]
    assert [m["id"] for m in groups_block["matches"]] == [grp.id]
    assert ko_block["format"] == "knockout" and ko_block["standings"] is None
    assert [m["id"] for m in ko_block["matches"]] == [ko_match.id] and ko_block["matches"][0]["tie"]["leg"] == 1
    assert [m["id"] for m in payload["competitions"][1]["stages"][0]["matches"]] == [a2.id]
    assert "events" not in league_block["matches"][0]  # resumo
    assert payload["latest_goals"] == []


def test_empty_day_and_cursor():
    payload = selectors.home_payload(day="2026-12-25", now=brt(3, 12))
    assert payload["competitions"] == [] and payload["latest_goals"] == [] and payload["date"] == "2026-12-25"
    assert payload["cursor"] == 0
    Outbox.objects.create(topic="match", payload={})
    assert selectors.home_payload(now=brt(3, 12))["cursor"] == Outbox.objects.latest("id").id


def test_latest_goals_only_valid_goals_newest_first(operator_user):
    lg = make_league(n_teams=4)
    t = lg["teams"]
    start = brt(3, 15)
    m1 = make_match(lg["stage"], t[0], t[1], kickoff_at=start)
    m2 = make_match(lg["stage"], t[2], t[3], kickoff_at=start + timedelta(minutes=30))
    yesterday = make_match(lg["stage"], t[0], t[2], kickoff_at=brt(2, 15))
    ko = make_knockout(lg["season"], legs=1, extra_time=False, team_a=t[1], team_b=t[3])
    (shootout,) = make_tie_matches(ko["tie"], kickoff=start + timedelta(minutes=10))

    op_old = play_first_half(yesterday, operator_user, brt(2, 15))
    op_old.goal(t[0], 10, "Ontem")

    op1 = play_first_half(m1, operator_user, start)
    goals = [op1.goal(t[0], minute, f"Gol {minute}").event.id for minute in range(1, 9)]
    annulled = op1.goal(t[1], 9, "Anulado")
    op1.post("goal_annulled", minute=10, annuls_event_id=annulled.event.id, payload={"reason": "Falta"})
    voided = op1.goal(t[1], 11, "Cancelado")
    op1.void(voided.event.id)

    op_ko = play_first_half(shootout, operator_user, start + timedelta(minutes=60))
    op_ko.post("half_time")
    op_ko.post("second_half_start")
    op_ko.post("penalties_start")
    op_ko.post("shootout_kick", team_id=t[1].id, payload={"player": "Cobrador", "scored": True})

    op2 = play_first_half(m2, operator_user, start + timedelta(minutes=90))
    late = [op2.goal(t[3], 5, "Tardio").event.id, op2.goal(t[2], 6, "Mais tardio").event.id]

    now = brt(3, 23)
    payload = selectors.home_payload(now=now)
    latest = payload["latest_goals"]
    assert len(latest) == 10
    assert [goal["event_id"] for goal in latest] == list(reversed(goals + late))[:10]
    assert all(set(goal) == LATEST_GOAL_KEYS for goal in latest)
    newest = latest[0]
    assert newest["player"] == "Mais tardio" and newest["team"]["id"] == t[2].id
    assert newest["match"] == {
        "id": m2.id,
        "competition": {"name": lg["competition"].name, "slug": lg["competition"].slug},
        "home": selectors.serialize_team(t[2]),
        "away": selectors.serialize_team(t[3]),
    }
    assert newest["score_after"] == {"home": 1, "away": 1}
    assert latest == selectors.latest_goals(date(2026, 10, 3), now)
    assert [goal["event_id"] for goal in selectors.latest_goals(date(2026, 10, 3), now, limit=3)] == [late[1], late[0], goals[-1]]
    excluded = {annulled.event.id, voided.event.id}
    assert not excluded & {goal["event_id"] for goal in selectors.latest_goals(date(2026, 10, 3), now, limit=50)}
    assert [goal["player"] for goal in selectors.latest_goals(date(2026, 10, 2), brt(2, 20))] == ["Ontem"]


def _populate(n_matches: int, user):
    lg = make_league(n_teams=6, position=1)
    other = make_league(n_teams=4, position=2)
    ko = make_knockout(other["season"], legs=2, extra_time=True, team_a=other["teams"][0], team_b=other["teams"][1])
    make_tie_matches(ko["tie"], kickoff=brt(3, 10))
    teams = lg["teams"]
    for index in range(n_matches):
        match = make_match(lg["stage"], teams[index % 6], teams[(index + 1) % 6], kickoff_at=brt(3, 12) + timedelta(minutes=index))
        op = play_first_half(match, user, brt(3, 12) + timedelta(minutes=index))
        op.goal(teams[index % 6], 3)
        op.post("yellow_card", team_id=teams[(index + 1) % 6].id, minute=4, payload={"player": "X"})


def test_home_payload_query_count_is_bounded(operator_user, django_assert_max_num_queries):
    _populate(3, operator_user)
    with django_assert_max_num_queries(11):
        small = selectors.home_payload(now=brt(3, 13))
    assert len(home_ids(small)) == 4  # 3 da liga + a ida do confronto
    _populate(6, operator_user)
    with django_assert_max_num_queries(11):
        big = selectors.home_payload(now=brt(3, 13))
    assert len(home_ids(big)) == 11 and len(big["latest_goals"]) == 9  # 4 + 6 da liga + a ida do 2º confronto


def test_serialize_matches_one_events_query(operator_user, django_assert_max_num_queries):
    _populate(5, operator_user)
    query = Match.objects.order_by("kickoff_at", "id")
    with django_assert_max_num_queries(3):  # partidas + eventos + jogos dos confrontos
        out = selectors.serialize_matches(query)
    assert len(out) == 7 and sum(len(item["goals"]) for item in out) == 5
    assert all(item["cards"]["away"]["yellow"] == 1 for item in out if item["goals"])


def test_latest_goals_follow_the_minute_within_a_match(operator_user):
    """Entre jogos, a hora do lançamento; no mesmo jogo, o minuto: o gol dos 5' lançado
    por último fica atrás dos gols de 30' e 10' do mesmo jogo."""
    lg = make_league(n_teams=4)
    t = lg["teams"]
    start = brt(3, 15)
    m1 = make_match(lg["stage"], t[0], t[1], kickoff_at=start)
    m2 = make_match(lg["stage"], t[2], t[3], kickoff_at=start + timedelta(minutes=10))
    op1 = play_first_half(m1, operator_user, start)
    g10 = op1.goal(t[0], 10, "Dez").event.id  # lançado às 15:01
    g30 = op1.goal(t[0], 30, "Trinta").event.id  # 15:02
    op2 = play_first_half(m2, operator_user, start + timedelta(minutes=10))
    g20 = op2.goal(t[2], 20, "Vinte").event.id  # 15:11
    g5 = op1.post("goal", team_id=t[1].id, minute=5, payload={"player": "Cinco"}, confirm=True, after=30).event.id  # 15:32

    latest = selectors.latest_goals(date(2026, 10, 3), brt(3, 23))
    assert [goal["event_id"] for goal in latest] == [g30, g20, g10, g5]
    assert [goal["score_after"] for goal in latest if goal["match"]["id"] == m1.id] == [
        {"home": 2, "away": 1}, {"home": 1, "away": 1}, {"home": 0, "away": 1},
    ]
