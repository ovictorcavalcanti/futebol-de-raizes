"""Mata-mata pela API (fase 5 do plano): dois confrontos de ponta a ponta — um de
ida e volta com prorrogação e outro de jogo único direto nos pênaltis. O
vencedor sai certo (`winner_team_id`, `decided_by`) e o fim de jogo com o
confronto empatado é rejeitado (422 `tie_level_requires_*`). E a documentação
interativa da API responde em `/api/docs` (só para a equipe)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from core import timeutils
from matches.models import Tie
from tests.factories import make_knockout, make_league, make_tie_matches
from tests.test_api_ops import ApiOp, assert_error

pytestmark = pytest.mark.django_db


@pytest.fixture
def league(db):
    return make_league(n_teams=4, slug="pe", team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"])


def tie_of(client, match_id: int) -> dict:
    response = client.get(f"/api/matches/{match_id}")
    assert response.status_code == 200
    return response.json()["match"]["tie"]


def test_two_leg_tie_with_extra_time_end_to_end(league, operator_client, client):
    ko = make_knockout(league["season"], legs=2, extra_time=True, team_a=league["teams"][0], team_b=league["teams"][1], name="Semifinal")
    tie, team_a, team_b = ko["tie"], ko["team_a"], ko["team_b"]
    leg1, leg2 = make_tie_matches(tie, kickoff=timeutils.now() - timedelta(days=7))

    # Ida: A (mandante) 2 × 1 B
    op1 = ApiOp(operator_client, leg1)
    op1.post("match_start")
    first_goal = op1.goal(team_a, 10, "Zé Roberto")
    op1.goal(team_b, 30, "Kayo Lima")
    op1.goal(team_a, 40, "Gustavo Coutinho")
    op1.post("half_time")
    op1.post("second_half_start")
    end1 = op1.post("match_end")
    assert end1["match"]["tie"]["aggregate"] == {"team_a": 2, "team_b": 1}
    assert end1["match"]["tie"]["complete"] is False and end1["match"]["tie"]["winner_team_id"] is None

    # Volta: B (mandante) 1 × 0 → agregado 2 × 2 ao fim do 2T
    op2 = ApiOp(operator_client, leg2)
    start2 = op2.post("match_start")
    assert start2["match"]["tie"]["leg"] == 2
    # com a volta começada, a ida não pode mais ser corrigida
    assert_error(op1.void(first_goal["event"]["id"], expect=422), "tie_leg_locked")
    op2.goal(team_b, 20, "Kayo Lima")
    op2.post("half_time")
    second_half = op2.post("second_half_start")
    assert "match_end" not in second_half["available"]["events"]
    assert "extra_time_start" in second_half["available"]["events"]
    assert "penalties_start" not in second_half["available"]["events"]

    rejected = op2.post("match_end", expect=422)
    assert_error(rejected, "tie_level_requires_extra_time")
    assert_error(op2.post("penalties_start", expect=422), "penalties_not_allowed")
    extra = op2.post("extra_time_start")
    assert extra["match"]["period"] == "extra_time" and extra["event"]["minute"] == 90
    op2.goal(team_b, 104, "Fabinho")
    assert_error(op2.post("match_end", expect=422), "invalid_transition")  # ainda no 1º tempo da prorrogação
    interval = op2.post("extra_half_time")
    assert interval["match"]["period"] == "extra_half_time" and interval["match"]["period_label"] == "Intervalo da prorrogação"
    assert interval["match"]["clock"] is None and interval["available"]["events"][0] == "extra_second_half_start"
    second = op2.post("extra_second_half_start")
    assert second["match"]["period"] == "extra_second_half" and second["match"]["clock"]["offset"] == 105
    end2 = op2.post("match_end")
    assert end2["match"]["status"] == "finished" and end2["match"]["winner"] == "home"
    result = end2["match"]["tie"]
    assert result["winner_team_id"] == team_b.id and result["complete"] is True
    assert result["decided_by"] == "extra_time" and result["decided_by_label"] == "na prorrogação"
    assert result["aggregate"] == {"team_a": 2, "team_b": 3}

    stored = Tie.objects.get(pk=tie.pk)
    assert (stored.winner_team_id, stored.decided_by) == (team_b.id, "extra_time")
    # a ida também mostra o confronto decidido
    assert tie_of(client, leg1.id)["winner_team_id"] == team_b.id

    # página da competição: o confronto da rodada com os dois jogos
    page = client.get("/api/competitions/pe", {"stage": ko["stage"].id}).json()
    assert page["stage"]["format"] == "knockout" and page["stage"]["standings"] is None
    (tie_out,) = page["stage"]["ties"]
    assert tie_out["winner_team_id"] == team_b.id and tie_out["decided_by"] == "extra_time"
    assert [match["id"] for match in tie_out["matches"]] == [leg1.id, leg2.id] and "leg" not in tie_out


def test_single_match_tie_straight_to_penalties_end_to_end(league, operator_client, client):
    ko = make_knockout(league["season"], legs=1, extra_time=False, team_a=league["teams"][2], team_b=league["teams"][3], name="Final")
    tie, team_a, team_b = ko["tie"], ko["team_a"], ko["team_b"]
    (match,) = make_tie_matches(tie, kickoff=timeutils.now() - timedelta(hours=2))
    op = ApiOp(operator_client, match)
    op.post("match_start")
    op.post("half_time")
    second_half = op.post("second_half_start")
    assert "penalties_start" in second_half["available"]["events"]
    assert "match_end" not in second_half["available"]["events"]

    assert_error(op.post("match_end", expect=422), "tie_level_requires_penalties")
    assert_error(op.post("extra_time_start", expect=422), "extra_time_not_allowed")
    pens = op.post("penalties_start")
    assert pens["match"]["period"] == "penalties"
    assert (pens["match"]["home_penalties"], pens["match"]["away_penalties"]) == (0, 0)

    kicks = [(team_a, True), (team_b, True), (team_a, True), (team_b, False), (team_a, False), (team_b, True)]
    for team, scored in kicks:
        op.post("shootout_kick", team_id=team.id, payload={"player": f"Cobrador {team.short_name}", "scored": scored})
    # 2 × 2 nos pênaltis: o fim de jogo exige placar diferente
    assert_error(op.post("match_end", expect=422), "penalties_level")
    last = op.post("shootout_kick", team_id=team_a.id, payload={"player": "Batedor", "scored": True})
    assert last["event"]["kind"] == "game" and last["event"]["score_after"] is None
    end = op.post("match_end")
    final = end["match"]
    assert final["status"] == "finished" and (final["home_score"], final["away_score"]) == (0, 0)
    assert (final["home_penalties"], final["away_penalties"]) == (3, 2)
    assert final["winner"] == "home" and final["goals"] == []
    assert final["tie"]["winner_team_id"] == team_a.id
    assert final["tie"]["decided_by"] == "penalties" and final["tie"]["decided_by_label"] == "nos pênaltis"

    stored = Tie.objects.get(pk=tie.pk)
    assert (stored.winner_team_id, stored.decided_by) == (team_a.id, "penalties")
    # cobrança da disputa não é gol: não entra nos últimos gols
    day = timeutils.local_today().isoformat()
    assert client.get("/api/home", {"date": day}).json()["latest_goals"] == []


def test_api_docs_and_openapi_are_staff_only(client, django_user_model):
    # a documentação interna lista as rotas de operação: anônimo vai para o login do admin
    for url in ("/api/docs", "/api/openapi.json"):
        response = client.get(url)
        assert response.status_code == 302 and "/admin/login/" in response["Location"]
    # usuário comum (sem acesso ao admin) também não vê
    django_user_model.objects.create_user("torcedor", password="senha-forte-123")
    client.login(username="torcedor", password="senha-forte-123")
    assert client.get("/api/openapi.json").status_code == 302


def test_api_docs_and_openapi_list_every_route(client, django_user_model):
    django_user_model.objects.create_user("equipe", password="senha-forte-123", is_staff=True)
    client.login(username="equipe", password="senha-forte-123")
    docs = client.get("/api/docs")
    assert docs.status_code == 200 and b"swagger" in docs.content.lower()
    schema = client.get("/api/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert set(paths) >= {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/ops/matches/{match_id}/events",
        "/api/ops/matches/{match_id}/events/{event_id}/void",
        "/api/ops/matches/{match_id}/status",
        "/api/ops/catalog",
        "/api/home",
        "/api/competitions",
        "/api/competitions/{slug}",
        "/api/stages/{stage_id}/standings",
        "/api/matches",
        "/api/matches/{match_id}",
    }
    params = {param["name"] for param in paths["/api/matches"]["get"]["parameters"]}
    assert params == {"roundId", "date", "status", "stageId"}
