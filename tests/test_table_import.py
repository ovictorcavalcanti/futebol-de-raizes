"""Tabela de jogos em JSON na fase de pontos corridos (competitions.table_import + admin da fase)."""

from __future__ import annotations

import json

import pytest

from competitions.models import GroupTeam, Round, Stage, Team
from competitions.table_import import TableImportError, parse_table
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


def table(*rounds):
    return json.dumps({"rodadas": list(rounds)})


def test_parse_resolves_sigla_name_and_id(teams):
    plan = parse_table(table({"numero": 1, "nome": "1ª rodada", "jogos": [
        {"mandante": "spt", "visitante": "Náutico", "data": "2027-01-15 19:00", "local": "Ilha do Retiro", "cidade": "Recife"},
        {"mandante": "Santa Cruz", "visitante": {"id": teams["cli"].id}, "data": "2027-01-16T16:00"},
    ]}), None)
    (rnd,) = plan.rounds
    assert (rnd.number, rnd.name) == (1, "1ª rodada")
    first, second = rnd.matches
    assert (first.home, first.away, first.venue) == (teams["spt"], teams["nau"], "Ilha do Retiro")
    assert first.kickoff.tzinfo == timeutils.app_tz() and (first.kickoff.hour, first.kickoff.minute) == (19, 0)
    assert (second.home, second.away) == (teams["scz"], teams["cli"])


def test_parse_errors_point_to_round_and_match(teams):
    with pytest.raises(TableImportError) as info:
        parse_table(table(
            {"numero": 1, "jogos": [
                {"mandante": "CEN", "visitante": "SPT", "data": "2027-01-15 19:00"},  # sigla ambígua
                {"mandante": "XYZ", "visitante": "NAU", "data": "15/01/2027"},  # time e data
                {"mandante": "SPT", "visitante": "SPT", "data": "2027-01-15 19:00"},
            ]},
            {"numero": 2, "jogos": [
                {"mandante": "SPT", "visitante": "NAU", "data": "2027-01-22 19:00", "juiz": "Fulano"},
                {"mandante": "NAU", "visitante": "SCZ", "data": "2027-01-22 21:00"},
            ]},
            {"numero": 2, "jogos": [{"mandante": "SPT", "visitante": "NAU", "data": "2027-01-29 19:00"}]},
        ), None)
    messages = info.value.messages
    assert any("Rodada 1, jogo 1 (mandante): a sigla CEN é de mais de um time (Central" in m for m in messages)
    assert 'Rodada 1, jogo 2 (mandante): time "XYZ" não cadastrado' in "\n".join(messages)
    assert any('Rodada 1, jogo 2: data "15/01/2027" fora do formato' in m for m in messages)
    assert any("Rodada 1, jogo 3: mandante e visitante são o mesmo time (Sport)" in m for m in messages)
    assert "Rodada 2, jogo 1: chave desconhecida juiz." in messages
    assert any("Rodada 2, jogo 2: Náutico joga duas vezes na rodada" in m for m in messages)
    assert "Rodada 2: rodada repetida no arquivo." in messages


def test_sigla_prefers_the_stage_teams(teams):
    comp, season = make_competition()
    stage = make_stage(season)
    GroupTeam.objects.create(group=stage.groups.get(), team=teams["cli"])  # só o Centro Limoeirense na fase
    plan = parse_table(table({"numero": 1, "jogos": [{"mandante": "CEN", "visitante": "SPT", "data": "2027-01-15 19:00"}]}), stage)
    assert plan.rounds[0].matches[0].home == teams["cli"]


def test_admin_creates_stage_with_rounds_matches_and_table(admin_client_fdr, teams):
    comp, season = make_competition()
    response = admin_client_fdr.get(url(Stage, "add") + f"?season={season.pk}")
    data = post_data(response)
    data.update(season=str(season.pk), name="1ª fase", position="1", format="league", table_json=table(
        {"numero": 1, "jogos": [
            {"mandante": "SPT", "visitante": "NAU", "data": "2027-01-15 19:00"},
            {"mandante": "SCZ", "visitante": "RET", "data": "2027-01-16 16:00"},
        ]},
        {"numero": 2, "nome": "2ª rodada", "jogos": [{"mandante": "NAU", "visitante": "SCZ", "data": "2027-01-22 19:00"}]},
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
    data["table_json"] = table({"numero": 1, "jogos": [{"mandante": "CEN", "visitante": "SPT", "data": "2027-01-15 19:00"}]})
    response = admin_client_fdr.post(change_url(stage), data)
    assert response.status_code == 200 and "a sigla CEN é de mais de um time" in response.content.decode()
    assert not Round.objects.filter(stage=stage).exists() and not Match.objects.filter(stage=stage).exists()

    good = table({"numero": 1, "jogos": [{"mandante": "SPT", "visitante": "NAU", "data": "2027-01-15 19:00"}]})
    for _ in range(2):  # a segunda vez pula o jogo que já existe
        data = post_data(admin_client_fdr.get(change_url(stage)))
        data["table_json"] = good
        assert admin_client_fdr.post(change_url(stage), data).status_code == 302
    assert Match.objects.filter(stage=stage).count() == 1 and stage.rounds.count() == 1


def test_admin_refuses_table_for_knockout(admin_client_fdr, teams):
    comp, season = make_competition()
    response = admin_client_fdr.get(url(Stage, "add") + f"?season={season.pk}")
    data = post_data(response)
    data.update(season=str(season.pk), name="Mata-mata", position="2", format="knockout",
                table_json=table({"numero": 1, "jogos": [{"mandante": "SPT", "visitante": "NAU", "data": "2027-01-15 19:00"}]}))
    response = admin_client_fdr.post(url(Stage, "add") + f"?season={season.pk}", data)
    assert response.status_code == 200 and "só para fase de pontos corridos" in response.content.decode()
    assert not Stage.objects.filter(season=season).exists()
