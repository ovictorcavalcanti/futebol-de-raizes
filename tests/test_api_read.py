"""Rotas de leitura (sem login).

Fase 2 do plano: `/api/home` devolve os jogos do dia por competição, com a
virada do dia no horário de Brasília e o jogo da véspera que passa da
meia-noite; menu, página da competição, lista e detalhe de partidas com hora do
servidor e fuso. Fase 4: trocar a ordem dos critérios ou a cor de uma zona muda
`/api/stages/{id}/standings`; um gol anulado desfaz a mudança na tabela ao vivo.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.test import Client

from competitions.models import StageCriterion, StandingZone
from core import timeutils
from standings import services as standings_services
from tests.factories import make_knockout, make_league, make_match, make_tie_matches
from tests.test_api_ops import ApiOp, assert_error, use_clock

BRT = ZoneInfo("America/Sao_Paulo")
pytestmark = pytest.mark.django_db


def brt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=BRT)


def home_ids(payload: dict) -> list[int]:
    return [match["id"] for comp in payload["competitions"] for stage in comp["stages"] for match in stage["matches"]]


def get_home(client: Client, date: str | None = None) -> dict:
    response = client.get("/api/home", {"date": date} if date else {})
    assert response.status_code == 200, response.content.decode()
    assert response["Cache-Control"] == "no-store"
    return response.json()


def standings(client: Client, stage_id: int, live: bool = False) -> dict:
    response = client.get(f"/api/stages/{stage_id}/standings", {"live": 1} if live else {})
    assert response.status_code == 200, response.content.decode()
    assert response["Cache-Control"] == "no-store"
    return response.json()


def rows_by_team(table: dict) -> dict[int, dict]:
    return {row["team"]["id"]: row for row in table["groups"][0]["rows"]}


# --- Fase 2: jogos do dia ------------------------------------------------------------------


def test_home_day_boundary_in_brasilia_with_match_passing_midnight(operator_client, monkeypatch):
    lg = make_league(n_teams=6)
    t = lg["teams"]
    clock = use_clock(monkeypatch, brt(2, 17, 59))
    # 02/10 22:30 BRT = 03/10 01:30 UTC: começa na véspera e passa da meia-noite
    overnight = make_match(lg["stage"], t[0], t[1], kickoff_at=brt(2, 22, 30))
    # 02/10 22:00 BRT, nunca começou: é só da véspera (mesmo sendo 03/10 em UTC)
    late_scheduled = make_match(lg["stage"], t[4], t[5], kickoff_at=brt(2, 22, 0))
    early = make_match(lg["stage"], t[2], t[3], kickoff_at=brt(2, 18, 0))  # termina antes da meia-noite
    after_midnight = make_match(lg["stage"], t[2], t[4], kickoff_at=brt(3, 0, 30))
    night = make_match(lg["stage"], t[3], t[5], kickoff_at=brt(3, 23, 30))  # 04/10 02:30 UTC

    op_early = ApiOp(operator_client, early, clock)
    op_early.play_to_second_half()
    op_early.post("match_end", after=50)  # ~19:52 BRT
    clock.set(brt(2, 22, 29))
    op = ApiOp(operator_client, overnight, clock)
    op.play_to_second_half()  # 2T começa ~23:32 BRT
    clock.set(brt(3, 0, 10))
    late_goal = op.goal(t[0], 84, "Hernanes")  # gol depois da meia-noite

    clock.set(brt(3, 0, 20))
    today = get_home(operator_client)
    assert today["date"] == "2026-10-03" and today["timezone"] == "America/Sao_Paulo"
    assert today["server_time"] == "2026-10-03T03:20:00Z"
    assert home_ids(today) == [overnight.id, after_midnight.id, night.id]
    assert [goal["event_id"] for goal in today["latest_goals"]] == [late_goal["event"]["id"]]
    assert today["cursor"] > 0
    yesterday = get_home(operator_client, "2026-10-02")
    assert home_ids(yesterday) == [early.id, late_scheduled.id, overnight.id]
    assert get_home(operator_client, "2026-10-04")["competitions"] == []

    # termina às 00:40 BRT: fica na home de hoje até 02:40
    clock.set(brt(3, 0, 40))
    end = op.post("match_end", after=0)
    assert end["match"]["finished_at"] == "2026-10-03T03:40:00Z"
    clock.set(brt(3, 2, 39))
    assert overnight.id in home_ids(get_home(operator_client))
    clock.set(brt(3, 2, 41))
    assert overnight.id not in home_ids(get_home(operator_client))
    assert overnight.id in home_ids(get_home(operator_client, "2026-10-02"))


def test_home_groups_by_competition_in_position_with_standings(operator_client, monkeypatch):
    use_clock(monkeypatch, brt(3, 12, 0))
    second = make_league(name="Série A2", slug="a2", position=2, n_teams=2)
    first = make_league(name="Pernambucano", slug="pe", position=1, n_teams=2)
    make_league(name="Sem jogo", slug="vazia", position=0, n_teams=2)
    make_match(second["stage"], *second["teams"], kickoff_at=brt(3, 16, 0))
    make_match(first["stage"], *first["teams"], kickoff_at=brt(3, 19, 0))
    ko = make_knockout(first["season"], legs=1)
    (ko_match,) = make_tie_matches(ko["tie"], kickoff=brt(3, 21, 0))

    home = get_home(operator_client)
    assert [comp["slug"] for comp in home["competitions"]] == ["pe", "a2"]
    stages = home["competitions"][0]["stages"]
    assert [stage["format"] for stage in stages] == ["league", "knockout"]
    assert stages[0]["standings"]["kind"] == "live" and stages[0]["standings"]["stage_id"] == first["stage"].id
    assert stages[1]["standings"] is None
    assert stages[1]["matches"][0]["id"] == ko_match.id and stages[1]["matches"][0]["tie"]["legs"] == 1


def test_home_rejects_invalid_date(client):
    for value in ("2026-13-01", "03/10/2026", "ontem", "2026-1-5"):
        response = client.get("/api/home", {"date": value})
        assert response.status_code == 400, value
        body = response.json()
        assert_error(body, "invalid_input")
        assert body["details"]["field"] == "date"


def test_competitions_menu_and_competition_page(client):
    second = make_league(name="Série A2", slug="a2", position=2, n_teams=2)
    first = make_league(name="Pernambucano", slug="pe", position=1, n_teams=4)
    r1, r2 = first["rounds"][0], first["rounds"][1]
    m1 = make_match(first["stage"], first["teams"][0], first["teams"][1], round=r1)
    m2 = make_match(first["stage"], first["teams"][2], first["teams"][3], round=r2)

    menu = client.get("/api/competitions")
    assert menu.status_code == 200 and menu["Cache-Control"] == "public, max-age=60"
    assert [comp["slug"] for comp in menu.json()["competitions"]] == ["pe", "a2"]
    assert set(menu.json()["competitions"][0]) == {"id", "name", "slug", "short_name", "position"}

    page = client.get("/api/competitions/pe")
    assert page.status_code == 200 and page["Cache-Control"] == "no-store"
    data = page.json()
    assert set(data) >= {"server_time", "timezone", "cursor", "competition", "season", "stages", "current_stage_id", "current_round_id", "stage"}
    assert data["current_stage_id"] == first["stage"].id and data["current_round_id"] == r1.id
    assert [match["id"] for match in data["stage"]["matches"]] == [m1.id]
    assert data["stage"]["standings"]["kind"] == "live" and data["stage"]["ties"] == []

    chosen = client.get("/api/competitions/pe", {"stage": first["stage"].id, "round": r2.id}).json()
    assert chosen["current_round_id"] == r2.id and [match["id"] for match in chosen["stage"]["matches"]] == [m2.id]

    assert_error(client.get("/api/competitions/nao-existe").json(), "not_found")
    assert client.get("/api/competitions/nao-existe").status_code == 404
    foreign_round = client.get("/api/competitions/pe", {"round": second["rounds"][0].id})
    assert foreign_round.status_code == 404 and foreign_round.json()["code"] == "not_found"
    bad = client.get("/api/competitions/pe", {"stage": "primeira"})
    assert bad.status_code == 400 and bad.json()["details"]["field"] == "stage"


def test_matches_list_filters_with_camel_case_names(client, operator_client):
    lg = make_league(n_teams=4)
    t, r1, r2 = lg["teams"], lg["rounds"][0], lg["rounds"][1]
    a = make_match(lg["stage"], t[0], t[1], round=r1, kickoff_at=brt(3, 16, 0))
    b = make_match(lg["stage"], t[2], t[3], round=r1, kickoff_at=brt(3, 21, 30))  # 04/10 em UTC
    c = make_match(lg["stage"], t[0], t[2], round=r2, kickoff_at=brt(10, 16, 0))
    ApiOp(operator_client, a).post("match_start")

    def ids(**params) -> list[int]:
        response = client.get("/api/matches", params)
        assert response.status_code == 200, response.content.decode()
        assert response["Cache-Control"] == "no-store"
        body = response.json()
        assert set(body) == {"server_time", "timezone", "matches"}
        return [match["id"] for match in body["matches"]]

    assert ids() == [a.id, b.id, c.id]
    assert ids(roundId=r1.id) == [a.id, b.id]
    assert ids(stageId=lg["stage"].id, roundId=r2.id) == [c.id]
    assert ids(date="2026-10-03") == [a.id, b.id]  # dia de Brasília
    assert ids(status="live") == [a.id]
    assert ids(status="live,scheduled", roundId=r1.id) == [a.id, b.id]
    for params, field in [({"status": "jogando"}, "status"), ({"date": "3/10"}, "date"), ({"roundId": "x"}, "roundId")]:
        response = client.get("/api/matches", params)
        assert response.status_code == 400, params
        assert response.json()["code"] == "invalid_input" and response.json()["details"]["field"] == field


def test_match_detail_shape_and_404(client):
    lg = make_league(n_teams=2)
    match = make_match(lg["stage"], *lg["teams"])
    response = client.get(f"/api/matches/{match.id}")
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    data = response.json()
    assert set(data) == {"server_time", "timezone", "cursor", "match", "available"}
    assert data["available"]["events"] == ["match_start"]
    assert set(data["available"]["status"]) == {"postpone", "reschedule", "cancel"}
    assert {"events", "lineups", "officials", "broadcasts", "stats", "attendance", "revenue_cents"} <= set(data["match"])
    missing = client.get("/api/matches/999999")
    assert missing.status_code == 404
    assert_error(missing.json(), "not_found")


# --- Fase 4: classificação configurável e ao vivo -------------------------------------------------


def play(client: Client, match, goals: list[tuple[object, int]]) -> None:
    """Jogo inteiro pela API com os gols dados (time, minuto) — minutos em ordem."""
    op = ApiOp(client, match)
    op.post("match_start")
    first_half = [(team, minute) for team, minute in goals if minute <= 45]
    for team, minute in first_half:
        op.goal(team, minute)
    op.post("half_time")
    op.post("second_half_start")
    for team, minute in goals:
        if minute > 45:
            op.goal(team, minute)
    op.post("match_end")


def test_criterion_order_and_zone_color_change_the_standings_response(client, operator_client):
    lg = make_league(
        n_teams=4,
        team_names=["Arapiraca", "Belo Jardim", "Carpina", "Decisão"],
        zones=[{"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 1}],
    )
    a, b, c, d = lg["teams"]
    stage, r1 = lg["stage"], lg["rounds"][0]
    # A vence C por 4 × 3 (saldo +1, 4 gols); B vence D por 2 × 0 (saldo +2, 2 gols)
    play(operator_client, make_match(stage, a, c, round=r1), [(a, 5), (c, 10), (a, 15), (c, 20), (a, 50), (c, 60), (a, 70)])
    play(operator_client, make_match(stage, b, d, round=r1), [(b, 30), (b, 80)])

    before = standings(client, stage.id)
    assert before["kind"] == "official" and set(before) >= {"server_time", "timezone", "criteria", "legend", "groups"}
    assert [item["key"] for item in before["criteria"]] == ["points", "wins", "goal_difference", "goals_for", "head_to_head"]
    rows = before["groups"][0]["rows"]
    assert [row["team"]["id"] for row in rows[:2]] == [b.id, a.id]  # saldo decide
    assert rows[0]["zone"] == {"name": "Classificados", "color": "#1B7F3B"} and rows[1]["zone"] is None

    # Admin troca a ordem: gols pró antes do saldo (posições únicas: troca em dois passos).
    goal_difference = StageCriterion.objects.get(stage=stage, key="goal_difference")
    goals_for = StageCriterion.objects.get(stage=stage, key="goals_for")
    gd_position, gf_position = goal_difference.position, goals_for.position
    StageCriterion.objects.filter(pk=goal_difference.pk).update(position=99)
    StageCriterion.objects.filter(pk=goals_for.pk).update(position=gd_position)
    StageCriterion.objects.filter(pk=goal_difference.pk).update(position=gf_position)
    standings_services.on_stage_rules_changed(stage)  # o que o admin chama ao salvar a fase

    after = standings(client, stage.id)
    assert [item["key"] for item in after["criteria"]] == ["points", "wins", "goals_for", "goal_difference", "head_to_head"]
    assert [row["team"]["id"] for row in after["groups"][0]["rows"][:2]] == [a.id, b.id]  # gols pró decidem
    assert [row["team"]["id"] for row in standings(client, stage.id, live=True)["groups"][0]["rows"][:2]] == [a.id, b.id]

    # Cor da zona: muda a legenda e a linha, sem recalcular.
    StandingZone.objects.filter(stage=stage).update(color="#12306B")
    standings_services.on_stage_rules_changed(stage, recalc=False)
    colored = standings(client, stage.id)
    assert colored["legend"] == [{"name": "Classificados", "color": "#12306B", "from": 1, "to": 1}]
    assert colored["groups"][0]["rows"][0]["zone"] == {"name": "Classificados", "color": "#12306B"}


def test_annulled_goal_undoes_the_live_table_change(client, operator_client):
    lg = make_league(n_teams=4, team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"])
    sport, nautico = lg["teams"][0], lg["teams"][1]
    match = make_match(lg["stage"], sport, nautico, round=lg["rounds"][0], kickoff_at=timeutils.now() - timedelta(minutes=30))
    op = ApiOp(operator_client, match)
    op.post("match_start")

    live = rows_by_team(standings(client, lg["stage"].id, live=True))
    assert (live[sport.id]["points"], live[nautico.id]["points"]) == (1, 1)
    assert live[sport.id]["playing"] and live[nautico.id]["playing"]
    official = standings(client, lg["stage"].id)
    assert official["kind"] == "official" and rows_by_team(official)[sport.id]["played"] == 0

    goal = op.goal(nautico, 12, "Kayo Lima")
    table = standings(client, lg["stage"].id, live=True)
    assert table["kind"] == "live" and table["groups"][0]["rows"][0]["team"]["id"] == nautico.id
    live = rows_by_team(table)
    assert (live[sport.id]["points"], live[nautico.id]["points"]) == (0, 3)

    op.post("goal_annulled", minute=14, annuls_event_id=goal["event"]["id"], payload={"reason": "Mão na bola"})
    live = rows_by_team(standings(client, lg["stage"].id, live=True))
    assert (live[sport.id]["points"], live[nautico.id]["points"]) == (1, 1)
    assert live[nautico.id]["goals_for"] == 0

    assert standings(client, lg["stage"].id, live=False)["kind"] == "official"
    assert client.get(f"/api/stages/{lg['stage'].id}/standings", {"live": "0"}).json()["kind"] == "official"
    bad = client.get(f"/api/stages/{lg['stage'].id}/standings", {"live": "talvez"})
    assert bad.status_code == 400 and bad.json()["details"]["field"] == "live"


def test_standings_404_for_unknown_or_knockout_stage(client):
    lg = make_league(n_teams=2)
    ko = make_knockout(lg["season"], legs=1)
    for stage_id in (999999, ko["stage"].id):
        response = client.get(f"/api/stages/{stage_id}/standings")
        assert response.status_code == 404
        assert_error(response.json(), "not_found")


def test_unknown_api_route_is_json_404(client):
    for path in ("/api/nada", "/api/home/extra"):
        response = client.get(path)
        assert response.status_code == 404 and response["Content-Type"] == "application/json"
        assert_error(response.json(), "not_found")
    assert Client(enforce_csrf_checks=True).post("/api/nada").status_code == 404
