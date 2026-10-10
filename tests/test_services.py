"""Caminho de escrita (matches/services.py): um jogo inteiro pelos serviços, com
gol anulado, cancelamentos, idempotência, relógio, reagendamento, mata-mata,
outbox, classificação na mesma transação, auditoria e rejeições sem gravar nada."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest

from competitions.models import StageCriterion, StandingZone
from core import timeutils
from matches import context, selectors, services
from matches.domain import DomainError, LineupPlayer, NewEvent
from matches.models import Match, MatchEvent, MatchLineup, MatchLineupPlayer, Tie
from observability.metrics import metrics
from observability.models import AuditLog
from realtime.models import Outbox
from standings import services as standings_services
from standings.models import Standing
from tests.factories import make_knockout, make_league, make_match, make_tie_matches

BRT = ZoneInfo("America/Sao_Paulo")
pytestmark = pytest.mark.django_db


class Op:
    """Operador de teste: lança pelos serviços com relógio controlado (`at`)."""

    def __init__(self, match, user, start: datetime | None = None):
        self.match_id = match.id
        self.user = user
        self.clock = start or timeutils.now() - timedelta(minutes=150)
        self.n = 0

    def _key(self, key):
        self.n += 1
        return key or f"k-{self.match_id}-{self.n}"

    def post(self, type_, *, after=1.0, key=None, confirm=False, source="operator", **fields):
        self.clock += timedelta(minutes=after)
        return services.post_event(
            self.match_id, self.user, NewEvent(type=type_, **fields),
            idempotency_key=self._key(key), confirm=confirm, source=source, at=self.clock,
        )

    def status(self, action, *, after=1.0, key=None, **kwargs):
        self.clock += timedelta(minutes=after)
        return services.change_status(self.match_id, self.user, action, idempotency_key=self._key(key), at=self.clock, **kwargs)

    def void(self, event_id, *, after=0.5, reason=""):
        self.clock += timedelta(minutes=after)
        return services.void_event(self.match_id, event_id, self.user, reason=reason, at=self.clock)

    def goal(self, team, minute, player="Fulano", **payload):
        return self.post("goal", team_id=team.id, minute=minute, payload={"player": player, **payload})

    def match(self):
        return Match.objects.get(pk=self.match_id)


def outbox_after(mark: int) -> list[Outbox]:
    return list(Outbox.objects.filter(id__gt=mark).order_by("id"))


def last_outbox_id() -> int:
    row = Outbox.objects.order_by("-id").first()
    return row.id if row else 0


@pytest.fixture
def league(db):
    return make_league(
        n_teams=4,
        team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"],
        zones=[{"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 2}],
    )


@pytest.fixture
def live_match(league):
    sport, nautico = league["teams"][0], league["teams"][1]
    return make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now() - timedelta(minutes=150), round=league["rounds"][0])


# --- Jogo inteiro ------------------------------------------------------------------------


def test_full_match_with_annulled_goal_cards_and_second_yellow(league, live_match, operator_user):
    sport, nautico = league["teams"][0], league["teams"][1]
    op = Op(live_match, operator_user)

    start = op.post("match_start")
    assert start.created and start.match.status == "live" and start.match.period == "first_half"
    assert start.event.minute == 0 and start.event.period == "first_half"

    g1 = op.goal(sport, 10, "Zé Roberto")
    assert (g1.match.home_score, g1.match.away_score) == (1, 0)
    g2 = op.goal(nautico, 20, "Kayo Lima")
    assert (g2.match.home_score, g2.match.away_score) == (1, 1)
    op.post("yellow_card", team_id=nautico.id, minute=25, payload={"player": "Thiago Freitas"})

    annul = op.post("goal_annulled", minute=22, annuls_event_id=g2.event.id, payload={"reason": "Impedimento"}, confirm=True)
    assert annul.event.team_id == nautico.id  # herdado do gol
    assert (annul.match.home_score, annul.match.away_score) == (1, 0)

    # Cancelar a anulação devolve o gol.
    restored = op.void(annul.event.id)
    assert restored.voided_ids == [annul.event.id] and not restored.already
    assert (restored.match.home_score, restored.match.away_score) == (1, 1)

    op.post("half_time")
    op.post("second_half_start", after=15)
    second = op.post("yellow_card", team_id=nautico.id, minute=60, payload={"player": "thiago freitas"})
    assert len(second.derived) == 1
    red = second.derived[0]
    assert red.type == "red_card" and red.source == "system"
    assert red.idempotency_key == f"{second.event.idempotency_key}:auto:1"
    assert red.payload == {"player": "thiago freitas", "reason": "second_yellow", "derived_from_sequence": second.event.sequence}
    assert red.sequence == second.event.sequence + 1 and red.created_by == operator_user

    op.goal(sport, 80, "Lucas Arcanjo")
    end = op.post("match_end", after=12)
    match = end.match
    assert match.status == "finished" and match.period is None and match.period_started_at is None
    assert (match.home_score, match.away_score) == (2, 1)
    assert match.finished_at == end.event.created_at
    # uma versão por escrita: 10 lançamentos + 1 cancelamento (o vermelho automático vem junto do amarelo)
    assert match.version == 11

    official = {row.team_id: row for row in Standing.objects.filter(group=league["group"], kind="official")}
    assert official[sport.id].points == 3 and official[sport.id].position == 1
    assert official[nautico.id].lost == 1 and official[nautico.id].yellow_cards == 2 and official[nautico.id].red_cards == 1

    detail = selectors.match_detail(match.id)["match"]
    assert [goal["player"] for goal in detail["goals"]] == ["Zé Roberto", "Kayo Lima", "Lucas Arcanjo"]
    assert detail["cards"] == {"home": {"yellow": 0, "red": 0}, "away": {"yellow": 2, "red": 1}}
    assert detail["winner"] == "home"
    # o lançamento cancelado (anulação) não aparece nas leituras
    assert annul.event.id not in {event["id"] for event in detail["events"]}


def test_version_increments_once_per_write(live_match, operator_user):
    op = Op(live_match, operator_user)
    versions = [op.post("match_start").match.version]
    versions.append(op.post("yellow_card", team_id=live_match.home_team_id, minute=3, payload={"player": "A"}).match.version)
    versions.append(op.post("yellow_card", team_id=live_match.home_team_id, minute=4, payload={"player": "A"}).match.version)
    assert versions == [1, 2, 3]
    assert op.match().version == 3


# --- Cancelamentos -----------------------------------------------------------------------


def test_void_cascade_goal_with_annulment_and_yellow_with_auto_red(league, live_match, operator_user):
    sport = league["teams"][0]
    op = Op(live_match, operator_user)
    op.post("match_start")
    goal = op.goal(sport, 5)
    annul = op.post("goal_annulled", minute=6, annuls_event_id=goal.event.id, payload={"reason": "Falta"})
    assert annul.match.home_score == 0

    outcome = op.void(goal.event.id)
    assert outcome.voided_ids == [goal.event.id, annul.event.id]
    rows = MatchEvent.objects.filter(id__in=outcome.voided_ids)
    assert all(row.voided_at is not None and row.voided_by == operator_user for row in rows)
    assert outcome.match.home_score == 0

    y1 = op.post("yellow_card", team_id=sport.id, minute=10, payload={"player": "B"})
    y2 = op.post("yellow_card", team_id=sport.id, minute=12, payload={"player": "B"})
    red = y2.derived[0]
    with pytest.raises(DomainError) as info:
        op.void(red.id)
    assert info.value.code == "void_derived_event"
    cascade = op.void(y2.event.id)
    assert cascade.voided_ids == [y2.event.id, red.id]
    # cancelar o 1º amarelo depois (só sobra ele): cai sozinho
    assert op.void(y1.event.id).voided_ids == [y1.event.id]


def test_old_card_inconsistency_does_not_block_unrelated_edit_or_void(league, live_match, operator_user):
    """Partida que a correção antiga deixou inconsistente (vermelho automático sem o 2º
    amarelo de origem; 2º amarelo sem vermelho): corrigir ou cancelar um gol não mexe nos
    cartões antigos; o vermelho que fica órfão por causa da correção cai e volta em `voided_ids`."""
    sport, nautico = league["teams"][0], league["teams"][1]
    op = Op(live_match, operator_user)
    op.post("match_start")
    first = op.post("yellow_card", team_id=sport.id, minute=10, payload={"player": "Alice"})
    op.post("yellow_card", team_id=sport.id, minute=12, payload={"player": "Alice"})
    op.post("yellow_card", team_id=nautico.id, minute=14, payload={"player": "Bia"})
    carla = op.post("yellow_card", team_id=nautico.id, minute=15, payload={"player": "Carla"})
    # gravado direto, como a correção antiga fazia (sem refazer o vermelho automático)
    MatchEvent.objects.filter(pk=first.event.id).update(payload={"player": "Bob"})
    MatchEvent.objects.filter(pk=carla.event.id).update(payload={"player": "Bia"})
    goal = op.goal(sport, 20, "Zé")

    def cards():
        rows = MatchEvent.objects.filter(match=live_match, type__in=["yellow_card", "red_card"]).order_by("sequence")
        return [(row.id, row.payload.get("player"), row.voided_at) for row in rows]

    before = cards()
    edited = services.edit_event(
        live_match.id, goal.event.id, operator_user, NewEvent(type="goal", minute=21, team_id=sport.id, payload={"player": "Zé"})
    )
    assert edited.event.minute == 21 and edited.voided_ids == []
    assert cards() == before
    assert op.void(goal.event.id).voided_ids == [goal.event.id]
    assert cards() == before

    dani = op.post("yellow_card", team_id=sport.id, minute=30, payload={"player": "Dani"})
    red = op.post("yellow_card", team_id=sport.id, minute=32, payload={"player": "Dani"}).derived[0]
    edited = services.edit_event(
        live_match.id, dani.event.id, operator_user, NewEvent(type="yellow_card", minute=30, team_id=sport.id, payload={"player": "Eva"})
    )
    assert edited.voided_ids == [red.id]
    assert MatchEvent.objects.get(pk=red.id).voided_at is not None
    assert cards()[:len(before)] == before  # os cartões antigos continuam como estavam


def test_void_of_the_culprit_still_works_when_events_no_longer_replay(league, live_match, operator_user):
    """Partida editada que deixou os eventos inconsistentes: o operador fica sem ações,
    e cancelar o lance culpado continua sendo a saída."""
    sport = league["teams"][0]
    op = Op(live_match, operator_user)
    op.post("match_start")
    goal = op.goal(sport, 10, "Zé")
    MatchEvent.objects.filter(pk=goal.event.id).update(period="second_half")
    assert op.void(goal.event.id).voided_ids == [goal.event.id]


def test_void_already_voided_is_idempotent(league, live_match, operator_user):
    op = Op(live_match, operator_user)
    op.post("match_start")
    y1 = op.post("yellow_card", team_id=live_match.home_team_id, minute=2, payload={"player": "C"})
    y2 = op.post("yellow_card", team_id=live_match.home_team_id, minute=3, payload={"player": "C"})
    first = op.void(y2.event.id)
    mark, version = last_outbox_id(), op.match().version
    again = op.void(y2.event.id)
    assert again.already and again.voided_ids == first.voided_ids
    assert outbox_after(mark) == [] and op.match().version == version
    derived_again = op.void(y2.derived[0].id)
    assert derived_again.already and derived_again.voided_ids[0] == y2.derived[0].id
    assert y1.event.id not in again.voided_ids


def test_void_unknown_event(live_match, operator_user):
    op = Op(live_match, operator_user)
    op.post("match_start")
    with pytest.raises(DomainError) as info:
        op.void(999999)
    assert info.value.code == "event_not_found"


# --- Idempotência --------------------------------------------------------------------------


def test_idempotent_replay_writes_nothing(league, live_match, operator_user):
    sport = league["teams"][0]
    op = Op(live_match, operator_user)
    op.post("match_start")
    op.post("yellow_card", team_id=sport.id, minute=3, payload={"player": "D"})
    first = op.post("yellow_card", key="dup-1", team_id=sport.id, minute=5, payload={"player": "D"})
    assert first.created and len(first.derived) == 1

    counts = (MatchEvent.objects.count(), Outbox.objects.count(), AuditLog.objects.count())
    version = op.match().version
    replay = op.post("yellow_card", key="dup-1", team_id=sport.id, minute=5, payload={"player": "D"})
    assert not replay.created and replay.event.id == first.event.id
    assert [row.id for row in replay.derived] == [first.derived[0].id]
    assert replay.match.version == version
    # mesma chave com outro corpo continua sendo replay do original
    other = op.post("goal", key="dup-1", team_id=sport.id, minute=9, payload={"player": "E"})
    assert not other.created and other.event.id == first.event.id and other.event.type == "yellow_card"
    assert (MatchEvent.objects.count(), Outbox.objects.count(), AuditLog.objects.count()) == counts

    status = op.status("suspend", key="dup-2")
    assert status.created
    again = op.status("suspend", key="dup-2")
    assert not again.created and again.event.id == status.event.id


def test_idempotency_key_and_source_validation(live_match, operator_user):
    with pytest.raises(services.InvalidInput) as info:
        services.post_event(live_match.id, operator_user, NewEvent(type="match_start"), idempotency_key="")
    assert info.value.field == "idempotency_key"
    with pytest.raises(services.InvalidInput):
        services.post_event(live_match.id, operator_user, NewEvent(type="match_start"), idempotency_key="x" * 65)
    with pytest.raises(services.InvalidInput) as info:
        services.post_event(live_match.id, operator_user, NewEvent(type="match_start"), idempotency_key="k", source="robot")
    assert info.value.field == "source"
    with pytest.raises(ValueError):
        services.post_event(live_match.id, None, NewEvent(type="match_start"), idempotency_key="k")
    with pytest.raises(Match.DoesNotExist):
        services.post_event(987654, operator_user, NewEvent(type="match_start"), idempotency_key="k")
    assert MatchEvent.objects.count() == 0


# --- Relógio --------------------------------------------------------------------------------


def test_period_started_at_discounts_closed_suspensions(live_match, operator_user):
    t0 = timeutils.now() - timedelta(minutes=40)
    op = Op(live_match, operator_user, start=t0 - timedelta(minutes=1))
    start = op.post("match_start")
    assert start.match.period_started_at == t0

    suspended = op.status("suspend", after=10, reason="Chuva")
    match = suspended.match
    assert match.status == "suspended" and match.period == "first_half"
    assert match.period_started_at == t0  # suspensão aberta não entra
    clock = selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=match.pk))["clock"]
    assert clock == {
        "running": False, "offset": 0, "regular_end": 45, "stoppage_announced": None,
        "paused_at": timeutils.iso_utc(t0 + timedelta(minutes=10)),
    }

    resumed = op.status("resume", after=5)
    assert resumed.match.status == "live"
    assert resumed.match.period_started_at == t0 + timedelta(minutes=5)

    op.status("suspend", after=3)
    again = op.status("resume", after=2)
    assert again.match.period_started_at == t0 + timedelta(minutes=7)

    op.post("stoppage_time", minute=45, payload={"minutes": 3})
    live = selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=live_match.pk))
    assert live["clock"] == {"running": True, "offset": 0, "regular_end": 45, "stoppage_announced": 3, "paused_at": None}

    op.post("half_time")
    second = op.post("second_half_start", after=15)
    assert second.match.period_started_at == second.event.created_at  # pausas zeram no período novo
    assert selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=live_match.pk))["clock"]["stoppage_announced"] is None


def test_finished_at_set_and_cleared_when_end_voided(live_match, operator_user):
    op = Op(live_match, operator_user)
    op.post("match_start")
    op.post("half_time")
    second = op.post("second_half_start", after=15)
    end = op.post("match_end", after=50)
    assert end.match.finished_at == end.event.created_at and end.match.status == "finished"

    outcome = op.void(end.event.id)
    match = outcome.match
    assert match.status == "live" and match.period == "second_half" and match.finished_at is None
    assert match.period_started_at == second.event.created_at
    assert Match.objects.get(pk=match.pk).finished_at is None


# --- Status e reagendamento -----------------------------------------------------------------


def test_reschedule_without_timezone_is_brasilia_and_void_restores_kickoff(live_match, operator_user):
    original = live_match.kickoff_at
    op = Op(live_match, operator_user)
    postponed = op.status("postpone", reason="Gramado")
    assert postponed.match.status == "postponed"

    first = op.status("reschedule", kickoff_at="2026-11-10T19:30")
    match = first.match
    assert match.status == "scheduled"
    assert match.kickoff_at == datetime(2026, 11, 10, 19, 30, tzinfo=BRT)
    assert first.event.payload["kickoff_at"] == "2026-11-10T22:30:00Z"
    assert first.event.payload["previous_kickoff_at"] == timeutils.iso_utc(original)

    second = op.status("reschedule", kickoff_at=datetime(2026, 11, 12, 16, 0, tzinfo=dt_timezone.utc))
    assert second.match.kickoff_at == datetime(2026, 11, 12, 16, 0, tzinfo=dt_timezone.utc)

    # cancelar o último reagendamento volta ao anterior; cancelar os dois volta ao original
    assert op.void(second.event.id).match.kickoff_at == datetime(2026, 11, 10, 19, 30, tzinfo=BRT)
    restored = op.void(first.event.id).match
    assert restored.kickoff_at == original and restored.status == "postponed"
    assert Match.objects.get(pk=live_match.pk).kickoff_at == original


def test_change_status_rejections(live_match, operator_user):
    op = Op(live_match, operator_user)
    with pytest.raises(DomainError) as info:
        op.status("explode")
    assert info.value.code == "invalid_status_action"
    with pytest.raises(DomainError) as info:
        op.status("reschedule", kickoff_at="2026-11-10")
    assert info.value.code == "invalid_payload"
    with pytest.raises(DomainError) as info:
        op.status("resume")
    assert info.value.code == "invalid_status_action"
    assert MatchEvent.objects.count() == 0
    cancelled = op.status("cancel", reason="Interdição")
    assert cancelled.match.status == "cancelled"


# --- Mata-mata --------------------------------------------------------------------------------


def test_two_leg_tie_with_extra_time_saves_winner_and_locks_first_leg(league, operator_user):
    ko = make_knockout(league["season"], legs=2, extra_time=True, team_a=league["teams"][0], team_b=league["teams"][1])
    tie, team_a, team_b = ko["tie"], ko["team_a"], ko["team_b"]
    leg1, leg2 = make_tie_matches(tie, kickoff=timeutils.now() - timedelta(days=7))
    op1 = Op(leg1, operator_user, start=timeutils.now() - timedelta(days=7))
    op1.post("match_start")
    g_leg1 = op1.goal(team_a, 10)
    op1.goal(team_b, 30)
    op1.goal(team_a, 40)
    op1.post("half_time")
    op1.post("second_half_start", after=15)
    end1 = op1.post("match_end", after=50)
    assert (end1.match.home_score, end1.match.away_score) == (2, 1)
    tie.refresh_from_db()
    assert tie.winner_team_id is None and tie.decided_by == ""

    op2 = Op(leg2, operator_user)
    op2.post("match_start")
    # volta começou: a ida não pode mais ser corrigida
    with pytest.raises(DomainError) as info:
        op1.void(g_leg1.event.id)
    assert info.value.code == "tie_leg_locked"
    op2.goal(team_b, 20)  # mandante da volta é o time B; agregado 2 × 2
    op2.post("half_time")
    op2.post("second_half_start", after=15)
    with pytest.raises(DomainError) as info:
        op2.post("match_end", after=50)
    assert info.value.code == "tie_level_requires_extra_time"
    op2.post("extra_time_start", after=5)
    op2.goal(team_b, 100)
    op2.post("extra_half_time", after=6)
    op2.post("extra_second_half_start", after=2)
    end2 = op2.post("match_end", after=15)
    assert end2.match.status == "finished"
    tie.refresh_from_db()
    assert tie.winner_team_id == team_b.id and tie.decided_by == "extra_time"

    payload = selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=leg2.pk))
    assert payload["tie"]["aggregate"] == {"team_a": 2, "team_b": 3}
    assert payload["tie"]["complete"] and payload["tie"]["leg"] == 2
    assert payload["tie"]["decided_by_label"] == "na prorrogação"

    # cancelar o fim da volta limpa o vencedor
    op2.void(end2.event.id)
    tie.refresh_from_db()
    assert tie.winner_team_id is None and tie.decided_by == ""


def test_single_leg_tie_straight_to_penalties(league, operator_user):
    ko = make_knockout(league["season"], legs=1, extra_time=False, team_a=league["teams"][2], team_b=league["teams"][3])
    tie, team_a, team_b = ko["tie"], ko["team_a"], ko["team_b"]
    (match,) = make_tie_matches(tie, kickoff=timeutils.now() - timedelta(hours=2))
    op = Op(match, operator_user)
    op.post("match_start")
    op.post("half_time")
    op.post("second_half_start", after=15)
    with pytest.raises(DomainError) as info:
        op.post("match_end", after=50)
    assert info.value.code == "tie_level_requires_penalties"
    with pytest.raises(DomainError) as info:
        op.post("extra_time_start")
    assert info.value.code == "extra_time_not_allowed"
    pens = op.post("penalties_start")
    assert (pens.match.home_penalties, pens.match.away_penalties) == (0, 0)
    for team, scored in [(team_a, True), (team_b, True), (team_a, True), (team_b, False), (team_a, True), (team_b, True), (team_a, False), (team_b, False)]:
        op.post("shootout_kick", team_id=team.id, payload={"player": f"Cobrador {team.id}", "scored": scored})
    end = op.post("match_end")
    assert (end.match.home_score, end.match.away_score) == (0, 0)
    assert (end.match.home_penalties, end.match.away_penalties) == (3, 2)
    tie.refresh_from_db()
    assert tie.winner_team_id == team_a.id and tie.decided_by == "penalties"
    out = selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=match.pk))
    assert out["winner"] == "home" and out["goals"] == []


def test_first_leg_message_published_when_tie_result_changes(league, operator_user):
    ko = make_knockout(league["season"], legs=2, extra_time=False)
    tie = ko["tie"]
    leg1, leg2 = make_tie_matches(tie, kickoff=timeutils.now() - timedelta(days=7))
    op1 = Op(leg1, operator_user)
    op1.post("match_start")
    op1.goal(ko["team_a"], 5)
    op1.post("half_time")
    op1.post("second_half_start", after=15)
    op1.post("match_end", after=50)
    op2 = Op(leg2, operator_user)
    op2.post("match_start")
    op2.post("half_time")
    op2.post("second_half_start", after=15)
    leg1_version = Match.objects.get(pk=leg1.pk).version
    mark = last_outbox_id()
    op2.post("match_end", after=50)
    messages = outbox_after(mark)
    matches = [row.payload["match"]["id"] for row in messages if row.topic == "match"]
    assert matches == [leg2.id, leg1.id]
    assert Match.objects.get(pk=leg1.pk).version == leg1_version + 1
    leg1_msg = next(row.payload for row in messages if row.topic == "match" and row.payload["match"]["id"] == leg1.id)
    assert leg1_msg["match"]["tie"]["winner_team_id"] == ko["team_a"].id
    assert Tie.objects.get(pk=tie.pk).decided_by == "aggregate"


# --- Outbox ---------------------------------------------------------------------------------


def test_outbox_messages_per_write(league, live_match, operator_user):
    sport = league["teams"][0]
    op = Op(live_match, operator_user)
    mark = last_outbox_id()
    op.post("match_start")
    msgs = outbox_after(mark)
    assert [row.topic for row in msgs] == ["match", "standings"]
    match_msg, standings_msg = msgs[0].payload, msgs[1].payload
    assert match_msg["stage_id"] == league["stage"].id
    assert match_msg["competition_id"] == league["competition"].id
    assert match_msg["match"]["id"] == live_match.id and "events" in match_msg["match"] and "lineups" in match_msg["match"]
    assert standings_msg["stage_id"] == league["stage"].id
    assert standings_msg["standings"]["kind"] == "live"
    rows = standings_msg["standings"]["groups"][0]["rows"]
    assert {row["team"]["id"] for row in rows if row["playing"]} == {live_match.home_team_id, live_match.away_team_id}

    mark = last_outbox_id()
    goal = op.goal(sport, 9, "Zé")
    msgs = outbox_after(mark)
    assert [row.topic for row in msgs] == ["match", "standings", "goals"]
    assert [row.id for row in msgs] == sorted(row.id for row in msgs)
    goals = msgs[2].payload
    assert goals["date"] == timeutils.local_today().isoformat()
    assert goals["changes"] == [{"kind": "added", "reason": None, "goal": goals["changes"][0]["goal"]}]
    added = goals["changes"][0]["goal"]
    assert added["event_id"] == goal.event.id and added["score_after"] == {"home": 1, "away": 0}
    assert added["match"]["competition"]["slug"] == league["competition"].slug and added["team"]["id"] == sport.id
    assert [item["event_id"] for item in goals["latest_goals"]] == [goal.event.id]

    # lance que não mexe em gol, placar, status nem cartão: sem goals e sem standings
    # (a tabela não foi recalculada: a mensagem seria idêntica à anterior)
    mark = last_outbox_id()
    op.post("substitution", team_id=sport.id, minute=20, payload={"player_out": "A", "player_in": "B"})
    assert [row.topic for row in outbox_after(mark)] == ["match"]


def test_standings_message_only_when_the_table_was_recomputed(league, live_match, operator_user):
    """`standings` sai só quando status, placar ou cartão mudaram (a tabela foi recalculada);
    lances que não mexem na tabela não reenviam a mesma classificação para todo cliente."""
    sport = league["teams"][0]
    op = Op(live_match, operator_user)
    op.post("match_start")

    def topics(action):
        mark = last_outbox_id()
        result = action()
        return [row.topic for row in outbox_after(mark)], result

    got, yellow = topics(lambda: op.post("yellow_card", team_id=sport.id, minute=10, payload={"player": "Zé"}))
    assert got == ["match", "standings"]
    got, sub = topics(lambda: op.post("substitution", team_id=sport.id, minute=20, payload={"player_out": "A", "player_in": "B"}))
    assert got == ["match"]
    got, _ = topics(lambda: op.post("stoppage_time", minute=45, payload={"minutes": 3}))
    assert got == ["match"]
    got, _ = topics(lambda: op.void(sub.event.id))
    assert got == ["match"]
    got, _ = topics(lambda: op.void(yellow.event.id))
    assert got == ["match", "standings"]
    got, _ = topics(lambda: op.post("half_time"))  # só o período muda (status segue "live"): tabela igual
    assert got == ["match"]
    got, _ = topics(lambda: op.post("second_half_start"))
    assert got == ["match"]
    got, _ = topics(lambda: op.status("suspend"))  # mudança de status: recalcula e publica
    assert got == ["match", "standings"]
    got, _ = topics(lambda: op.status("resume"))
    assert got == ["match", "standings"]


def test_admin_kickoff_change_on_or_off_today_publishes_latest_goals(league, operator_user):
    """Início editado no admin que tira (ou põe) de hoje uma partida com gols válidos:
    mensagem `goals` com `changes` vazio e `latest_goals` refeito."""
    sport, nautico = league["teams"][:2]
    today = timeutils.local_today()
    noon = timeutils.day_bounds(today)[0] + timedelta(hours=15)  # 12:00 de Brasília, hoje
    match = make_match(league["stage"], sport, nautico, kickoff_at=noon, round=league["rounds"][0])
    op = Op(match, operator_user, start=noon)
    op.post("match_start")
    goal = op.goal(sport, 5, "Zé")

    def edit(new_kickoff, previous):
        Match.objects.filter(pk=match.pk).update(kickoff_at=new_kickoff)
        mark = last_outbox_id()
        services.on_match_edited(op.match(), previous, user=operator_user)
        return outbox_after(mark)

    msgs = edit(noon + timedelta(days=2), {"kickoff_at": noon})  # sai de hoje
    assert [row.topic for row in msgs] == ["match", "goals"]
    assert msgs[-1].payload == {"date": today.isoformat(), "changes": [], "latest_goals": []}

    msgs = edit(noon, {"kickoff_at": noon + timedelta(days=2)})  # volta para hoje
    assert [row.topic for row in msgs] == ["match", "goals"]
    assert msgs[-1].payload["changes"] == []
    assert [item["event_id"] for item in msgs[-1].payload["latest_goals"]] == [goal.event.id]

    msgs = edit(noon + timedelta(hours=1), {"kickoff_at": noon})  # mesmo dia: a lista não muda
    assert [row.topic for row in msgs] == ["match"]

    # só os nomes (sem o valor antigo): publica a lista, por garantia
    mark = last_outbox_id()
    services.on_match_edited(op.match(), ["kickoff_at"])
    assert [row.topic for row in outbox_after(mark)] == ["match", "goals"]

    # sem gols válidos, nunca
    op.void(goal.event.id)
    msgs = edit(noon + timedelta(days=3), {"kickoff_at": noon + timedelta(hours=1)})
    assert [row.topic for row in msgs] == ["match"]


@pytest.mark.parametrize(
    "kickoff_shift, status, finished_shift, expected",
    [
        (timedelta(hours=0), "scheduled", None, True),  # começa hoje
        (timedelta(days=1), "scheduled", None, False),  # amanhã
        (timedelta(hours=-3), "live", None, True),  # véspera, ao vivo (hoje)
        (timedelta(hours=-3), "finished", timedelta(minutes=-30), True),  # terminou depois da meia-noite, < 2 h
        (timedelta(hours=-3), "finished", timedelta(hours=-3), False),  # terminou antes da meia-noite
        (timedelta(days=-2), "live", None, False),  # antevéspera
    ],
)
def test_on_day_mirrors_day_matches_query(league, kickoff_shift, status, finished_shift, expected):
    today = timeutils.local_today()
    midnight = timeutils.day_bounds(today)[0]
    now = midnight + timedelta(hours=1)  # 01:00 de Brasília
    kickoff = (midnight + timedelta(hours=1, minutes=30)) if kickoff_shift == timedelta(0) else midnight + kickoff_shift
    match = make_match(league["stage"], *league["teams"][:2], kickoff_at=kickoff)
    finished = now + finished_shift if finished_shift is not None else None
    Match.objects.filter(pk=match.pk).update(status=status, finished_at=finished)
    match.refresh_from_db()
    in_query = selectors.day_matches_query(today, now).filter(pk=match.pk).exists()
    assert in_query is expected
    assert selectors.on_day(match, today, now) is expected


def test_goals_message_removed_when_goal_annulled_and_restored_when_annulment_voided(league, live_match, operator_user):
    nautico = league["teams"][1]
    op = Op(live_match, operator_user)
    op.post("match_start")
    goal = op.goal(nautico, 30, "Kayo Lima")
    mark = last_outbox_id()
    annul = op.post("goal_annulled", minute=31, annuls_event_id=goal.event.id, payload={"reason": "VAR"})
    goals_msgs = [row.payload for row in outbox_after(mark) if row.topic == "goals"]
    assert len(goals_msgs) == 1
    (change,) = goals_msgs[0]["changes"]
    assert change["kind"] == "removed" and change["reason"] == "annulled"
    assert change["goal"]["event_id"] == goal.event.id
    assert change["goal"]["score_after"] == {"home": 0, "away": 1}  # placar do gol que saiu
    assert goals_msgs[0]["latest_goals"] == []

    mark = last_outbox_id()
    op.void(annul.event.id)
    (msg,) = [row.payload for row in outbox_after(mark) if row.topic == "goals"]
    assert msg["changes"] == [{"kind": "restored", "reason": "unvoided", "goal": msg["changes"][0]["goal"]}]
    assert [item["event_id"] for item in msg["latest_goals"]] == [goal.event.id]

    mark = last_outbox_id()
    op.void(goal.event.id)
    (msg,) = [row.payload for row in outbox_after(mark) if row.topic == "goals"]
    assert [(c["kind"], c["reason"]) for c in msg["changes"]] == [("removed", "voided")]


def test_knockout_match_has_no_standings_message(league, operator_user):
    ko = make_knockout(league["season"], legs=1, extra_time=True)
    (match,) = make_tie_matches(ko["tie"], kickoff=timeutils.now() - timedelta(hours=1))
    mark = last_outbox_id()
    Op(match, operator_user).post("match_start")
    assert [row.topic for row in outbox_after(mark)] == ["match"]


# --- Classificação --------------------------------------------------------------------------


def test_standings_recomputed_in_transaction_and_annulled_goal_undoes_live_table(league, live_match, operator_user):
    sport, nautico = league["teams"][0], league["teams"][1]
    op = Op(live_match, operator_user)
    op.post("match_start")

    def live_row(team):
        return Standing.objects.get(group=league["group"], kind="live", team=team)

    assert live_row(sport).points == 1 and live_row(sport).played == 1  # 0 × 0 ao vivo
    assert Standing.objects.get(group=league["group"], kind="official", team=sport).played == 0
    goal = op.goal(nautico, 12)
    assert live_row(nautico).points == 3 and live_row(nautico).position == 1
    assert live_row(sport).points == 0
    op.post("goal_annulled", minute=14, annuls_event_id=goal.event.id, payload={"reason": "Mão"})
    assert live_row(nautico).points == 1 and live_row(sport).points == 1
    table = standings_services.stage_standings(league["stage"])
    nautico_row = next(row for row in table["groups"][0]["rows"] if row["team"]["id"] == nautico.id)
    assert nautico_row["goals_for"] == 0 and nautico_row["playing"]


def test_stage_criterion_and_zone_color_change_standings_output(league, operator_user):
    stage = league["stage"]
    before = standings_services.stage_standings(stage)
    assert [item["key"] for item in before["criteria"]] == ["points", "wins", "goal_difference", "goals_for", "head_to_head"]
    assert before["legend"] == [{"name": "Classificados", "color": "#1B7F3B", "from": 1, "to": 2}]

    StageCriterion.objects.filter(stage=stage, key="head_to_head").update(key="fewer_red_cards")
    StandingZone.objects.filter(stage=stage).update(color="#12306B")
    mark = last_outbox_id()
    standings_services.on_stage_rules_changed(stage, recalc=True)
    (msg,) = outbox_after(mark)
    assert msg.topic == "standings" and msg.payload["stage_id"] == stage.id
    after = msg.payload["standings"]
    assert after["criteria"][-1] == {"key": "fewer_red_cards", "label": "Menos cartões vermelhos"}
    assert after["legend"][0]["color"] == "#12306B"
    assert after["groups"][0]["rows"][0]["zone"] == {"name": "Classificados", "color": "#12306B"}


def test_invalid_saved_criteria_fall_back_to_defaults_without_breaking_post(league, live_match, operator_user, caplog):
    StageCriterion.objects.filter(stage=league["stage"], key="head_to_head").update(key="best_vibes")
    op = Op(live_match, operator_user)
    with caplog.at_level("WARNING", logger="fdr.standings"):
        result = op.post("match_start")
    assert result.created
    assert any("critérios inválidos" in record.getMessage() for record in caplog.records)
    table = standings_services.stage_standings(league["stage"])
    assert [item["key"] for item in table["criteria"]] == list(standings_services.DEFAULT_CRITERIA)


# --- Auditoria, métricas e rejeições ---------------------------------------------------------


def test_audit_rows_written_for_each_action(league, live_match, operator_user):
    op = Op(live_match, operator_user)
    start = op.post("match_start", key="audit-start", source="script")
    yellow = op.post("yellow_card", team_id=league["teams"][0].id, minute=4, payload={"player": "Z"})
    op.status("suspend", key="audit-suspend", reason="Briga")
    op.void(yellow.event.id, reason="Lançado no time errado")

    logs = list(AuditLog.objects.filter(match_id=live_match.id).order_by("id"))
    assert [log.action for log in logs] == ["event.create", "event.create", "match.status", "event.void"]
    assert all(log.actor == operator_user and log.actor_username == "operador" for log in logs)
    assert logs[0].data["type"] == "match_start" and logs[0].data["key"] == "audit-start" and logs[0].data["source"] == "script"
    assert logs[0].object_id == str(start.event.id)
    assert logs[1].data["minute"] == 4
    assert logs[2].data["action"] == "suspend" and logs[2].data["reason"] == "Briga"
    assert logs[3].data["voided_ids"] == [yellow.event.id] and logs[3].data["reason"] == "Lançado no time errado"


def test_domain_error_writes_nothing_and_counts_rejection(league, live_match, operator_user):
    op = Op(live_match, operator_user)
    before_metric = metrics.value("fdr_domain_rejections_total", code="match_not_live")
    with pytest.raises(DomainError) as info:
        op.goal(league["teams"][0], 10)
    assert info.value.code == "match_not_live"
    assert metrics.value("fdr_domain_rejections_total", code="match_not_live") == before_metric + 1
    assert (MatchEvent.objects.count(), Outbox.objects.count(), AuditLog.objects.count()) == (0, 0, 0)
    assert op.match().version == 0

    op.post("match_start")
    op.post("goal", team_id=league["teams"][0].id, minute=10, payload={"player": "X"})
    counts = (MatchEvent.objects.count(), Outbox.objects.count(), AuditLog.objects.count(), op.match().version)
    with pytest.raises(DomainError) as info:
        op.post("goal", team_id=league["teams"][0].id, minute=3, payload={"player": "Y"})  # minuto menor: aviso
    assert info.value.code == "confirmation_required" and info.value.warnings
    assert (MatchEvent.objects.count(), Outbox.objects.count(), AuditLog.objects.count(), op.match().version) == counts
    confirmed = op.post("goal", team_id=league["teams"][0].id, minute=3, payload={"player": "Y"}, confirm=True)
    assert [warning.code for warning in confirmed.warnings] == ["minute_decreasing"]


def test_metrics_counted(league, live_match, operator_user):
    op = Op(live_match, operator_user)
    posted = metrics.value("fdr_events_posted_total", type="yellow_card", source="operator")
    auto = metrics.value("fdr_events_posted_total", type="red_card", source="system")
    status = metrics.value("fdr_status_changes_total", action="suspend")
    voided = metrics.value("fdr_events_voided_total")
    op.post("match_start")
    op.post("yellow_card", team_id=live_match.home_team_id, minute=2, payload={"player": "M"})
    second = op.post("yellow_card", team_id=live_match.home_team_id, minute=3, payload={"player": "M"})
    op.void(second.event.id)
    op.status("suspend")
    assert metrics.value("fdr_events_posted_total", type="yellow_card", source="operator") == posted + 2
    assert metrics.value("fdr_events_posted_total", type="red_card", source="system") == auto + 1
    assert metrics.value("fdr_status_changes_total", action="suspend") == status + 1
    assert metrics.value("fdr_events_voided_total") == voided + 2


# --- Escalação ----------------------------------------------------------------------------------


def test_lineup_validation_warns_and_completes_player(league, live_match, operator_user):
    sport = league["teams"][0]
    lineup = MatchLineup.objects.create(match=live_match, team=sport, formation="4-3-3")
    MatchLineupPlayer.objects.create(lineup=lineup, name="Zé Roberto", number=10, starter=True)
    MatchLineupPlayer.objects.create(lineup=lineup, name="Lucas Arcanjo", number=14, starter=False)
    op = Op(live_match, operator_user)
    op.post("match_start")
    goal = op.goal(sport, 5, "ze roberto")
    assert goal.event.payload["player"] == "Zé Roberto"  # nome canônico da escalação
    with pytest.raises(DomainError) as info:
        op.goal(sport, 7, "Lucas Arcanjo")
    assert info.value.code == "confirmation_required"
    assert [warning.code for warning in info.value.warnings] == ["player_not_on_field"]
    # Jogador não tem cadastro: id de jogador é recusado antes do domínio (400 na API).
    with pytest.raises(services.InvalidInput) as invalid:
        op.post("goal", team_id=sport.id, minute=8, player_id=987654, payload={"player": "Fantasma"}, confirm=True)
    assert invalid.value.field == "player_id"
    with pytest.raises(services.InvalidInput) as invalid:
        op.post("substitution", team_id=sport.id, minute=9, payload={"player_out": "Zé Roberto", "player_in": "Lucas Arcanjo", "player_in_id": 3})
    assert invalid.value.field == "payload.player_in_id"
    assert context.lineups_for(live_match)[sport.id][0] == LineupPlayer(name="Zé Roberto", starter=True, player_id=None, number=10)


# --- Admin ----------------------------------------------------------------------------------------


def test_on_match_edited_recomputes_groups_and_publishes(league, live_match, operator_user):
    op = Op(live_match, operator_user)
    op.post("match_start")
    op.goal(league["teams"][0], 3)
    version = op.match().version
    other = make_league(n_teams=2)
    old_group = live_match.group
    # muda a partida para outro grupo (de outra fase) no admin
    Match.objects.filter(pk=live_match.pk).update(stage=other["stage"], group=other["group"], round=other["rounds"][0])
    from competitions.models import GroupTeam

    GroupTeam.objects.create(group=other["group"], team=league["teams"][0])
    GroupTeam.objects.create(group=other["group"], team=league["teams"][1])
    mark = last_outbox_id()
    services.on_match_edited(op.match(), {"group": old_group, "stage": league["stage"]}, user=operator_user)
    assert op.match().version == version + 1
    topics = [row.topic for row in outbox_after(mark)]
    assert topics[0] == "match" and topics.count("standings") == 2
    assert not Standing.objects.filter(group=old_group, kind="live", played__gt=0).exists()
    assert Standing.objects.get(group=other["group"], kind="live", team=league["teams"][0]).points == 3
    assert AuditLog.objects.filter(action="match.edit", match_id=live_match.id).exists()

    mark = last_outbox_id()
    services.on_match_edited(op.match(), ["venue"])
    assert [row.topic for row in outbox_after(mark)] == ["match"]


def test_publish_match_enqueues_detail(live_match):
    mark = last_outbox_id()
    row = services.publish_match(live_match)
    assert row.topic == "match" and row.id > mark
    assert row.payload["match"]["id"] == live_match.id and row.payload["match"]["events"] == []


def test_operator_clock_stop_start_and_set_minute(live_match, operator_user):
    t0 = timeutils.now() - timedelta(minutes=40)
    op = Op(live_match, operator_user, start=t0 - timedelta(minutes=1))
    assert op.post("match_start").match.period_started_at == t0

    stopped = op.post("clock_adjust", after=10, payload={"action": "stop"}).match
    assert stopped.status == "live" and stopped.clock_paused_at == t0 + timedelta(minutes=10)
    clock = selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=stopped.pk))["clock"]
    assert clock["running"] is False and clock["paused_at"] == timeutils.iso_utc(t0 + timedelta(minutes=10))

    started = op.post("clock_adjust", after=4, payload={"action": "start"}).match
    assert started.clock_paused_at is None and started.period_started_at == t0 + timedelta(minutes=4)

    # Início lançado com atraso: o operador acerta o relógio para mostrar 20' agora.
    fixed = op.post("clock_adjust", after=1, payload={"action": "set", "minute": 20}).match
    now = t0 + timedelta(minutes=15)
    assert fixed.period_started_at == now - timedelta(minutes=19)
    # 0 no 1º tempo = início do período (o relógio recomeça do zero agora)
    zero = op.post("clock_adjust", after=1, payload={"action": "set", "minute": 0}).match
    assert zero.period_started_at == now + timedelta(minutes=1)

    # Parado, acertar o minuto põe o relógio para correr de novo a partir dele.
    op.post("clock_adjust", after=2, payload={"action": "stop"})
    resumed = op.post("clock_adjust", after=3, payload={"action": "set", "minute": 30}).match
    later = t0 + timedelta(minutes=21)
    assert resumed.clock_paused_at is None and resumed.period_started_at == later - timedelta(minutes=29)
    clock = selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=resumed.pk))["clock"]
    assert clock["running"] is True and clock["paused_at"] is None


def test_set_partial_info_publishes_and_audits(live_match, operator_user):
    from observability.models import AuditLog

    op = Op(live_match, operator_user, start=timeutils.now() - timedelta(minutes=30))
    op.post("match_start")
    version = Match.objects.get(pk=live_match.pk).version
    mark = Outbox.objects.order_by("-id").values_list("id", flat=True).first() or 0
    match = services.set_partial_info(live_match.pk, operator_user, True)
    assert match.partial_info is True and match.version == version + 1
    row = Outbox.objects.filter(id__gt=mark, topic="match").get()
    assert row.payload["match"]["partial_info"] is True and row.payload["match"]["clock"] is None
    assert AuditLog.objects.get(action="match.edit").data == {"fields": ["partial_info"]}
    services.set_partial_info(live_match.pk, operator_user, True)  # sem mudança: nada sai
    assert not Outbox.objects.filter(id__gt=row.id).exists()


def test_partial_info_match_has_no_clock(live_match, operator_user):
    op = Op(live_match, operator_user, start=timeutils.now() - timedelta(minutes=30))
    op.post("match_start")
    Match.objects.filter(pk=live_match.pk).update(partial_info=True)
    data = selectors.serialize_match(Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=live_match.pk))
    assert data["partial_info"] is True and data["clock"] is None
