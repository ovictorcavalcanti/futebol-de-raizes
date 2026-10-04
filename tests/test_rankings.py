"""Classificação geral do torneio e classificações personalizadas (standings.models.Ranking)."""

from __future__ import annotations

import pytest

from competitions.models import GroupTeam, Stage
from matches.models import Match
from standings.models import PointAdjustment, Ranking, RankingCriterion, RankingZone
from standings.services import ranking_standings
from tests.factories import make_knockout, make_league, make_match, make_stage, make_tie_matches
from tests.test_admin import change_url, post_data, url

pytestmark = pytest.mark.django_db


def finish(match, home, away):
    Match.objects.filter(pk=match.pk).update(status="finished", home_score=home, away_score=away)


@pytest.fixture
def season():
    """1ª fase (pontos corridos, 4 times), 2ª fase (pontos corridos, 2 times) e final (mata-mata)."""
    league = make_league(n_teams=4, team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"])
    spt, nau, scz, ret = league["teams"]
    finish(make_match(league["stage"], spt, nau), 2, 0)
    finish(make_match(league["stage"], scz, ret), 1, 1)
    second = make_stage(league["season"], Stage.Format.LEAGUE, name="2ª fase", position=2)
    for team in (spt, scz):
        GroupTeam.objects.create(group=second.groups.get(), team=team)
    finish(make_match(second, scz, spt), 3, 0)
    ko = make_knockout(league["season"], legs=1, extra_time=False, team_a=spt, team_b=nau, name="Final", position=3)
    (final,) = make_tie_matches(ko["tie"])
    finish(final, 1, 0)  # mandante do jogo único vence
    return {**league, "second": second, "final_stage": ko["stage"], "final": Match.objects.get(pk=final.pk)}


def rows_by_name(table):
    (group,) = table["groups"]
    return {row["team"]["name"]: row for row in group["rows"]}


def test_overall_sums_all_matches_of_the_chosen_stages_knockout_included(season):
    ranking = Ranking.objects.create(season=season["season"], name="Geral", scope="overall")
    ranking.stages.set([season["stage"], season["second"], season["final_stage"]])
    table = ranking_standings(ranking, live=False)
    rows = rows_by_name(table)
    assert set(rows) == {"Sport", "Náutico", "Santa Cruz", "Retrô"}
    final_home = season["final"].home_team.name
    assert rows["Sport"]["played"] == 3 and rows["Santa Cruz"]["played"] == 2 and rows["Retrô"]["played"] == 1
    assert rows[final_home]["won"] >= 1
    assert rows["Santa Cruz"]["points"] == 4  # empate (1) + vitória na 2ª fase (3)
    assert table["stage_ids"] == [season["stage"].id, season["second"].id, season["final_stage"].id]
    assert table["scope"] == "overall" and table["groups"][0]["name"] == "Geral"


def test_skipped_stage_does_not_count_and_adjustments_of_included_stages_do(season):
    ranking = Ranking.objects.create(season=season["season"], name="Geral a partir da 2ª", scope="overall")
    ranking.stages.set([season["second"]])
    PointAdjustment.objects.create(stage=season["second"], team=season["teams"][2], points=-2, reason="W.O.")
    PointAdjustment.objects.create(stage=season["stage"], team=season["teams"][0], points=-5, reason="fora da soma")
    rows = rows_by_name(ranking_standings(ranking, live=False))
    assert set(rows) == {"Sport", "Santa Cruz"}  # só os times da 2ª fase
    assert rows["Santa Cruz"]["points"] == 1 and rows["Santa Cruz"]["points_adjustment"] == -2
    assert rows["Sport"]["points"] == 0 and rows["Sport"]["played"] == 1


def test_custom_ranking_only_shows_the_chosen_teams_with_its_own_rules(season):
    ranking = Ranking.objects.create(season=season["season"], name="Vaga na Série D", scope="custom", points_win=2)
    ranking.stages.set([season["stage"]])
    ranking.teams.set(season["teams"][1:])  # 3 de 4 (o Sport já tem divisão)
    RankingCriterion.objects.create(ranking=ranking, position=1, key="points")
    RankingZone.objects.create(ranking=ranking, name="Série D", color="#0A7A3D", position_from=1, position_to=1)
    table = ranking_standings(ranking, live=False)
    rows = rows_by_name(table)
    assert set(rows) == {"Náutico", "Santa Cruz", "Retrô"}
    assert rows["Náutico"]["played"] == 1 and rows["Náutico"]["lost"] == 1  # jogo contra o Sport conta para o Náutico
    assert table["points"] == {"win": 2, "draw": 1, "loss": 0}
    assert table["criteria"] == [{"key": "points", "label": "Pontos"}]
    assert table["legend"][0]["name"] == "Série D" and table["groups"][0]["rows"][0]["zone"]["name"] == "Série D"


def test_api_ranking_and_competition_buttons(client, season):
    geral = Ranking.objects.create(season=season["season"], name="Geral", scope="overall", show_on_competition=True)
    geral.stages.set([season["stage"], season["second"]])
    serie_d = Ranking.objects.create(season=season["season"], name="Série D", scope="custom", position=2)
    serie_d.stages.set([season["stage"]])
    serie_d.teams.set(season["teams"][1:])
    serie_d.show_on_stages.set([season["stage"]])
    data = client.get(f"/api/rankings/{geral.id}?live=1").json()
    assert data["ranking_id"] == geral.id and data["kind"] == "live" and len(data["groups"][0]["rows"]) == 4
    assert client.get("/api/rankings/999999").status_code == 404
    page = client.get(f"/api/competitions/{season['competition'].slug}?stage={season['stage'].id}").json()
    assert page["rankings"] == [{"id": geral.id, "name": "Geral", "scope": "overall"}]
    assert page["stage"]["rankings"] == [{"id": serie_d.id, "name": "Série D", "scope": "custom"}]
    other = client.get(f"/api/competitions/{season['competition'].slug}?stage={season['second'].id}").json()
    assert other["stage"]["rankings"] == []


def test_admin_creates_rankings_and_validates(admin_client_fdr, season):
    add = url(Ranking, "add") + f"?season={season['season'].pk}"
    data = post_data(admin_client_fdr.get(add))
    data.update(season=str(season["season"].pk), name="Série D", scope="custom", position="1",
                points_win="3", points_draw="1", points_loss="0", stages=[str(season["stage"].pk)])
    data.pop("teams", None)
    response = admin_client_fdr.post(add, data)
    assert response.status_code == 200 and "A personalizada precisa dos times que disputam" in response.content.decode()

    data["teams"] = [str(team.pk) for team in season["teams"][1:]]
    data["show_on_stages"] = [str(season["stage"].pk)]
    response = admin_client_fdr.post(add, data)
    assert response.status_code == 302, response.content.decode()[:2000]
    ranking = Ranking.objects.get(name="Série D")
    assert ranking.teams.count() == 3 and list(ranking.show_on_stages.all()) == [season["stage"]]

    data = post_data(admin_client_fdr.get(change_url(ranking)))
    data.update({"zones-TOTAL_FORMS": "2", "zones-0-name": "A", "zones-0-color": "#000000", "zones-0-position_from": "1",
                 "zones-0-position_to": "2", "zones-1-name": "B", "zones-1-color": "#FFFFFF", "zones-1-position_from": "2",
                 "zones-1-position_to": "3"})
    response = admin_client_fdr.post(change_url(ranking), data)
    assert response.status_code == 200 and not ranking.zones.exists()  # faixas sobrepostas: recusadas no formulário

    page = admin_client_fdr.get(change_url(season["season"])).content.decode()
    assert "Classificações gerais e personalizadas" in page and change_url(ranking) in page
