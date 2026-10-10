"""Rotas de operação (`/api/ops`) — fase 3 do plano pela API: um jogo inteiro
lançado pela API com gol anulado e o placar fechando; o gol anulado sai dos
últimos gols; transições inválidas → 422 com o código da regra; idempotência;
confirmação de avisos; cancelamento (com cascata); mudança de status; catálogo;
formato inválido → 400; perfil sem permissão → 403.

`ApiOp` e `use_clock` são usados também pelos outros testes da API.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from core import timeutils
from matches.domain import CATALOG
from matches.models import Match, MatchEvent
from tests.factories import make_league, make_match

pytestmark = pytest.mark.django_db


# --- Apoio (também usado em test_api_read e test_api_knockout) -----------------------------


class Clock:
    """Relógio de teste: substitui `core.timeutils.now` (lançamentos e leituras)."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def set(self, moment: datetime) -> datetime:
        self.now = moment
        return moment

    def advance(self, minutes: float = 1.0) -> datetime:
        self.now += timedelta(minutes=minutes)
        return self.now


def use_clock(monkeypatch, start: datetime) -> Clock:
    clock = Clock(start)
    monkeypatch.setattr(timeutils, "now", clock)
    return clock


class ApiOp:
    """Operador de teste: lança pela API, com chave de idempotência nova a cada envio."""

    def __init__(self, client: Client, match: Match, clock: Clock | None = None):
        self.client = client
        self.match_id = match.id
        self.clock = clock
        self.n = 0

    def key(self) -> str:
        self.n += 1
        return f"k-{self.match_id}-{self.n}"

    def _tick(self, after: float) -> None:
        if self.clock is not None and after:
            self.clock.advance(after)

    def _post(self, path: str, body: dict | None, *, expect: int, key: str | None = None) -> dict:
        headers = {"Idempotency-Key": key} if key is not None else {}
        response = self.client.post(path, body if body is not None else {}, content_type="application/json", headers=headers)
        assert response.status_code == expect, (response.status_code, response.content.decode())
        return response.json()

    def post(self, type_: str, *, expect: int = 201, key: str | None = None, after: float = 1.0, **body) -> dict:
        self._tick(after)
        return self._post(f"/api/ops/matches/{self.match_id}/events", {"type": type_, **body}, expect=expect, key=key or self.key())

    def goal(self, team, minute: int, player: str = "Fulano", *, expect: int = 201, **payload) -> dict:
        return self.post("goal", team_id=team.id, minute=minute, payload={"player": player, **payload}, expect=expect)

    def status(self, action: str, *, expect: int = 201, key: str | None = None, after: float = 1.0, **body) -> dict:
        self._tick(after)
        return self._post(f"/api/ops/matches/{self.match_id}/status", {"action": action, **body}, expect=expect, key=key or self.key())

    def void(self, event_id: int, *, expect: int = 200, reason: str = "", after: float = 0.5) -> dict:
        self._tick(after)
        return self._post(f"/api/ops/matches/{self.match_id}/events/{event_id}/void", {"reason": reason}, expect=expect)

    def play_to_second_half(self) -> None:
        self.post("match_start")
        self.post("half_time", after=47)
        self.post("second_half_start", after=15)

    def detail(self) -> dict:
        response = self.client.get(f"/api/matches/{self.match_id}")
        assert response.status_code == 200, response.content.decode()
        return response.json()


def assert_error(body: dict, code: str) -> None:
    assert body["code"] == code, body
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["details"], dict)


# --- Fixtures ----------------------------------------------------------------------------


@pytest.fixture
def league(db):
    return make_league(
        n_teams=4,
        team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"],
        zones=[{"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 2}],
    )


@pytest.fixture
def match(league):
    sport, nautico = league["teams"][0], league["teams"][1]
    return make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now() - timedelta(minutes=5), round=league["rounds"][0])


@pytest.fixture
def op(operator_client, match):
    return ApiOp(operator_client, match)


def events_count(match) -> int:
    return MatchEvent.objects.filter(match=match).count()


# --- Jogo inteiro ------------------------------------------------------------------------


def test_whole_match_through_api_with_annulled_goal(league, match, operator_client, monkeypatch):
    sport, nautico = league["teams"][0], league["teams"][1]
    clock = use_clock(monkeypatch, timeutils.now().replace(microsecond=0) - timedelta(minutes=130))
    # Início no relógio do teste: perto da meia-noite de Brasília, "agora − 5 min" seria outro
    # dia que o do relógio, e a partida ficaria fora da home pedida no fim.
    Match.objects.filter(pk=match.pk).update(kickoff_at=clock.now)
    op = ApiOp(operator_client, match, clock)

    start = op.post("match_start")
    assert start["replayed"] is False and start["warnings"] == [] and start["derived"] == []
    assert start["event"]["type"] == "match_start" and start["event"]["period"] == "first_half"
    assert start["match"]["status"] == "live" and start["match"]["period"] == "first_half"
    assert "goal" in start["available"]["events"] and "suspend" in start["available"]["status"]
    assert start["match"]["clock"]["running"] is True and "events" in start["match"]

    g1 = op.goal(sport, 10, "Zé Roberto")
    assert (g1["match"]["home_score"], g1["match"]["away_score"]) == (1, 0)
    assert g1["event"]["score_after"] == {"home": 1, "away": 0} and g1["event"]["team_side"] == "home"
    g2 = op.goal(nautico, 20, "Kayo Lima")
    assert (g2["match"]["home_score"], g2["match"]["away_score"]) == (1, 1)
    g3 = op.goal(sport, 30, "Gustavo Coutinho", origin="penalty")
    assert g3["event"]["icon"] == "ball-penalty"

    annul = op.post("goal_annulled", minute=31, annuls_event_id=g2["event"]["id"], payload={"reason": "Impedimento"})
    assert annul["event"]["team_id"] == nautico.id  # herdado do gol
    assert (annul["match"]["home_score"], annul["match"]["away_score"]) == (2, 0)
    op.post("yellow_card", team_id=nautico.id, minute=40, payload={"player": "Thiago Freitas"})
    half = op.post("half_time", after=12)
    assert half["match"]["period"] == "half_time" and half["match"]["clock"] is None
    op.post("second_half_start", after=15)
    op.goal(nautico, 70, "Kayo Lima")
    end = op.post("match_end", after=50)
    final = end["match"]
    assert final["status"] == "finished" and final["period"] is None and final["winner"] == "home"
    assert (final["home_score"], final["away_score"]) == (2, 1)
    assert final["finished_at"] == timeutils.iso_utc(clock.now)
    assert end["available"]["events"] == []

    # Leitura da partida: placar fechado, o gol anulado fica na linha do tempo, fora dos gols.
    detail = op.detail()
    assert set(detail) >= {"server_time", "timezone", "cursor", "match", "available"}
    assert detail["timezone"] == "America/Sao_Paulo" and detail["cursor"] > 0
    out = detail["match"]
    assert (out["home_score"], out["away_score"]) == (2, 1)
    by_id = {event["id"]: event for event in out["events"]}
    assert by_id[g2["event"]["id"]]["annulled"] is True and by_id[g2["event"]["id"]]["score_after"] is None
    assert by_id[annul["event"]["id"]]["type"] == "goal_annulled"
    assert [goal["player"] for goal in out["goals"]] == ["Zé Roberto", "Gustavo Coutinho", "Kayo Lima"]
    assert out["cards"]["away"] == {"yellow": 1, "red": 0}
    assert [event["sequence"] for event in out["events"]] == sorted(event["sequence"] for event in out["events"])

    # O gol anulado sai dos últimos gols da home.
    day = timeutils.local_today(clock.now).isoformat()
    home = operator_client.get("/api/home", {"date": day}).json()
    latest = [goal["event_id"] for goal in home["latest_goals"]]
    assert g2["event"]["id"] not in latest
    assert len(latest) == 3 and latest[-1] == g1["event"]["id"]

    # Tabela oficial fechada com o jogo.
    table = operator_client.get(f"/api/stages/{league['stage'].id}/standings").json()
    rows = {row["team"]["id"]: row for row in table["groups"][0]["rows"]}
    assert table["kind"] == "official"
    assert rows[sport.id]["points"] == 3 and rows[sport.id]["position"] == 1
    assert (rows[nautico.id]["goals_for"], rows[nautico.id]["goals_against"]) == (1, 2)

    # Encerrado: lance novo é rejeitado.
    body = op.goal(sport, 91, expect=422)
    assert_error(body, "match_not_live")


def test_invalid_transitions_and_rules_return_422_with_code_and_write_nothing(league, match, op):
    sport, retro = league["teams"][0], league["teams"][3]
    assert_error(op.goal(sport, 5, expect=422), "match_not_live")
    assert_error(op.post("half_time", expect=422), "invalid_transition")
    assert events_count(match) == 0

    op.post("match_start")
    cases = [
        ({"type": "match_start"}, "invalid_transition"),
        ({"type": "second_half_start"}, "invalid_transition"),
        ({"type": "extra_time_start"}, "extra_time_not_allowed"),
        ({"type": "penalties_start"}, "penalties_not_allowed"),
        ({"type": "goal", "minute": 10, "team_id": retro.id, "payload": {"player": "X"}}, "team_not_in_match"),
        ({"type": "goal", "minute": 10, "payload": {"player": "X"}}, "team_required"),
        ({"type": "goal", "minute": 50, "team_id": sport.id, "payload": {"player": "X"}}, "invalid_minute"),
        ({"type": "goal_annulled", "minute": 12, "annuls_event_id": 999999, "payload": {"reason": "VAR"}}, "annul_target_invalid"),
        ({"type": "dance", "minute": 10}, "unknown_event_type"),
    ]
    for body, code in cases:
        result = op.post(body.pop("type"), expect=422, **body)
        assert_error(result, code)
        assert "warnings" not in result
    assert events_count(match) == 1
    assert Match.objects.get(pk=match.pk).home_score == 0


def test_player_ids_are_rejected_players_are_names(op, league, match):
    """Jogador não tem cadastro: id de jogador no corpo → 400 invalid_input; o lance
    leva o nome (EventOut.player = {"id": null, "name": ...})."""
    sport = league["teams"][0]
    op.post("match_start")
    body = op.post("goal", expect=400, minute=10, team_id=sport.id, player_id=7, payload={"player": "Zé Roberto"})
    assert_error(body, "invalid_input")
    assert body["details"]["field"] == "player_id"
    body = op.post(
        "substitution", expect=400, minute=11, team_id=sport.id,
        payload={"player_out": "Zé Roberto", "player_in": "Lucas Arcanjo", "player_out_id": 3},
    )
    assert_error(body, "invalid_input")
    assert body["details"]["field"] == "payload.player_out_id"
    assert events_count(match) == 1
    goal = op.goal(sport, 12, "Zé Roberto")
    assert goal["event"]["player"] == {"id": None, "name": "Zé Roberto"}
    sub = op.post("substitution", minute=13, team_id=sport.id, payload={"player_out": "Zé Roberto", "player_in": "Lucas Arcanjo"})
    assert sub["event"]["payload"] == {"player_out": "Zé Roberto", "player_in": "Lucas Arcanjo"}


def test_unknown_match_is_404(op, operator_client):
    response = operator_client.post(
        "/api/ops/matches/999999/events", {"type": "match_start"}, content_type="application/json", headers={"Idempotency-Key": "x"}
    )
    assert response.status_code == 404
    assert_error(response.json(), "not_found")
    response = operator_client.post(
        "/api/ops/matches/999999/status", {"action": "postpone"}, content_type="application/json", headers={"Idempotency-Key": "y"}
    )
    assert response.status_code == 404
    assert operator_client.get("/api/matches/999999").status_code == 404


# --- Idempotência e confirmação -------------------------------------------------------------


def test_same_idempotency_key_twice_is_one_event_and_replay_is_200(league, match, op):
    sport, nautico = league["teams"][0], league["teams"][1]
    op.post("match_start")
    first = op.post("goal", key="clique-duplo", team_id=sport.id, minute=12, payload={"player": "Zé"})
    second = op.post("goal", key="clique-duplo", expect=200, team_id=sport.id, minute=12, payload={"player": "Zé"})
    assert first["replayed"] is False and second["replayed"] is True
    assert second["event"]["id"] == first["event"]["id"]
    assert second["match"]["home_score"] == 1 and second["warnings"] == []
    # outra carga com a mesma chave continua sendo o pedido original
    other = op.post("goal", key="clique-duplo", expect=200, team_id=nautico.id, minute=13, payload={"player": "Kayo"})
    assert other["event"]["id"] == first["event"]["id"] and other["match"]["away_score"] == 0
    # a chave vale entre /events e /status
    status = op.status("suspend", key="clique-duplo", expect=200)
    assert status["replayed"] is True and status["event"]["id"] == first["event"]["id"]
    assert status["match"]["status"] == "live"
    assert events_count(match) == 2


def test_idempotency_key_header_is_required_with_max_length(match, op, operator_client):
    url = f"/api/ops/matches/{match.id}/events"
    for headers in ({}, {"Idempotency-Key": "   "}, {"Idempotency-Key": "k" * 65}):
        response = operator_client.post(url, {"type": "match_start"}, content_type="application/json", headers=headers)
        assert response.status_code == 400, headers
        body = response.json()
        assert_error(body, "invalid_input")
        assert body["details"]["field"] == "Idempotency-Key"
    response = operator_client.post(f"/api/ops/matches/{match.id}/status", {"action": "postpone"}, content_type="application/json")
    assert response.status_code == 400 and response.json()["details"]["field"] == "Idempotency-Key"
    assert events_count(match) == 0
    op.post("match_start", key="k" * 64)
    assert events_count(match) == 1


def test_confirmation_required_then_confirm(league, match, op):
    nautico = league["teams"][1]
    op.post("match_start")
    op.goal(nautico, 30, "Kayo Lima")
    body = op.post("yellow_card", key="amarelo", expect=422, team_id=nautico.id, minute=20, payload={"player": "Thiago"})
    assert_error(body, "confirmation_required")
    assert [warning["code"] for warning in body["warnings"]] == ["minute_decreasing"]
    assert body["warnings"][0]["message"] and body["details"]["codes"] == ["minute_decreasing"]
    assert events_count(match) == 2  # nada gravado

    confirmed = op.post("yellow_card", key="amarelo", confirm=True, team_id=nautico.id, minute=20, payload={"player": "Thiago"})
    assert confirmed["replayed"] is False
    assert [warning["code"] for warning in confirmed["warnings"]] == ["minute_decreasing"]
    assert confirmed["match"]["cards"]["away"]["yellow"] == 1
    assert events_count(match) == 3


# --- Cancelamento ------------------------------------------------------------------------


def test_void_endpoint_with_cascade_already_and_derived(league, match, op, operator_client):
    sport, nautico = league["teams"][0], league["teams"][1]
    op.post("match_start")
    goal = op.goal(nautico, 20, "Kayo Lima")
    annul = op.post("goal_annulled", minute=21, annuls_event_id=goal["event"]["id"], payload={"reason": "Falta"})
    op.post("yellow_card", team_id=sport.id, minute=25, payload={"player": "Fabinho"})
    second = op.post("yellow_card", team_id=sport.id, minute=30, payload={"player": "fabinho"})
    (red,) = second["derived"]
    assert red["type"] == "red_card" and red["derived"] is True and red["icon"] == "card-second-yellow"
    assert second["match"]["cards"]["home"] == {"yellow": 2, "red": 1}

    # gol cancelado leva junto a anulação que apontava para ele
    voided = op.void(goal["event"]["id"], reason="lançado no jogo errado")
    assert voided["voided"] == [goal["event"]["id"], annul["event"]["id"]] and voided["already"] is False
    assert (voided["match"]["home_score"], voided["match"]["away_score"]) == (0, 0)
    assert {event["id"] for event in voided["match"]["events"]}.isdisjoint(voided["voided"])
    assert set(voided["available"]) == {"events", "status"}
    # clique duplo: nada muda
    again = op.void(goal["event"]["id"])
    assert again["already"] is True and again["voided"] == voided["voided"]

    # o vermelho automático só cai com o amarelo de origem
    assert_error(op.void(red["id"], expect=422), "void_derived_event")
    dropped = op.void(second["event"]["id"])
    assert dropped["voided"] == [second["event"]["id"], red["id"]]
    assert dropped["match"]["cards"]["home"] == {"yellow": 1, "red": 0}
    assert MatchEvent.objects.filter(match=match, voided_at__isnull=False).count() == 4

    # lançamento inexistente ou de outra partida → 404
    other = make_match(league["stage"], league["teams"][2], league["teams"][3], round=league["rounds"][0])
    other_op = ApiOp(operator_client, other)
    other_start = other_op.post("match_start")
    body = op.void(other_start["event"]["id"], expect=404)
    assert_error(body, "not_found")
    assert body["details"] == {"event_id": other_start["event"]["id"], "match_id": match.id}
    assert_error(op.void(999999, expect=404), "not_found")
    response = operator_client.post(f"/api/ops/matches/999999/events/{goal['event']['id']}/void", {}, content_type="application/json")
    assert response.status_code == 404
    assert MatchEvent.objects.get(pk=other_start["event"]["id"]).voided_at is None


def test_void_body_is_optional_and_reason_limited(league, match, op, operator_client):
    start = op.post("match_start")
    goal = op.goal(league["teams"][0], 3)
    url = f"/api/ops/matches/{match.id}/events/{goal['event']['id']}/void"
    response = operator_client.post(url, {"reason": "x" * 281}, content_type="application/json")
    assert response.status_code == 400 and response.json()["details"]["field"] == "reason"
    response = operator_client.generic("POST", url, b"", content_type="application/json")
    assert response.status_code == 200 and response.json()["voided"] == [goal["event"]["id"]]
    assert start["event"]["id"] not in response.json()["voided"]


# --- Status --------------------------------------------------------------------------------


def test_status_postpone_reschedule_suspend_resume_cancel(league, operator_client):
    sport, nautico = league["teams"][0], league["teams"][1]
    later = make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now() + timedelta(days=1), round=league["rounds"][0])
    op = ApiOp(operator_client, later)

    postponed = op.status("postpone", reason="Chuva forte")
    assert postponed["replayed"] is False and set(postponed) == {"event", "match", "available", "replayed"}
    assert postponed["event"]["type"] == "postponed" and postponed["event"]["kind"] == "status"
    assert postponed["event"]["payload"] == {"reason": "Chuva forte"}
    assert postponed["match"]["status"] == "postponed"
    assert "reschedule" in postponed["available"]["status"] and postponed["available"]["events"] == []

    assert_error(op.status("reschedule", expect=422), "invalid_payload")
    assert_error(op.status("reschedule", kickoff_at="2026-10-10", expect=422), "invalid_payload")
    rescheduled = op.status("reschedule", key="reagendar", kickoff_at="2026-10-10T16:00")  # sem fuso = Brasília
    assert rescheduled["match"]["status"] == "scheduled"
    assert rescheduled["match"]["kickoff_at"] == "2026-10-10T19:00:00Z"
    assert rescheduled["event"]["payload"]["kickoff_at"] == "2026-10-10T19:00:00Z"
    replay = op.status("reschedule", key="reagendar", kickoff_at="2026-10-11T16:00", expect=200)
    assert replay["replayed"] is True and replay["match"]["kickoff_at"] == "2026-10-10T19:00:00Z"

    assert_error(op.status("resume", expect=422), "invalid_status_action")
    assert_error(op.status("teleport", expect=422), "invalid_status_action")

    op.post("match_start")
    suspended = op.status("suspend", reason="Queda de energia")
    assert suspended["match"]["status"] == "suspended" and suspended["match"]["period"] == "first_half"
    assert suspended["match"]["clock"]["running"] is False and suspended["match"]["clock"]["paused_at"]
    assert set(suspended["available"]["status"]) == {"resume", "cancel"}
    assert_error(op.goal(sport, 10, expect=422), "match_not_live")
    resumed = op.status("resume")
    assert resumed["match"]["status"] == "live" and resumed["match"]["clock"]["running"] is True
    assert_error(op.status("cancel", expect=422), "invalid_status_action")  # ao vivo não cancela
    op.status("suspend")
    cancelled = op.status("cancel", reason="Sem condições de jogo")
    assert cancelled["match"]["status"] == "cancelled" and cancelled["available"] == {"events": [], "status": []}
    types = list(MatchEvent.objects.filter(match=later).order_by("sequence").values_list("type", flat=True))
    assert types == ["postponed", "rescheduled", "match_start", "suspended", "resumed", "suspended", "cancelled"]


# --- Formato, permissão e catálogo -------------------------------------------------------------


def test_invalid_body_is_400_invalid_input(match, op, operator_client):
    url = f"/api/ops/matches/{match.id}/events"
    cases = [
        ({"minute": 10}, "type"),
        ({"type": "goal", "minute": "dez"}, "minute"),
        ({"type": "goal", "payload": ["não", "é", "objeto"]}, "payload"),
        ({"type": "goal", "source": "system"}, "source"),
        ({"type": "goal", "confirm": "talvez"}, "confirm"),
    ]
    for body, field in cases:
        response = operator_client.post(url, body, content_type="application/json", headers={"Idempotency-Key": op.key()})
        assert response.status_code == 400, body
        result = response.json()
        assert_error(result, "invalid_input")
        assert result["details"]["field"] == field, result
        assert result["details"]["errors"][0]["field"] == field
    response = operator_client.post(url, "{não é json", content_type="application/json", headers={"Idempotency-Key": op.key()})
    assert response.status_code == 400
    assert_error(response.json(), "invalid_input")
    assert events_count(match) == 0


def test_feed_source_is_recorded(league, match, op):
    start = op.post("match_start", source="feed")
    assert MatchEvent.objects.get(pk=start["event"]["id"]).source == "feed"


def test_user_without_permission_is_403_and_nothing_is_written(match, django_user_model):
    user = django_user_model.objects.create_user("so-status", password="senha-forte-123")
    user.user_permissions.add(Permission.objects.get(codename="change_status", content_type__app_label="matches"))
    client = Client()
    client.force_login(user)
    op = ApiOp(client, match)
    body = op.post("match_start", expect=403)
    assert_error(body, "permission_denied")
    assert events_count(match) == 0
    assert op.status("postpone")["match"]["status"] == "postponed"
    assert client.get("/api/ops/catalog").status_code == 200


def test_catalog_lists_every_type_and_labels(operator_client):
    response = operator_client.get("/api/ops/catalog")
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, max-age=300"
    data = response.json()
    assert [spec["type"] for spec in data["events"]] == list(CATALOG)
    goal = next(spec for spec in data["events"] if spec["type"] == "goal")
    assert goal["minute"] == "optional" and goal["kind"] == "game"  # gol "a confirmar"
    assert {"name": "team_id", "kind": "team", "label": "Time beneficiado", "required": True, "choices": []} in goal["fields"]
    assert {item["action"] for item in data["status_actions"]} == {"delay", "postpone", "suspend", "resume", "reschedule", "cancel"}
    assert [item["key"] for item in data["periods"]] == [
        "first_half", "half_time", "second_half", "extra_time", "extra_half_time", "extra_second_half", "penalties",
    ]
    assert {item["key"] for item in data["statuses"]} == {"scheduled", "delayed", "live", "finished", "postponed", "suspended", "cancelled"}


def test_ops_responses_are_not_cached(match, op, operator_client):
    response = operator_client.post(
        f"/api/ops/matches/{match.id}/events", {"type": "match_start"}, content_type="application/json", headers={"Idempotency-Key": "c1"}
    )
    assert response.status_code == 201 and response["Cache-Control"] == "no-store"


def test_config_error_maps_to_422(rf):
    from api.main import api
    from standings.domain import ConfigError

    response = api.on_exception(rf.get("/api/stages/1/standings"), ConfigError("zones_overlap", "Faixas de zona sobrepostas."))
    assert response.status_code == 422
    assert json.loads(response.content) == {"code": "zones_overlap", "message": "Faixas de zona sobrepostas.", "details": {}}


def test_status_delay_with_note_then_start(league, operator_client):
    sport, nautico = league["teams"][0], league["teams"][1]
    match = make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now(), round=league["rounds"][0])
    op = ApiOp(operator_client, match)

    assert_error(op.status("delay", expect=422), "invalid_payload")
    delayed = op.status("delay", reason="Chuva forte")
    assert delayed["match"]["status"] == "delayed" and delayed["match"]["status_label"] == "Atrasado"
    assert delayed["match"]["status_note"] == "Chuva forte"
    assert "match_start" in delayed["available"]["events"]
    # A observação também aparece nas listas (resumo) e some quando o jogo começa.
    listed = operator_client.get(f"/api/matches?roundId={league['rounds'][0].id}").json()["matches"]
    assert next(m for m in listed if m["id"] == match.id)["status_note"] == "Chuva forte"
    started = op.post("match_start")
    assert started["match"]["status"] == "live" and started["match"]["status_note"] is None


def test_partial_info_switch(match, op, operator_client):
    op.post("match_start")
    path = f"/api/ops/matches/{match.id}/partial-info"
    on = op._post(path, {"partial_info": True}, expect=200)
    assert on["match"]["partial_info"] is True and on["match"]["clock"] is None and "events" in on["available"]
    off = op._post(path, {"partial_info": False}, expect=200)
    assert off["match"]["partial_info"] is False and off["match"]["clock"] is not None
    assert_error(op._post(path, {}, expect=400), "invalid_input")
    assert_error(op._post("/api/ops/matches/999999/partial-info", {"partial_info": True}, expect=404), "not_found")
    assert Client().post(path, {"partial_info": True}, content_type="application/json").status_code in (401, 403)


def test_goal_to_confirm_then_completed_moves_to_its_minute(match, op, operator_client):
    op.post("match_start")
    known = op._post(f"/api/ops/matches/{match.id}/events", {"type": "goal", "minute": 10, "team_id": match.home_team_id,
                     "payload": {"player": "Zé"}}, expect=201, key=op.key())["event"]
    unknown = op._post(f"/api/ops/matches/{match.id}/events", {"type": "goal", "team_id": match.away_team_id},
                       expect=201, key=op.key())
    assert unknown["event"]["minute"] is None and unknown["event"]["player"]["name"] is None
    assert (unknown["match"]["home_score"], unknown["match"]["away_score"]) == (1, 1)
    goals = [e["id"] for e in operator_client.get(f"/api/matches/{match.id}").json()["match"]["events"] if e["type"] == "goal"]
    assert goals == [known["id"], unknown["event"]["id"]]  # sem minuto: na ordem do lançamento
    op._post(f"/api/ops/matches/{match.id}/events/{unknown['event']['id']}/edit",
             {"type": "goal", "minute": 5, "team_id": match.away_team_id, "payload": {"player": "Kieza"}, "confirm": True},
             expect=200)
    events = operator_client.get(f"/api/matches/{match.id}").json()["match"]["events"]
    goals = [(e["id"], e["player"]["name"], e["score_after"]) for e in events if e["type"] == "goal"]
    assert goals == [  # com o minuto: na ordem do jogo, placar refeito
        (unknown["event"]["id"], "Kieza", {"home": 0, "away": 1}),
        (known["id"], "Zé", {"home": 1, "away": 1}),
    ]


def test_edit_event_corrects_scorer_minute_and_team(league, operator_client):
    from observability.models import AuditLog

    sport, nautico = league["teams"][0], league["teams"][1]
    match = make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now(), round=league["rounds"][0])
    op = ApiOp(operator_client, match)
    op.post("match_start")
    goal = op.goal(sport, 18, "Zé")["event"]
    op.post("half_time")
    op.post("second_half_start")  # já no 2T: a correção mantém o gol no 1T

    edited = op._post(
        f"/api/ops/matches/{match.id}/events/{goal['id']}/edit",
        {"type": "goal", "minute": 20, "team_id": nautico.id, "payload": {"player": "Kieza"}},
        expect=200,
    )
    assert edited["event"]["id"] == goal["id"] and edited["event"]["period"] == "first_half"
    assert edited["event"]["minute"] == 20 and edited["event"]["player"]["name"] == "Kieza"
    assert (edited["match"]["home_score"], edited["match"]["away_score"]) == (0, 1)
    log = AuditLog.objects.get(action="event.edit")
    assert log.data["before"]["payload"]["player"] == "Zé" and log.data["after"]["minute"] == 20

    # Minuto fora do período do lance é recusado; andamento não se edita; o tipo não muda.
    bad = op._post(f"/api/ops/matches/{match.id}/events/{goal['id']}/edit",
                   {"type": "goal", "minute": 70, "team_id": nautico.id, "payload": {"player": "Kieza"}}, expect=422)
    assert bad["code"] == "invalid_minute"
    start_id = next(e["id"] for e in edited["match"]["events"] if e["type"] == "match_start")
    assert op._post(f"/api/ops/matches/{match.id}/events/{start_id}/edit", {"type": "match_start"}, expect=422)["code"] == "event_not_editable"
    assert op._post(f"/api/ops/matches/{match.id}/events/{goal['id']}/edit",
                    {"type": "yellow_card", "team_id": sport.id, "payload": {"player": "X"}}, expect=422)["code"] == "event_not_editable"


def test_edit_earlier_yellow_reconciles_automatic_red(league, match, op):
    from observability.models import AuditLog
    from standings.models import Standing

    sport = league["teams"][0]

    def yellow(player: str, minute: int) -> dict:
        return op.post("yellow_card", team_id=sport.id, minute=minute, payload={"player": player})

    def edit(event_id: int, player: str, minute: int, *, expect: int) -> dict:
        return op._post(f"/api/ops/matches/{match.id}/events/{event_id}/edit",
                        {"type": "yellow_card", "minute": minute, "team_id": sport.id, "payload": {"player": player}}, expect=expect)

    def cards() -> tuple[int, int]:
        row = Standing.objects.get(team=sport, kind=Standing.Kind.LIVE)
        return row.yellow_cards, row.red_cards

    op.post("match_start")
    first = yellow("Alice", 10)["event"]
    second = yellow("Alice", 20)
    (red,) = second["derived"]
    assert cards() == (2, 1)

    # 1º amarelo de Alice passa para Bob: o de 20' deixa de ser o 2º e o vermelho automático cai junto
    edited = edit(first["id"], "Bob", 10, expect=200)
    assert edited["match"]["cards"]["home"] == {"yellow": 2, "red": 0}
    assert red["id"] not in {event["id"] for event in edited["match"]["events"]}
    assert MatchEvent.objects.get(pk=red["id"]).voided_at is not None
    assert AuditLog.objects.get(action="event.edit").data["voided_ids"] == [red["id"]]
    assert cards() == (2, 0)
    assert len(yellow("Alice", 25)["derived"]) == 1  # Alice não ficou expulsa: o novo 2º amarelo gera o vermelho

    # amarelo anterior de Carlos passa para Davi, que já tem um amarelo depois: recusa, nada muda
    carlos = yellow("Carlos", 30)["event"]
    yellow("Davi", 35)
    before = events_count(match), cards()
    refused = edit(carlos["id"], "Davi", 30, expect=422)
    assert_error(refused, "event_not_editable")
    assert refused["details"]["cause"] == "second_yellow_without_red"
    assert (events_count(match), cards()) == before
    assert MatchEvent.objects.get(pk=carlos["id"]).payload["player"] == "Carlos"


def test_edit_event_requires_void_permission(league, operator_user, client):
    from django.contrib.auth.models import Permission

    sport, nautico = league["teams"][0], league["teams"][1]
    match = make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now(), round=league["rounds"][0])
    operator_user.groups.clear()
    operator_user.user_permissions.add(Permission.objects.get(codename="post_event"))
    client.force_login(operator_user)
    op = ApiOp(client, match)
    op.post("match_start")
    goal = op.goal(sport, 10, "Zé")["event"]
    denied = op._post(f"/api/ops/matches/{match.id}/events/{goal['id']}/edit",
                      {"type": "goal", "minute": 11, "team_id": sport.id, "payload": {"player": "Zé"}}, expect=403)
    assert denied["code"] == "permission_denied"
