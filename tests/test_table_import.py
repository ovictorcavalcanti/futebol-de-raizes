"""Tabela de jogos em JSON na fase com tabela (competitions.table_import + admin da fase)."""

from __future__ import annotations

import json

import pytest

from competitions.models import Group, GroupTeam, Round, Stage, Team
from competitions.table_import import TableImportError, apply_table, parse_table
from core import timeutils
from matches.models import Match
from realtime.models import Outbox
from tests.factories import make_competition, make_stage
from tests.test_admin import change_url, post_data, url

pytestmark = pytest.mark.django_db


@pytest.fixture
def teams():
    make = lambda name, code: Team.objects.create(name=name, short_name=code)  # noqa: E731
    return {
        "spt": make("Sport", "SPT"), "nau": make("Náutico", "NAU"), "scz": make("Santa Cruz", "SCZ"),
        "cen": make("Central", "CEN"), "cli": make("Centro Limoeirense", "CEN"), "ret": make("Retrô", "RET"),
    }


def table(*rounds, **top):
    return json.dumps({**top, "rodadas": list(rounds)})


def game(home, away, when="2027-01-15 19:00", **extra):
    return {"mandante": home, "visitante": away, "data": when, **extra}


ALL = ["SPT", "Náutico", "Santa Cruz", "Retrô"]


def test_parse_resolves_sigla_name_and_id(teams):
    plan = parse_table(table({"numero": 1, "nome": "1ª rodada", "jogos": [
        game("spt", "Náutico", local="Ilha do Retiro", cidade="Recife"),
        game("Santa Cruz", {"id": teams["cli"].id}, "2027-01-16T16:00"),
    ]}, times=[*ALL, {"id": teams["cli"].id}]), None)
    (rnd,) = plan.rounds
    assert (rnd.number, rnd.name) == (1, "1ª rodada")
    first, second = rnd.matches
    assert (first.home, first.away, first.venue) == (teams["spt"], teams["nau"], "Ilha do Retiro")
    assert first.kickoff.tzinfo == timeutils.app_tz() and (first.kickoff.hour, first.kickoff.minute) == (19, 0)
    assert (second.home, second.away) == (teams["scz"], teams["cli"])
    assert len(plan.entries) == 5


def test_parse_errors_point_to_round_and_match(teams):
    with pytest.raises(TableImportError) as info:
        parse_table(table(
            {"numero": 1, "jogos": [
                game("CEN", "SPT"),  # sigla ambígua
                game("XYZ", "NAU", "15/01/2027"),  # time e data
                game("SPT", "SPT"),
                game("Central", "RET"),  # Central fora do campeonato
            ]},
            {"numero": 2, "jogos": [
                game("SPT", "NAU", "2027-01-22 19:00", juiz="Fulano"),
                game("NAU", "SCZ", "2027-01-22 21:00"),
                game("RET", "SCZ", "2027-01-22 21:00"),
            ]},
            {"numero": 2, "jogos": [game("SPT", "NAU", "2027-01-29 19:00")]},
            times=ALL,
        ), None)
    messages = "\n".join(info.value.messages)
    assert "Rodada 1, jogo 1 (mandante): a sigla CEN é de mais de um time (Central" in messages
    assert 'Rodada 1, jogo 2 (mandante): time "XYZ" não cadastrado' in messages
    assert 'Rodada 1, jogo 2: data "15/01/2027" fora do formato' in messages
    assert "Rodada 1, jogo 3: mandante e visitante são o mesmo time (Sport)" in messages
    assert 'Rodada 1, jogo 4: Central não está no campeonato (inclua em "times" ou na tabela da fase).' in messages
    assert "Rodada 2, jogo 1: chave desconhecida juiz." in messages
    assert "Rodada 2, jogo 2: Náutico já joga nesta rodada." in messages
    assert "Rodada 2, jogo 3: Santa Cruz já joga nesta rodada." in messages
    assert "Rodada 2: rodada repetida no arquivo." in messages


def test_sigla_prefers_the_stage_teams_and_stage_teams_need_no_declaration(teams):
    comp, season = make_competition()
    stage = make_stage(season)
    for key in ("cli", "spt"):  # Centro Limoeirense e Sport já na fase
        GroupTeam.objects.create(group=stage.groups.get(), team=teams[key])
    plan = parse_table(table({"numero": 1, "jogos": [game("CEN", "SPT")]}), stage)
    assert plan.rounds[0].matches[0].home == teams["cli"] and plan.entries == {}


def test_admin_creates_stage_with_rounds_matches_and_table(admin_client_fdr, teams):
    comp, season = make_competition()
    response = admin_client_fdr.get(url(Stage, "add") + f"?season={season.pk}")
    data = post_data(response)
    data.update(season=str(season.pk), name="1ª fase", position="1", format="league", table_json=table(
        {"numero": 1, "jogos": [game("SPT", "NAU"), game("SCZ", "RET", "2027-01-16 16:00")]},
        {"numero": 2, "nome": "2ª rodada", "jogos": [game("NAU", "SCZ", "2027-01-22 19:00")]},
        times=ALL,
    ))
    response = admin_client_fdr.post(url(Stage, "add") + f"?season={season.pk}", data, follow=True)
    assert response.status_code == 200, response.content.decode()[:2000]
    stage = Stage.objects.get(season=season)
    assert list(stage.rounds.order_by("number").values_list("number", "name")) == [(1, ""), (2, "2ª rodada")]
    assert Match.objects.filter(stage=stage).count() == 3
    assert set(GroupTeam.objects.filter(group__stage=stage).values_list("team__short_name", flat=True)) == {"SPT", "NAU", "SCZ", "RET"}
    assert "Tabela em JSON: 2 rodada(s) e 3 jogo(s) criados" in response.content.decode()
    assert Outbox.objects.filter(topic="standings").exists()  # classificação nova publicada


def test_admin_error_saves_nothing_and_rerun_skips_existing(admin_client_fdr, teams):
    comp, season = make_competition()
    stage = make_stage(season)
    data = post_data(admin_client_fdr.get(change_url(stage)))
    data["table_json"] = table({"numero": 1, "jogos": [game("SPT", "NAU")]})  # ninguém na fase ainda
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200 and "Sport e Náutico não estão no campeonato" in response.content.decode()
    assert not Round.objects.filter(stage=stage).exists() and not Match.objects.filter(stage=stage).exists()
    assert not GroupTeam.objects.filter(group__stage=stage).exists()

    good = table({"numero": 1, "jogos": [game("SPT", "NAU")]}, times=["SPT", "NAU"])
    for _ in range(2):  # a segunda vez pula o jogo e os times que já existem
        data = post_data(admin_client_fdr.get(change_url(stage)))
        data["table_json"] = good
        assert admin_client_fdr.post(change_url(stage), data).status_code == 302
    assert Match.objects.filter(stage=stage).count() == 1 and stage.rounds.count() == 1
    assert GroupTeam.objects.filter(group__stage=stage).count() == 2


def test_admin_refuses_table_for_knockout(admin_client_fdr, teams):
    comp, season = make_competition()
    response = admin_client_fdr.get(url(Stage, "add") + f"?season={season.pk}")
    data = post_data(response)
    data.update(season=str(season.pk), name="Mata-mata", position="2", format="knockout",
                table_json=table({"numero": 1, "jogos": [game("SPT", "NAU")]}))
    response = admin_client_fdr.post(url(Stage, "add") + f"?season={season.pk}", data)
    assert response.status_code == 200 and "só para fase de pontos corridos ou de grupos" in response.content.decode()
    assert not Stage.objects.filter(season=season).exists()


def groups_stage():
    comp, season = make_competition()
    return make_stage(season, Stage.Format.GROUPS, name="Grupos")


def test_groups_stage_declares_groups_and_allows_cross_group_matches(teams):
    stage = groups_stage()
    plan = parse_table(table(
        {"numero": 1, "jogos": [game("SPT", "SCZ"), game("NAU", "RET", "2027-01-15 21:00")]},  # A × B (Copa do Nordeste)
        {"numero": 2, "jogos": [game("SPT", "NAU", "2027-01-22 19:00")]},
        grupos={"A": ["SPT", "NAU"], "B": ["SCZ", "RET"]},
    ), stage)
    result = apply_table(stage, plan)
    assert sorted(result.groups_created) == ["A", "B"] and len(result.teams_added) == 4
    assert result.matches_created == 3
    by_home = dict(Match.objects.filter(stage=stage, round__number=1).values_list("home_team__short_name", "group__name"))
    assert by_home == {"SPT": "A", "NAU": "A"}  # o jogo fica no grupo do mandante


def test_groups_stage_refuses_team_in_two_groups_and_outsiders(teams):
    stage = groups_stage()
    GroupTeam.objects.create(group=Group.objects.create(stage=stage, name="A"), team=teams["spt"])
    with pytest.raises(TableImportError) as info:
        parse_table(table(
            {"numero": 1, "jogos": [game("SPT", "SCZ"), game("Central", "NAU")]},
            grupos={"B": ["SPT", "SCZ", "NAU"], "C": ["NAU"]},
        ), stage)
    messages = "\n".join(info.value.messages)
    assert "Grupo B, item 1: Sport já está no grupo A." in messages
    assert "Grupo C, item 1: Náutico já está no grupo B." in messages
    assert 'Rodada 1, jogo 2: Central não está no campeonato (inclua em "grupos" ou na tabela da fase).' in messages
    with pytest.raises(TableImportError, match='use "grupos"'):
        parse_table(table({"numero": 1, "jogos": [game("SPT", "SCZ")]}, times=["SCZ"]), stage)
    with pytest.raises(TableImportError, match="Pontos corridos não tem grupos"):
        parse_table(table({"numero": 1, "jogos": [game("SPT", "SCZ")]}, grupos={"A": ["SPT", "SCZ"]}), None)


def test_admin_groups_stage_import(admin_client_fdr, teams):
    stage = groups_stage()
    data = post_data(admin_client_fdr.get(change_url(stage)))
    data["table_json"] = table({"numero": 1, "jogos": [game("SPT", "RET")]}, grupos={"A": ["SPT"], "B": ["RET"]})
    response = admin_client_fdr.post(change_url(stage), data, follow=True)
    content = response.content.decode()
    assert "Tabela em JSON: 1 rodada(s) e 1 jogo(s) criados" in content and "grupos criados: A, B" in content
    assert Match.objects.get(stage=stage).group.name == "A"
