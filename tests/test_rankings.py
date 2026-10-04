"""Classificação geral do torneio e classificações personalizadas (standings.models.Ranking)."""

from __future__ import annotations

import pytest

from competitions.models import GroupTeam, Stage
from matches.models import Match
from standings.models import PointAdjustment, Ranking, RankingCriterion, RankingZone
from standings.services import ranking_standings
from tests.factories import make_knockout, make_league, make_match, make_stage, make_team, make_tie_matches
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


# --- Posição nos grupos (melhores terceiros) e zona condicional --------------------------


@pytest.fixture
def groups_stage():
    """Fase de grupos A (4), B (4) e C (5): todos contra todos, o de cima vence por 1 × 0;
    o 3º de cada grupo vence o 4º por 1 × 0 (A), 3 × 0 (B) e 2 × 0 (C)."""
    from competitions.models import Group

    league = make_league(n_teams=2)
    stage = make_stage(league["season"], Stage.Format.GROUPS, name="Grupos", position=2)
    teams, margin = {}, {"A": 1, "B": 3, "C": 2}
    for name, size in (("A", 4), ("B", 4), ("C", 5)):
        group = Group.objects.create(stage=stage, name=name)
        members = [make_team(f"{name}{n}") for n in range(1, size + 1)]
        for team in members:
            GroupTeam.objects.create(group=group, team=team)
        for i, home in enumerate(members):
            for away in members[i + 1:]:
                goals = margin[name] if (home.name, away.name) == (f"{name}3", f"{name}4") else 1
                finish(make_match(stage, home, away, group=group), goals, 0)
        teams.update({team.name: team for team in members})
    return {**league, "groups_stage": stage, "t": teams}


def thirds(groups_stage, **extra):
    ranking = Ranking.objects.create(season=groups_stage["season"], name="Melhores terceiros", scope="position", group_position=3, **extra)
    ranking.stages.set([groups_stage["groups_stage"]])
    for position, key in enumerate(("points", "goal_difference", "goals_for"), start=1):
        RankingCriterion.objects.create(ranking=ranking, position=position, key=key)
    return ranking


def test_position_ranking_compares_the_nth_of_each_group(groups_stage):
    table = ranking_standings(thirds(groups_stage), live=False)
    rows = table["groups"][0]["rows"]
    # sem desconsiderar nada: o C3 (grupo de 5) tem 2 vitórias e passa à frente
    assert [(row["team"]["name"], row["group"], row["points"]) for row in rows] == [("C3", "C", 6), ("B3", "B", 3), ("A3", "A", 3)]
    assert table["group_position"] == 3 and table["scope"] == "position"


def test_position_ranking_can_skip_matches_against_extra_teams(groups_stage):
    rows = ranking_standings(thirds(groups_stage, skip_extra_teams=True), live=False)["groups"][0]["rows"]
    # sem o jogo contra o C5: todos com 3 pontos, desempata o saldo (B3 +1, C3 0, A3 -1)
    assert [(row["team"]["name"], row["points"], row["goal_difference"], row["played"]) for row in rows] == [
        ("B3", 3, 1, 3), ("C3", 3, 0, 3), ("A3", 3, -1, 3),
    ]


def test_conditional_zone_paints_only_the_qualified_thirds(groups_stage):
    from competitions.models import StandingZone
    from standings.services import stage_standings

    ranking = thirds(groups_stage, skip_extra_teams=True)
    stage = groups_stage["groups_stage"]
    StandingZone.objects.create(stage=stage, name="Classificados", color="#0A7A3D", position_from=1, position_to=2)
    StandingZone.objects.create(stage=stage, name="Melhores terceiros", color="#7ACB8F", position_from=3, position_to=3,
                                ranking=ranking, ranking_position_from=1, ranking_position_to=2)
    table = stage_standings(stage, live=False)
    zone_of = {row["team"]["name"]: (row["zone"] or {}).get("name") for group in table["groups"] for row in group["rows"]}
    assert zone_of["B3"] == zone_of["C3"] == "Melhores terceiros"  # os 2 melhores terceiros
    assert zone_of["A3"] is None  # o pior terceiro fica sem a cor
    assert zone_of["A1"] == zone_of["C2"] == "Classificados" and zone_of["C4"] is None
    conditional = next(item for item in table["legend"] if item["name"] == "Melhores terceiros")
    assert conditional["condition"] == "só quem estiver do 1º ao 2º em “Melhores terceiros”"
    assert "condition" not in next(item for item in table["legend"] if item["name"] == "Classificados")


def test_admin_validates_position_ranking(admin_client_fdr, groups_stage):
    add = url(Ranking, "add") + f"?season={groups_stage['season'].pk}"
    data = post_data(admin_client_fdr.get(add))
    data.update(season=str(groups_stage["season"].pk), name="Terceiros", scope="position", position="1",
                points_win="3", points_draw="1", points_loss="0", stages=[str(groups_stage["stage"].pk)])
    data.pop("teams", None)
    content = admin_client_fdr.post(add, data).content.decode()
    assert "marque só ela" in content and "Informe a posição no grupo" in content
    data.update(stages=[str(groups_stage["groups_stage"].pk)], group_position="3", skip_extra_teams="on")
    data.pop("show_on_stages", None)
    assert admin_client_fdr.post(add, data).status_code == 302
    ranking = Ranking.objects.get(name="Terceiros")
    assert (ranking.group_position, ranking.skip_extra_teams) == (3, True)


def test_admin_stage_page_with_conditional_zone(admin_client_fdr, groups_stage):
    from competitions.models import StandingZone

    ranking = thirds(groups_stage)
    stage = groups_stage["groups_stage"]
    zone = StandingZone.objects.create(stage=stage, name="Melhores terceiros", color="#7ACB8F", position_from=3, position_to=3,
                                       ranking=ranking, ranking_position_from=1, ranking_position_to=2)
    response = admin_client_fdr.get(change_url(stage))
    assert response.status_code == 200 and "só se estiver na classificação" in response.content.decode().lower()
    data = post_data(response)
    assert admin_client_fdr.post(change_url(stage), data).status_code == 302  # salva sem mudar nada
    prefix = next(key[: -len("-ranking")] for key, value in data.items() if key.endswith("-ranking") and value == str(ranking.pk))
    data[f"{prefix}-ranking_position_to"] = ""
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200 and "Informe a faixa da condição" in response.content.decode()
    zone.refresh_from_db()
    assert zone.ranking_position_to == 2
