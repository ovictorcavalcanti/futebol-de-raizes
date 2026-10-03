"""Regressões da revisão do back (serviços, leitura, API e API pública).

Cada teste cobre um defeito encontrado na revisão:

* evento de status lançado por `/events` sem a permissão `matches.change_status`;
* chave de idempotência com o sufixo reservado dos derivados (`:auto:`) → 500 / replay errado;
* reagendamento para uma data que não cabe em UTC → 500 em vez de 422;
* `created_at` lido antes da trava de escrita;
* `on_match_edited` publicava o confronto velho (vencedor antigo) e não avisava os outros jogos;
* corpo ilegível sem `details.field`; 405 fora do formato de erro da API;
* jogo de mata-mata sem rodada própria (só a do confronto) sumia da rodada.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from core import timeutils
from core.locks import holds_write_lock
from matches import services
from matches.domain import DomainError, NewEvent
from matches.models import Match, MatchEvent, Tie
from public_api.models import ApiKey
from realtime.models import Outbox
from tests.factories import make_knockout, make_league, make_match, make_tie_matches

pytestmark = pytest.mark.django_db


class Op:
    """Lança pelos serviços com relógio controlado (`at`)."""

    def __init__(self, match, user, start=None):
        self.match_id = match.id
        self.user = user
        self.clock = start or timeutils.now() - timedelta(minutes=150)
        self.n = 0

    def post(self, type_, *, after=1.0, **fields):
        self.clock += timedelta(minutes=after)
        self.n += 1
        return services.post_event(
            self.match_id, self.user, NewEvent(type=type_, **fields), idempotency_key=f"r-{self.match_id}-{self.n}", at=self.clock
        )

    def goal(self, team, minute):
        return self.post("goal", team_id=team.id, minute=minute, payload={"player": "Fulano"})

    def play(self, *, end=True):
        self.post("match_start")
        self.post("half_time", after=47)
        self.post("second_half_start", after=15)
        if end:
            return self.post("match_end", after=50)
        return None


@pytest.fixture
def league(db):
    return make_league(n_teams=4, team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"])


@pytest.fixture
def match(league):
    sport, nautico = league["teams"][0], league["teams"][1]
    return make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now() - timedelta(minutes=5), round=league["rounds"][0])


def user_with(django_user_model, username, *codenames):
    user = django_user_model.objects.create_user(username, password="senha-forte-123")
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames, content_type__app_label="matches"))
    client = Client()
    client.force_login(user)
    return user, client


def post_json(client, path, body, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(path, body, content_type="application/json", headers=headers)


# --- Permissão: status por /events ---------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["postponed", "cancelled", "suspended", "rescheduled"])
def test_status_event_through_events_route_needs_change_status_permission(match, django_user_model, event_type):
    _, client = user_with(django_user_model, "so-lances", "post_event")
    body = {"type": event_type, "payload": {"kickoff_at": "2026-12-01T19:30"}}
    response = post_json(client, f"/api/ops/matches/{match.id}/events", body, key="k1")
    assert response.status_code == 403, response.content
    data = response.json()
    assert data["code"] == "permission_denied"
    assert data["details"]["required_any"] == ["matches.change_status"]
    assert not MatchEvent.objects.filter(match=match).exists()
    assert Match.objects.get(pk=match.pk).status == "scheduled"


def test_status_event_through_events_route_works_with_both_permissions(match, django_user_model):
    _, client = user_with(django_user_model, "lances-e-status", "post_event", "change_status")
    response = post_json(client, f"/api/ops/matches/{match.id}/events", {"type": "postponed"}, key="k1")
    assert response.status_code == 201, response.content
    assert response.json()["match"]["status"] == "postponed"


def test_game_events_through_events_route_need_only_post_event(match, django_user_model):
    _, client = user_with(django_user_model, "so-lances-2", "post_event")
    response = post_json(client, f"/api/ops/matches/{match.id}/events", {"type": "match_start"}, key="k1")
    assert response.status_code == 201, response.content


# --- Idempotência: sufixo reservado dos derivados ------------------------------------------------


def test_idempotency_key_with_reserved_derived_suffix_is_rejected(league, match, operator_user, operator_client):
    sport = league["teams"][0]
    services.post_event(match.id, operator_user, NewEvent(type="match_start"), idempotency_key="inicio")
    with pytest.raises(services.InvalidInput) as info:
        services.post_event(
            match.id, operator_user, NewEvent(type="yellow_card", minute=3, team_id=sport.id, payload={"player": "A"}),
            idempotency_key="amarelo:auto:1",
        )
    assert info.value.field == "idempotency_key"
    response = post_json(
        operator_client, f"/api/ops/matches/{match.id}/events",
        {"type": "yellow_card", "minute": 3, "team_id": sport.id, "payload": {"player": "A"}}, key="amarelo:auto:1",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input"
    assert response.json()["details"]["field"] == "Idempotency-Key"
    response = post_json(operator_client, f"/api/ops/matches/{match.id}/status", {"action": "suspend"}, key="x:auto:2")
    assert response.status_code == 400 and response.json()["details"]["field"] == "Idempotency-Key"

    # O 2º amarelo com a chave "amarelo" grava o vermelho automático em "amarelo:auto:1" sem conflito.
    first = post_json(
        operator_client, f"/api/ops/matches/{match.id}/events",
        {"type": "yellow_card", "minute": 3, "team_id": sport.id, "payload": {"player": "A"}}, key="primeiro",
    )
    assert first.status_code == 201
    second = post_json(
        operator_client, f"/api/ops/matches/{match.id}/events",
        {"type": "yellow_card", "minute": 4, "team_id": sport.id, "payload": {"player": "A"}}, key="amarelo",
    )
    assert second.status_code == 201, second.content
    assert [row["type"] for row in second.json()["derived"]] == ["red_card"]
    assert MatchEvent.objects.get(match=match, idempotency_key="amarelo:auto:1").type == "red_card"
    replay = post_json(operator_client, f"/api/ops/matches/{match.id}/events", {"type": "goal"}, key="amarelo")
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["event"]["type"] == "yellow_card"


# --- Reagendamento fora do intervalo -------------------------------------------------------------


@pytest.mark.parametrize("kickoff_at", ["9999-12-31T23:59:59-03:00", "0001-01-01T00:10:00+05:00"])
def test_reschedule_to_a_date_out_of_utc_range_is_422_and_writes_nothing(match, operator_client, kickoff_at):
    before = Match.objects.get(pk=match.pk)
    response = post_json(
        operator_client, f"/api/ops/matches/{match.id}/status", {"action": "reschedule", "kickoff_at": kickoff_at}, key="r1"
    )
    assert response.status_code == 422, response.content
    assert response.json()["code"] == "invalid_payload"
    assert response.json()["details"]["field"] == "kickoff_at"
    response = post_json(
        operator_client, f"/api/ops/matches/{match.id}/events", {"type": "rescheduled", "payload": {"kickoff_at": kickoff_at}}, key="r2"
    )
    assert response.status_code == 422, response.content
    assert response.json()["details"]["field"] == "payload.kickoff_at"
    after = Match.objects.get(pk=match.pk)
    assert not MatchEvent.objects.filter(match=match).exists()
    assert (after.kickoff_at, after.version) == (before.kickoff_at, before.version)


# --- Horário do lançamento lido com a trava -------------------------------------------------------


def test_created_at_and_voided_at_are_read_while_holding_the_write_lock(league, match, operator_user, monkeypatch):
    real_now = timeutils.now
    seen: list[bool] = []

    def spy():
        seen.append(holds_write_lock())
        return real_now()

    monkeypatch.setattr(timeutils, "now", spy)
    result = services.post_event(match.id, operator_user, NewEvent(type="match_start"), idempotency_key="inicio")
    assert seen and all(seen), seen
    seen.clear()
    services.void_event(match.id, result.event.id, operator_user)
    assert seen and all(seen), seen
    seen.clear()
    services.change_status(match.id, operator_user, "postpone", idempotency_key="adiar")
    assert seen and all(seen), seen


# --- Admin: confronto recalculado sai na mensagem ----------------------------------------------


def last_match_messages(mark: int) -> list[dict]:
    return [row.payload for row in Outbox.objects.filter(id__gt=mark, topic="match").order_by("id")]


def test_on_match_edited_publishes_the_recomputed_tie_and_notifies_the_other_leg(league, operator_user):
    ko = make_knockout(league["season"], legs=2, extra_time=False)
    tie, team_a = ko["tie"], ko["team_a"]
    leg1, leg2 = make_tie_matches(tie, kickoff=timeutils.now() - timedelta(days=7))
    op1 = Op(leg1, operator_user, start=timeutils.now() - timedelta(days=7))
    op1.post("match_start")
    op1.goal(team_a, 5)
    op1.post("half_time", after=47)
    op1.post("second_half_start", after=15)
    op1.post("match_end", after=50)
    Op(leg2, operator_user).play()
    tie.refresh_from_db()
    assert tie.winner_team_id == team_a.id

    # Confronto gravado ficou velho (ex.: edição por fora): o admin salva a volta.
    Tie.objects.filter(pk=tie.pk).update(winner_team=None, decided_by="")
    leg1_version = Match.objects.get(pk=leg1.pk).version
    mark = Outbox.objects.order_by("-id").values_list("id", flat=True).first() or 0
    services.on_match_edited(Match.objects.get(pk=leg2.pk), {"tie": tie.pk}, user=operator_user)

    tie.refresh_from_db()
    assert (tie.winner_team_id, tie.decided_by) == (team_a.id, "aggregate")
    messages = last_match_messages(mark)
    assert [message["match"]["id"] for message in messages] == [leg2.id, leg1.id]
    for message in messages:
        assert message["match"]["tie"]["winner_team_id"] == team_a.id
        assert message["match"]["tie"]["decided_by"] == "aggregate"
        assert message["match"]["tie"]["complete"] is True
    assert Match.objects.get(pk=leg1.pk).version == leg1_version + 1
    assert messages[1]["match"]["version"] == leg1_version + 1


def test_on_match_edited_without_tie_change_publishes_only_the_match(league, operator_user):
    ko = make_knockout(league["season"], legs=2, extra_time=False)
    leg1, leg2 = make_tie_matches(ko["tie"], kickoff=timeutils.now() - timedelta(days=7))
    leg1_version = Match.objects.get(pk=leg1.pk).version
    mark = Outbox.objects.order_by("-id").values_list("id", flat=True).first() or 0
    services.on_match_edited(Match.objects.get(pk=leg2.pk), {"home_team": leg2.home_team_id}, user=operator_user)
    assert [message["match"]["id"] for message in last_match_messages(mark)] == [leg2.id]
    assert Match.objects.get(pk=leg1.pk).version == leg1_version


# --- Formato de erro --------------------------------------------------------------------------


def test_unparseable_body_is_400_invalid_input_with_field(match, operator_client):
    response = operator_client.post(
        f"/api/ops/matches/{match.id}/events", "{quebrado", content_type="application/json", headers={"Idempotency-Key": "k"}
    )
    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "invalid_input" and data["details"]["field"] == "body"
    assert response["Cache-Control"] == "no-store"


def test_wrong_method_is_json_405(match, operator_client, client):
    response = operator_client.get(f"/api/ops/matches/{match.id}/events")
    assert response.status_code == 405
    data = response.json()
    assert data["code"] == "method_not_allowed" and data["details"]["allowed"] == ["POST"]
    assert response["Allow"] == "POST"
    assert response["Cache-Control"] == "no-store"
    response = client.post("/api/home", {}, content_type="application/json")
    assert response.status_code == 405 and response.json()["code"] == "method_not_allowed"
    # as rotas certas continuam iguais (CSRF conferido na rota, não no middleware)
    assert client.get("/api/home").status_code == 200
    assert Client(enforce_csrf_checks=True).post(f"/api/ops/matches/{match.id}/events").status_code == 401


# --- Mata-mata sem rodada própria --------------------------------------------------------------


def test_knockout_match_without_own_round_belongs_to_the_tie_round(league, operator_user, client):
    ko = make_knockout(league["season"], legs=1, extra_time=False, name="Final", round_number=1)
    (final,) = make_tie_matches(ko["tie"], kickoff=timeutils.now() + timedelta(days=3))
    Match.objects.filter(pk=final.pk).update(round=None)
    # a fase de grupos fica toda encerrada: a fase atual é o mata-mata
    Match.objects.filter(stage=league["stage"]).update(status="finished")

    data = client.get(f"/api/competitions/{league['competition'].slug}").json()
    assert data["current_stage_id"] == ko["stage"].id
    assert data["current_round_id"] == ko["round"].id
    assert [item["id"] for item in data["stage"]["matches"]] == [final.id]
    assert data["stage"]["matches"][0]["round"] == {"id": ko["round"].id, "number": 1, "name": "Final"}
    assert [item["id"] for item in data["stage"]["ties"][0]["matches"]] == [final.id]

    listed = client.get(f"/api/matches?roundId={ko['round'].id}").json()["matches"]
    assert [item["id"] for item in listed] == [final.id]

    _, raw = ApiKey.generate("Parceiro", 1000)
    public = client.get(f"/public/v1/matches?round_id={ko['round'].id}", headers={"X-API-Key": raw}).json()
    assert [item["id"] for item in public["matches"]] == [final.id]
    assert public["matches"][0]["round"]["id"] == ko["round"].id


def test_reschedule_still_converts_brasilia_to_utc(match, operator_user):
    result = services.change_status(match.id, operator_user, "reschedule", idempotency_key="r", kickoff_at="2026-12-01T19:30")
    assert result.event.payload["kickoff_at"] == "2026-12-01T22:30:00Z"
    with pytest.raises(DomainError) as info:
        services.change_status(match.id, operator_user, "reschedule", idempotency_key="r2", kickoff_at="9999-12-31T23:59:59-03:00")
    assert info.value.code == "invalid_payload"
