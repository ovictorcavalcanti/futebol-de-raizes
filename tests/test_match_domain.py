"""Testes das regras da partida em funções puras (matches/domain.py), sem banco.

Cada lançamento passa por `apply_event` e o simulador confere, a cada passo, que o
estado incremental é igual ao refeito do zero por `derive_state`.
"""

from __future__ import annotations

import ast
import random
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from matches.domain import (
    CATALOG,
    GOAL_ORIGIN_LABELS,
    DomainError,
    Event,
    EventType,
    LineupPlayer,
    MatchContext,
    MatchState,
    NewEvent,
    Period,
    Status,
    StatusAction,
    annulled_goal_ids,
    apply_event,
    available_actions,
    check_void,
    derive_state,
    event_icon,
    format_minute,
    minute_mode,
    player_key,
    score,
    status_action_event,
    valid_goals,
    visible_events,
)

SPORT, NAUTICO = 1, 2
CTX = MatchContext(SPORT, NAUTICO)


# --- Apoio -----------------------------------------------------------------------


class Sim:
    """Partida em memória: lança pelo domínio e guarda os eventos como o banco."""

    def __init__(self, ctx: MatchContext = CTX):
        self.ctx = ctx
        self.events: list[Event] = []
        self.state = MatchState()
        self.last = None
        self._ids = 100

    def post(self, type_: str, *, confirm: bool = False, **fields) -> Event:
        result = apply_event(self.state, self.events, NewEvent(type=type_, **fields), self.ctx, confirm=confirm)
        stored = []
        for event in (result.event, *result.derived):
            self._ids += 1
            stored.append(replace(event, id=self._ids))
        self.events.extend(stored)
        self.state = result.state
        self.last = result
        assert derive_state(self.events, self.ctx) == self.state
        return stored[0]

    def status(self, action: str, **kwargs) -> Event:
        new = status_action_event(action, **kwargs)
        return self.post(new.type, payload=new.payload)

    def void(self, event_id: int):
        result = check_void(self.events, event_id, self.ctx)
        dropped = set(result.voided_ids)
        self.events = [replace(event, voided=True) if event.id in dropped else event for event in self.events]
        self.state = result.state
        assert derive_state(self.events, self.ctx) == self.state
        return result

    # atalhos
    def goal(self, team: int, player: str, minute: int, stoppage: int | None = None, **payload) -> Event:
        return self.post(EventType.GOAL, team_id=team, minute=minute, stoppage=stoppage, payload={"player": player, **payload})

    def card(self, kind: str, team: int, player: str, minute: int | None, *, confirm: bool = False) -> Event:
        return self.post(kind, team_id=team, minute=minute, payload={"player": player}, confirm=confirm)

    def sub(self, team: int, out: str, into: str, minute: int | None, *, confirm: bool = False) -> Event:
        payload = {"player_out": out, "player_in": into}
        return self.post(EventType.SUBSTITUTION, team_id=team, minute=minute, payload=payload, confirm=confirm)

    def to_second_half(self) -> Sim:
        self.post(EventType.MATCH_START)
        self.post(EventType.HALF_TIME)
        self.post(EventType.SECOND_HALF_START)
        return self

    def by_id(self, event_id: int) -> Event:
        return next(event for event in self.events if event.id == event_id)


def live_sim(ctx: MatchContext = CTX) -> Sim:
    sim = Sim(ctx)
    sim.post(EventType.MATCH_START)
    return sim


def rejects(code: str, sim: Sim, type_: str, **fields) -> DomainError:
    before = (list(sim.events), sim.state)
    with pytest.raises(DomainError) as info:
        sim.post(type_, **fields)
    assert info.value.code == code, info.value.message
    assert (sim.events, sim.state) == before  # nada muda quando há erro
    return info.value


def warning_codes(error_or_result) -> list[str]:
    return [warning.code for warning in error_or_result.warnings]


# --- Funções auxiliares puras -------------------------------------------------------


def test_domain_module_does_not_import_django():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "matches" / "domain.py").read_text(encoding="utf-8"))
    modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    modules |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name.split(".")[0] == "django" for name in modules)


def test_player_key_by_id_or_normalized_name():
    assert player_key(1, 77, "Qualquer") == "id:77"
    assert player_key(1, None, "  José   Ferreira ") == "t1:jose ferreira"
    assert player_key(1, None, "JOSÉ FERREIRA") == player_key(1, None, "jose ferreira")
    assert player_key(2, None, "Zé") != player_key(1, None, "Zé")
    with pytest.raises(ValueError):
        player_key(1, None, "   ")


def test_format_minute():
    assert format_minute(45, 2) == "45+2'"
    assert format_minute(90) == "90'"
    assert format_minute(90, 0) == "90'"
    assert format_minute(None) == ""


def test_visible_events_drop_voided_and_sort_by_sequence():
    events = [Event(3, "goal"), Event(1, "match_start"), Event(2, "goal", voided=True)]
    assert [event.sequence for event in visible_events(events)] == [1, 3]


def test_valid_goals_and_score_ignore_annulled_voided_and_shootout():
    events = [
        Event(1, "match_start", id=1),
        Event(2, "goal", team_id=SPORT, id=2),
        Event(3, "goal", team_id=NAUTICO, id=3),
        Event(4, "goal_annulled", team_id=NAUTICO, annuls_event_id=3, id=4),
        Event(5, "goal", team_id=NAUTICO, id=5, voided=True),
        Event(6, "goal_annulled", team_id=SPORT, annuls_event_id=2, id=6, voided=True),
        Event(7, "shootout_kick", team_id=SPORT, payload={"scored": True}, id=7),
    ]
    assert [goal.id for goal in valid_goals(events)] == [2]
    assert annulled_goal_ids(events) == {3}
    assert score(events, CTX) == (1, 0)


# --- Catálogo -----------------------------------------------------------------------

ICONS = {
    "ball", "ball-penalty", "ball-own", "ball-x", "whistle", "var", "sub", "card-yellow", "card-red",
    "card-second-yellow", "clock", "flag", "calendar", "pause", "play", "x-circle", "target",
}


def test_catalog_has_one_spec_per_event_type():
    assert set(CATALOG) == {event_type.value for event_type in EventType}
    for event_type, spec in CATALOG.items():
        assert spec.type == event_type
        assert spec.label and spec.icon in ICONS
        assert spec.kind in ("structural", "game", "status")
        assert spec.minute in ("required", "optional", "none")
        for field_spec in spec.fields:
            assert field_spec.name in ("team_id", "annuls_event_id") or field_spec.name.startswith("payload.")


def test_catalog_labels_kinds_and_public_flag():
    labels = {
        "match_start": "Início de jogo", "half_time": "Fim do 1º tempo", "second_half_start": "Início do 2º tempo",
        "extra_time_start": "Início da prorrogação", "penalties_start": "Início dos pênaltis", "match_end": "Fim de jogo",
        "goal": "Gol", "goal_annulled": "Gol anulado", "penalty_awarded": "Pênalti marcado",
        "penalty_missed": "Pênalti perdido", "var_review": "Revisão do VAR", "substitution": "Substituição",
        "yellow_card": "Cartão amarelo", "red_card": "Cartão vermelho", "stoppage_time": "Acréscimos",
        "shootout_kick": "Cobrança de pênalti", "delayed": "Atrasado", "postponed": "Adiado", "suspended": "Suspenso",
        "resumed": "Retomado", "rescheduled": "Reagendado", "cancelled": "Cancelado",
    }
    assert {key: spec.label for key, spec in CATALOG.items()} == labels
    hidden = {key for key, spec in CATALOG.items() if not spec.public}
    assert hidden == {"goal_annulled", "delayed", "postponed", "suspended", "resumed", "rescheduled", "cancelled"}
    assert {key for key, spec in CATALOG.items() if spec.kind == "status"} == hidden - {"goal_annulled"}
    assert CATALOG["match_start"].minute == "optional" and CATALOG["goal"].minute == "required"
    assert CATALOG["postponed"].minute == "none"
    origin = next(f for f in CATALOG["goal"].fields if f.name == "payload.origin")
    assert dict(origin.choices) == {"open_play": "Jogada", "penalty": "Pênalti", "own_goal": "Contra"}
    assert set(GOAL_ORIGIN_LABELS.values()) == {"Jogada", "Pênalti", "Contra"}


def test_catalog_periods():
    play = {"first_half", "second_half", "extra_time"}
    for event_type in ("goal", "penalty_awarded", "penalty_missed", "var_review", "stoppage_time"):
        assert CATALOG[event_type].periods == play
    for event_type in ("goal_annulled", "substitution"):
        assert CATALOG[event_type].periods == play | {"half_time"}
    for event_type in ("yellow_card", "red_card"):
        assert CATALOG[event_type].periods == {period.value for period in Period}
    assert CATALOG["shootout_kick"].periods == {"penalties"}


def test_event_icon_variants():
    assert event_icon("goal", {"origin": "open_play"}) == "ball"
    assert event_icon("goal", {"origin": "penalty"}) == "ball-penalty"
    assert event_icon("goal", {"origin": "own_goal"}) == "ball-own"
    assert event_icon("red_card", {"reason": "second_yellow"}) == "card-second-yellow"
    assert event_icon("red_card", {}) == "card-red"
    assert event_icon("shootout_kick", {"scored": False}) == "x-circle"
    assert event_icon("shootout_kick", {"scored": True}) == "target"
    assert event_icon("nao_existe") == ""


# --- Jogo inteiro -------------------------------------------------------------------


def test_whole_match_with_goals():
    sim = Sim()
    start = sim.post(EventType.MATCH_START)
    assert (start.period, start.minute, start.sequence) == ("first_half", 0, 1)
    assert sim.state.status == Status.LIVE and sim.state.period == Period.FIRST_HALF
    assert sim.state.period_started_seq == start.sequence

    first = sim.goal(SPORT, "Diego Souza", 12)
    assert first.period == "first_half" and first.payload == {"player": "Diego Souza", "origin": "open_play"}
    sim.goal(NAUTICO, "Kieza", 45, 2, origin="penalty")
    half = sim.post(EventType.HALF_TIME, minute=45, stoppage=3)
    assert (half.period, half.minute, half.stoppage) == ("first_half", 45, 3)
    assert sim.state.period == Period.HALF_TIME and sim.state.period_started_seq == half.sequence

    second = sim.post(EventType.SECOND_HALF_START)
    assert (second.period, second.minute) == ("second_half", 45)
    sim.goal(SPORT, "Hernane", 67, assist="Diego Souza")
    sim.goal(SPORT, "Ortigoza", 90, 4, origin="own_goal")  # gol contra: jogador do adversário
    assert (sim.state.home_score, sim.state.away_score) == (3, 1)

    end = sim.post(EventType.MATCH_END)
    assert (end.period, end.minute) == ("second_half", 90)
    assert sim.state.status == Status.FINISHED and sim.state.period is None
    assert sim.state.period_started_seq is None
    assert score(sim.events, CTX) == (3, 1)
    assert [event.sequence for event in sim.events] == list(range(1, 9))
    assert available_actions(sim.state, CTX, sim.events) == {"events": [], "status": []}


def test_goal_annulled_then_annulment_voided_restores_score():
    sim = live_sim()
    goal = sim.goal(SPORT, "Barbosa", 20)
    annul = sim.post(EventType.GOAL_ANNULLED, minute=22, annuls_event_id=goal.id, payload={"reason": "Impedimento"})
    assert annul.team_id == SPORT and annul.annuls_event_id == goal.id
    assert annul.payload == {"reason": "Impedimento", "player": "Barbosa"}
    assert (sim.state.home_score, sim.state.away_score) == (0, 0)
    assert valid_goals(sim.events) == []

    result = sim.void(annul.id)
    assert result.voided_ids == (annul.id,)
    assert (sim.state.home_score, sim.state.away_score) == (1, 0)
    assert [g.id for g in valid_goals(sim.events)] == [goal.id]

    # anulação cancelada: o gol pode ser anulado de novo
    sim.post(EventType.GOAL_ANNULLED, minute=25, annuls_event_id=goal.id, payload={"reason": "Falta no lance"})
    assert sim.state.home_score == 0


def test_voiding_goal_cascades_its_annulment():
    sim = live_sim()
    goal = sim.goal(NAUTICO, "Kieza", 30)
    sim.goal(SPORT, "Leandro Barcia", 31)
    annul = sim.post(EventType.GOAL_ANNULLED, minute=33, annuls_event_id=goal.id, payload={"reason": "Mão na bola"})
    result = sim.void(goal.id)
    assert result.voided_ids == (goal.id, annul.id)
    assert (sim.state.home_score, sim.state.away_score) == (1, 0)
    assert sim.by_id(annul.id).voided


def test_annul_target_must_be_a_visible_goal_not_annulled():
    sim = live_sim()
    card = sim.card(EventType.YELLOW_CARD, SPORT, "Durval", 5)
    goal = sim.goal(SPORT, "Durval", 10)
    reason = {"reason": "VAR"}
    rejects("annul_target_invalid", sim, EventType.GOAL_ANNULLED, minute=11, annuls_event_id=card.id, payload=reason)
    rejects("annul_target_invalid", sim, EventType.GOAL_ANNULLED, minute=11, annuls_event_id=9999, payload=reason)
    rejects("annul_target_invalid", sim, EventType.GOAL_ANNULLED, minute=11, annuls_event_id=goal.id,
            team_id=NAUTICO, payload=reason)
    sim.post(EventType.GOAL_ANNULLED, minute=11, annuls_event_id=goal.id, payload=reason)
    rejects("annul_target_invalid", sim, EventType.GOAL_ANNULLED, minute=12, annuls_event_id=goal.id, payload=reason)
    voided_goal = sim.goal(NAUTICO, "Kieza", 14)
    sim.void(voided_goal.id)
    rejects("annul_target_invalid", sim, EventType.GOAL_ANNULLED, minute=15, annuls_event_id=voided_goal.id,
            payload=reason)


def test_goal_annulled_without_target_needs_team_and_reason():
    sim = live_sim()
    sim.goal(SPORT, "Magrão", 3)
    rejects("team_required", sim, EventType.GOAL_ANNULLED, minute=8, payload={"reason": "Falta"})
    rejects("invalid_payload", sim, EventType.GOAL_ANNULLED, minute=8, team_id=NAUTICO, payload={"reason": "  "})
    annul = sim.post(EventType.GOAL_ANNULLED, minute=8, team_id=NAUTICO, payload={"reason": "Falta no goleiro"})
    assert annul.annuls_event_id is None and annul.team_id == NAUTICO
    assert (sim.state.home_score, sim.state.away_score) == (1, 0)


# --- Transições ---------------------------------------------------------------------


def test_invalid_structural_transitions():
    sim = Sim()
    rejects("invalid_transition", sim, EventType.HALF_TIME)
    rejects("invalid_transition", sim, EventType.MATCH_END)
    rejects("invalid_transition", sim, EventType.SECOND_HALF_START)
    sim.post(EventType.MATCH_START)
    rejects("invalid_transition", sim, EventType.MATCH_START)
    rejects("invalid_transition", sim, EventType.SECOND_HALF_START)
    rejects("invalid_transition", sim, EventType.MATCH_END)
    rejects("extra_time_not_allowed", sim, EventType.EXTRA_TIME_START)
    rejects("penalties_not_allowed", sim, EventType.PENALTIES_START)
    sim.post(EventType.HALF_TIME)
    rejects("invalid_transition", sim, EventType.HALF_TIME)
    rejects("invalid_transition", sim, EventType.MATCH_END)
    sim.post(EventType.SECOND_HALF_START)
    # jogo sem confronto: nunca há prorrogação nem pênaltis
    rejects("extra_time_not_allowed", sim, EventType.EXTRA_TIME_START)
    rejects("penalties_not_allowed", sim, EventType.PENALTIES_START)
    sim.post(EventType.MATCH_END)
    for event_type in (EventType.MATCH_START, EventType.MATCH_END, EventType.HALF_TIME):
        rejects("invalid_transition", sim, event_type)


@pytest.mark.parametrize("prepare", ["scheduled", "finished", "suspended", "postponed", "cancelled"])
def test_game_events_need_live_match(prepare):
    sim = Sim()
    if prepare == "finished":
        sim.to_second_half().post(EventType.MATCH_END)
    elif prepare == "suspended":
        sim.post(EventType.MATCH_START)
        sim.status("suspend")
    elif prepare == "postponed":
        sim.status("postpone")
    elif prepare == "cancelled":
        sim.status("cancel")
    for event_type, fields in (
        (EventType.GOAL, {"team_id": SPORT, "minute": 10, "payload": {"player": "Zé"}}),
        (EventType.YELLOW_CARD, {"team_id": SPORT, "minute": 10, "payload": {"player": "Zé"}}),
        (EventType.STOPPAGE_TIME, {"minute": 45, "payload": {"minutes": 3}}),
    ):
        rejects("match_not_live", sim, event_type, **fields)


def test_events_only_in_allowed_periods():
    sim = live_sim()
    rejects("invalid_period_for_event", sim, EventType.SHOOTOUT_KICK, team_id=SPORT,
            payload={"player": "Zé", "scored": True})
    sim.post(EventType.HALF_TIME)
    rejects("invalid_period_for_event", sim, EventType.GOAL, team_id=SPORT, payload={"player": "Zé"})
    rejects("invalid_period_for_event", sim, EventType.STOPPAGE_TIME, payload={"minutes": 2})
    rejects("invalid_period_for_event", sim, EventType.VAR_REVIEW, payload={"incident": "a", "decision": "b"})
    # no intervalo valem cartão e substituição, sem minuto
    card = sim.card(EventType.YELLOW_CARD, NAUTICO, "Kieza", None)
    assert (card.period, card.minute) == ("half_time", None)
    sub = sim.sub(SPORT, "Magrão", "Saulo", None)
    assert sub.period == "half_time"


def test_unknown_type_and_team_player_validation():
    sim = live_sim()
    rejects("unknown_event_type", sim, "bicicleta", minute=3)
    rejects("team_required", sim, EventType.GOAL, minute=3, payload={"player": "Zé"})
    rejects("team_not_in_match", sim, EventType.GOAL, minute=3, team_id=99, payload={"player": "Zé"})
    rejects("player_required", sim, EventType.GOAL, minute=3, team_id=SPORT, payload={"player": "  "})
    rejects("player_required", sim, EventType.SUBSTITUTION, minute=3, team_id=SPORT, payload={"player_in": "Zé"})
    rejects("invalid_payload", sim, EventType.GOAL, minute=3, team_id=SPORT, payload={"player": "Zé", "origin": "voleio"})
    rejects("invalid_payload", sim, EventType.GOAL, minute=3, team_id=SPORT,
            payload={"player": "Zé", "origin": "own_goal", "assist": "Biu"})
    rejects("invalid_payload", sim, EventType.GOAL, minute=3, team_id=SPORT, payload=["Zé"])
    rejects("invalid_payload", sim, EventType.GOAL, minute=3, team_id=SPORT, payload={"player": "Z" * 81})
    rejects("invalid_payload", sim, EventType.VAR_REVIEW, minute=3, payload={"incident": "Pênalti?"})
    rejects("team_not_in_match", sim, EventType.VAR_REVIEW, minute=3, team_id=7,
            payload={"incident": "Pênalti?", "decision": "Mantido"})
    rejects("invalid_payload", sim, EventType.SUBSTITUTION, minute=3, team_id=SPORT,
            payload={"player_out": "Zé", "player_in": "  zé "})
    rejects("invalid_payload", sim, EventType.PENALTY_MISSED, minute=3, team_id=SPORT,
            payload={"player": "Zé", "outcome": "na lua"})
    rejects("invalid_payload", sim, EventType.GOAL, minute=3, team_id=SPORT, player_id=-4)


def test_game_payloads_are_normalized():
    sim = live_sim()
    goal = sim.post(EventType.GOAL, minute=9, team_id=SPORT, payload={"player": "  Zé   Roberto ", "extra": "x"})
    assert goal.payload == {"player": "Zé Roberto", "origin": "open_play"}
    var = sim.post(EventType.VAR_REVIEW, minute=10, payload={"incident": " Possível pênalti ", "decision": "Mantido"})
    assert var.team_id is None and var.payload == {"incident": "Possível pênalti", "decision": "Mantido"}
    awarded = sim.post(EventType.PENALTY_AWARDED, minute=11, team_id=NAUTICO, annuls_event_id=goal.id)
    assert awarded.annuls_event_id is None and awarded.payload == {}
    missed = sim.post(EventType.PENALTY_MISSED, minute=12, team_id=NAUTICO, payload={"player": "Kieza", "outcome": "saved"})
    assert missed.payload == {"player": "Kieza", "outcome": "saved"}


# --- Minutos ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "minute", "stoppage"),
    [
        ("first_half", 46, None),
        ("first_half", 44, 2),
        ("first_half", -1, None),
        ("first_half", 45, 31),
        ("second_half", 44, None),
        ("second_half", 91, None),
        ("second_half", 89, 1),
        ("second_half", 45, 1),
        ("extra_time", 89, None),
        ("extra_time", 121, None),
        ("extra_time", 100, 1),
        ("half_time", 50, None),
    ],
)
def test_minutes_out_of_range(period, minute, stoppage):
    sim = live_sim()
    if period != "first_half":
        sim.post(EventType.HALF_TIME)
    if period in ("second_half", "extra_time"):
        sim.post(EventType.SECOND_HALF_START)
    if period == "extra_time":
        sim.state = replace(sim.state, period=Period.EXTRA_TIME)  # atalho: só o minuto importa aqui
        sim.events = []
    error = rejects("invalid_minute", sim, EventType.YELLOW_CARD, team_id=SPORT, minute=minute, stoppage=stoppage,
                    payload={"player": "Zé"})
    assert error.message


@pytest.mark.parametrize(
    ("period", "minute", "stoppage"),
    [
        ("first_half", 0, None),
        ("first_half", 45, 3),
        ("second_half", 45, None),
        ("second_half", 90, 7),
        ("extra_time", 105, 1),
        ("extra_time", 120, 2),
    ],
)
def test_minutes_in_range(period, minute, stoppage):
    state = MatchState(status=Status.LIVE, period=period)
    result = apply_event(state, [], NewEvent("yellow_card", minute, stoppage, SPORT, payload={"player": "Zé"}), CTX)
    assert (result.event.minute, result.event.stoppage, result.event.period) == (minute, stoppage, period)


def test_minute_required_in_play_and_stoppage_zero_is_no_stoppage():
    sim = live_sim()
    rejects("invalid_minute", sim, EventType.GOAL, team_id=SPORT, payload={"player": "Zé"})
    rejects("invalid_minute", sim, EventType.GOAL, team_id=SPORT, stoppage=2, payload={"player": "Zé"})
    rejects("invalid_minute", sim, EventType.GOAL, team_id=SPORT, minute="12", payload={"player": "Zé"})
    goal = sim.goal(SPORT, "Zé", 30, 0)
    assert goal.stoppage is None and format_minute(goal.minute, goal.stoppage) == "30'"


def test_structural_minutes_have_defaults_and_fixed_points():
    sim = Sim()
    rejects("invalid_minute", sim, EventType.MATCH_START, minute=1)
    rejects("invalid_minute", sim, EventType.MATCH_START, minute=0, stoppage=1)
    sim.post(EventType.MATCH_START, minute=0)
    rejects("invalid_minute", sim, EventType.HALF_TIME, minute=44)
    half = sim.post(EventType.HALF_TIME, minute=45, stoppage=2)
    assert (half.minute, half.stoppage) == (45, 2)
    rejects("invalid_minute", sim, EventType.SECOND_HALF_START, minute=45, stoppage=1)
    sim.post(EventType.SECOND_HALF_START)
    rejects("invalid_minute", sim, EventType.MATCH_END, minute=88)
    end = sim.post(EventType.MATCH_END, minute=90, stoppage=6)
    assert (end.minute, end.stoppage) == (90, 6)


def test_stoppage_time_announcement():
    sim = live_sim()
    rejects("invalid_payload", sim, EventType.STOPPAGE_TIME, minute=45, payload={"minutes": 0})
    rejects("invalid_payload", sim, EventType.STOPPAGE_TIME, minute=45, payload={"minutes": 31})
    rejects("invalid_payload", sim, EventType.STOPPAGE_TIME, minute=45, payload={"minutes": "três"})
    rejects("invalid_payload", sim, EventType.STOPPAGE_TIME, minute=45, payload={"minutes": True})
    event = sim.post(EventType.STOPPAGE_TIME, minute=45, payload={"minutes": "4"})
    assert event.payload == {"minutes": 4}
    sim.post(EventType.HALF_TIME)
    sim.post(EventType.SECOND_HALF_START)
    sim.post(EventType.STOPPAGE_TIME, minute=90, payload={"minutes": 6})
    assert sim.state.stoppage_announced == {"first_half": 4, "second_half": 6}


# --- Regras brandas -------------------------------------------------------------------


def test_minute_decreasing_needs_confirmation():
    sim = live_sim()
    sim.goal(SPORT, "Zé", 30)
    error = rejects("confirmation_required", sim, EventType.YELLOW_CARD, team_id=NAUTICO, minute=20,
                    payload={"player": "Kieza"})
    assert warning_codes(error) == ["minute_decreasing"]
    assert error.details["codes"] == ["minute_decreasing"]
    sim.post(EventType.YELLOW_CARD, team_id=NAUTICO, minute=20, payload={"player": "Kieza"}, confirm=True)
    assert warning_codes(sim.last) == ["minute_decreasing"]
    assert sim.state.last_clock == (1, 30, 0)  # guarda o instante mais adiantado
    sim.goal(SPORT, "Zé", 45, 1)
    assert sim.last.warnings == ()
    # outro período sempre vem depois
    sim.post(EventType.HALF_TIME)
    sim.post(EventType.SECOND_HALF_START)
    sim.goal(NAUTICO, "Kieza", 45)
    assert sim.last.warnings == () and sim.state.last_clock == (3, 45, 0)


def test_player_sent_off_warning_uses_normalized_name():
    sim = live_sim()
    sim.card(EventType.RED_CARD, NAUTICO, "João Paulo", 10)
    error = rejects("confirmation_required", sim, EventType.GOAL, team_id=NAUTICO, minute=12,
                    payload={"player": "joao  paulo"})
    assert warning_codes(error) == ["player_sent_off"]
    rejects("confirmation_required", sim, EventType.YELLOW_CARD, team_id=NAUTICO, minute=13,
            payload={"player": "JOÃO PAULO"})
    # mesmo nome no outro time é outro jogador
    sim.goal(SPORT, "João Paulo", 14)
    assert sim.last.warnings == ()


def test_substitutions_tracked_without_lineup():
    sim = live_sim()
    sim.sub(SPORT, "Magrão", "Saulo", 20)
    assert sim.last.warnings == ()
    error = rejects("confirmation_required", sim, EventType.GOAL, team_id=SPORT, minute=25, payload={"player": "Magrão"})
    assert warning_codes(error) == ["player_not_on_field"]
    error = rejects("confirmation_required", sim, EventType.SUBSTITUTION, team_id=SPORT, minute=26,
                    payload={"player_out": "Magrão", "player_in": "Rithely"})
    assert warning_codes(error) == ["player_not_on_field"]
    error = rejects("confirmation_required", sim, EventType.SUBSTITUTION, team_id=SPORT, minute=26,
                    payload={"player_out": "Durval", "player_in": "magrao"})
    assert warning_codes(error) == ["substitute_already_used"]
    error = rejects("confirmation_required", sim, EventType.SUBSTITUTION, team_id=SPORT, minute=26,
                    payload={"player_out": "Durval", "player_in": "Saulo"})
    assert warning_codes(error) == ["substitute_already_used"]
    # quem entrou pode sair depois; cartão para quem saiu não avisa (vale no banco)
    sim.sub(SPORT, "Saulo", "Rithely", 40)
    sim.card(EventType.YELLOW_CARD, SPORT, "Magrão", 41)
    assert sim.last.warnings == ()
    assert {"t1:magrao", "t1:saulo"} <= sim.state.subbed_off and {"t1:saulo", "t1:rithely"} <= sim.state.subbed_on


LINEUPS = {
    SPORT: (
        LineupPlayer("Magrão", True, 11, 1),
        LineupPlayer("Durval", True, 12, 4),
        LineupPlayer("Diego Souza", True, 13, 87),
        LineupPlayer("Leonardo", False, 14, 9),
        LineupPlayer("Rithely", False, 15, 5),
    ),
    NAUTICO: (
        LineupPlayer("Kieza", True, 21, 9),
        LineupPlayer("Grafite", True, None, 23),
        LineupPlayer("Ademir", False, 23, 7),
    ),
}
LINEUP_CTX = MatchContext(SPORT, NAUTICO, lineups=LINEUPS)


def test_lineup_resolves_names_and_ids():
    sim = live_sim(LINEUP_CTX)
    goal = sim.goal(SPORT, "diego  souza", 10)
    assert goal.payload["player"] == "Diego Souza" and goal.player_id == 13
    by_id = sim.post(EventType.YELLOW_CARD, team_id=SPORT, minute=11, player_id=12)
    assert by_id.payload == {"player": "Durval"} and by_id.player_id == 12
    sub = sim.sub(SPORT, "Magrao", "LEONARDO", 46 - 1)
    assert sub.payload == {"player_out": "Magrão", "player_in": "Leonardo", "player_out_id": 11, "player_in_id": 14}
    assert "id:11" in sim.state.subbed_off and "id:14" in sim.state.subbed_on


def test_lineup_warnings():
    sim = live_sim(LINEUP_CTX)
    error = rejects("confirmation_required", sim, EventType.GOAL, team_id=SPORT, minute=5, payload={"player": "Juninho"})
    assert warning_codes(error) == ["player_not_in_lineup"]
    error = rejects("confirmation_required", sim, EventType.GOAL, team_id=SPORT, minute=5, payload={"player": "Leonardo"})
    assert warning_codes(error) == ["player_not_on_field"]
    error = rejects("confirmation_required", sim, EventType.PENALTY_MISSED, team_id=SPORT, minute=5,
                    payload={"player": "Rithely"})
    assert warning_codes(error) == ["player_not_on_field"]
    # substituição: sai quem está no banco, entra quem é titular
    error = rejects("confirmation_required", sim, EventType.SUBSTITUTION, team_id=SPORT, minute=6,
                    payload={"player_out": "Rithely", "player_in": "Durval"})
    assert warning_codes(error) == ["player_not_on_field", "substitute_already_used"]
    error = rejects("confirmation_required", sim, EventType.SUBSTITUTION, team_id=SPORT, minute=6,
                    payload={"player_out": "Durval", "player_in": "Juninho"})
    assert warning_codes(error) == ["player_not_in_lineup"]
    # cartão: banco pode, fora da escalação avisa
    sim.card(EventType.YELLOW_CARD, SPORT, "Rithely", 7)
    assert sim.last.warnings == ()
    error = rejects("confirmation_required", sim, EventType.YELLOW_CARD, team_id=SPORT, minute=8,
                    payload={"player": "Juninho"})
    assert warning_codes(error) == ["player_not_in_lineup"]

    sim.sub(SPORT, "Diego Souza", "Leonardo", 60 - 15)
    sim.goal(SPORT, "Leonardo", 45, 1)
    assert sim.last.warnings == ()
    error = rejects("confirmation_required", sim, EventType.GOAL, team_id=SPORT, minute=45, stoppage=2,
                    payload={"player": "Diego Souza"})
    assert warning_codes(error) == ["player_not_on_field"]
    error = rejects("confirmation_required", sim, EventType.SUBSTITUTION, team_id=SPORT, minute=45, stoppage=2,
                    payload={"player_out": "Durval", "player_in": "Leonardo"})
    assert warning_codes(error) == ["substitute_already_used"]
    # com confirmação o lançamento entra e os avisos voltam
    sim.post(EventType.GOAL, team_id=SPORT, minute=45, stoppage=3, payload={"player": "Juninho"}, confirm=True)
    assert warning_codes(sim.last) == ["player_not_in_lineup"]
    assert sim.state.home_score == 2


def test_own_goal_scorer_checked_against_opponent_lineup():
    sim = live_sim(LINEUP_CTX)
    own = sim.post(EventType.GOAL, team_id=SPORT, minute=20, payload={"player": "kieza", "origin": "own_goal"})
    assert sim.last.warnings == () and own.player_id == 21 and own.payload["player"] == "Kieza"
    assert sim.state.home_score == 1
    error = rejects("confirmation_required", sim, EventType.GOAL, team_id=SPORT, minute=21,
                    payload={"player": "Magrão", "origin": "own_goal"})
    assert warning_codes(error) == ["player_not_in_lineup"]


# --- Segundo amarelo -------------------------------------------------------------------


def second_yellow_sim() -> tuple[Sim, Event, Event, Event]:
    sim = live_sim()
    first = sim.card(EventType.YELLOW_CARD, NAUTICO, "Ademir", 15)
    assert sim.last.derived == ()
    second = sim.card(EventType.YELLOW_CARD, NAUTICO, "ADEMIR", 40)
    red = sim.events[-1]
    return sim, first, second, red


def test_second_yellow_derives_automatic_red():
    sim, first, second, red = second_yellow_sim()
    assert len(sim.last.derived) == 1
    assert red.type == "red_card" and red.sequence == second.sequence + 1
    assert (red.team_id, red.minute, red.period) == (NAUTICO, 40, "first_half")
    # o vermelho repete o nome como lançado no 2º amarelo
    assert red.payload == {"player": "ADEMIR", "reason": "second_yellow", "derived_from_sequence": second.sequence}
    key = player_key(NAUTICO, None, "Ademir")
    assert key in sim.state.sent_off and sim.state.yellow_cards[key] == 2
    assert event_icon(red.type, red.payload) == "card-second-yellow"
    # terceiro amarelo (confirmado) não gera outro vermelho
    sim.card(EventType.YELLOW_CARD, NAUTICO, "Ademir", 41, confirm=True)
    assert sim.last.derived == () and warning_codes(sim.last) == ["player_sent_off"]


def test_voiding_second_yellow_cascades_red():
    sim, first, second, red = second_yellow_sim()
    result = sim.void(second.id)
    assert result.voided_ids == (second.id, red.id)
    key = player_key(NAUTICO, None, "Ademir")
    assert key not in sim.state.sent_off and sim.state.yellow_cards[key] == 1


def test_voiding_first_yellow_drops_orphan_red():
    sim, first, second, red = second_yellow_sim()
    result = sim.void(first.id)
    assert result.voided_ids == (first.id, red.id)
    key = player_key(NAUTICO, None, "Ademir")
    assert key not in sim.state.sent_off and sim.state.yellow_cards[key] == 1


def test_derived_red_cannot_be_voided_alone():
    sim, first, second, red = second_yellow_sim()
    with pytest.raises(DomainError) as info:
        check_void(sim.events, red.id, sim.ctx)
    assert info.value.code == "void_derived_event"


def test_second_yellow_at_half_time_without_minute():
    sim = live_sim()
    sim.card(EventType.YELLOW_CARD, SPORT, "Durval", 30)
    sim.post(EventType.HALF_TIME)
    sim.card(EventType.YELLOW_CARD, SPORT, "Durval", None)
    red = sim.events[-1]
    assert (red.type, red.period, red.minute) == ("red_card", "half_time", None)


# --- Cancelamento de lançamento ---------------------------------------------------------


def test_void_errors():
    sim = live_sim()
    goal = sim.goal(SPORT, "Zé", 10)
    with pytest.raises(DomainError) as info:
        check_void(sim.events, 424242, CTX)
    assert info.value.code == "event_not_found"
    sim.void(goal.id)
    with pytest.raises(DomainError) as info:
        check_void(sim.events, goal.id, CTX)
    assert info.value.code == "already_voided"


def test_void_breaks_sequence():
    sim = live_sim()
    start = sim.events[0]
    sim.goal(SPORT, "Zé", 10)
    with pytest.raises(DomainError) as info:
        check_void(sim.events, start.id, CTX)
    assert info.value.code == "void_breaks_sequence" and info.value.details["cause"] == "match_not_live"
    half = sim.post(EventType.HALF_TIME)
    sim.post(EventType.SECOND_HALF_START)
    with pytest.raises(DomainError) as info:
        check_void(sim.events, half.id, CTX)
    assert info.value.code == "void_breaks_sequence" and info.value.details["cause"] == "invalid_transition"


def test_void_structural_event_reverts_state():
    sim = Sim().to_second_half()
    sim.goal(SPORT, "Zé", 50)
    end = sim.post(EventType.MATCH_END)
    sim.void(end.id)
    assert sim.state.status == Status.LIVE and sim.state.period == Period.SECOND_HALF
    sim.goal(SPORT, "Zé", 88)
    assert sim.state.home_score == 2


def test_period_mismatch_after_void_breaks_sequence():
    sim = Sim().to_second_half()
    second_half = sim.events[-1]
    sim.status("suspend")
    with pytest.raises(DomainError) as info:
        check_void(sim.events, second_half.id, CTX)
    assert info.value.details["cause"] == "period_mismatch"


def test_apply_event_sequence_counts_voided_events():
    sim = live_sim()
    goal = sim.goal(SPORT, "Zé", 10)
    sim.void(goal.id)
    card = sim.card(EventType.YELLOW_CARD, SPORT, "Zé", 12)
    assert card.sequence == goal.sequence + 1
    result = apply_event(sim.state, sim.events, NewEvent("penalty_awarded", 13, team_id=SPORT), CTX, next_sequence=50)
    assert result.event.sequence == 50


# --- Status ----------------------------------------------------------------------------


def test_postpone_reschedule_then_start():
    sim = Sim()
    postponed = sim.status("postpone", reason="  Chuva forte no Arruda ")
    assert postponed.payload == {"reason": "Chuva forte no Arruda"} and postponed.period is None
    assert sim.state.status == Status.POSTPONED
    assert available_actions(sim.state, CTX) == {"events": [], "status": ["reschedule", "cancel"]}
    rejects("invalid_transition", sim, EventType.MATCH_START)
    rescheduled = sim.status("reschedule", kickoff_at="2026-10-10T19:30:00Z")
    assert rescheduled.payload == {"kickoff_at": "2026-10-10T19:30:00+00:00"}
    assert sim.state.status == Status.SCHEDULED and sim.state.rescheduled_to == "2026-10-10T19:30:00+00:00"
    sim.status("reschedule", kickoff_at="2026-10-11T16:00:00-03:00")  # agendado → agendado
    assert sim.state.rescheduled_to == "2026-10-11T16:00:00-03:00"
    sim.post(EventType.MATCH_START)
    assert sim.state.status == Status.LIVE


def test_delay_needs_reason_then_start():
    sim = Sim()
    with pytest.raises(DomainError) as exc:
        status_action_event("delay")
    assert exc.value.code == "invalid_payload"
    delayed = sim.status("delay", reason=" Chuva forte ")
    assert delayed.payload == {"reason": "Chuva forte"} and delayed.period is None
    assert sim.state.status == Status.DELAYED
    assert available_actions(sim.state, CTX) == {
        "events": ["match_start"],
        "status": ["delay", "postpone", "reschedule", "cancel"],
    }
    sim.status("delay", reason="Ambulância a caminho")  # atualiza a observação
    assert sim.state.status == Status.DELAYED
    sim.post(EventType.MATCH_START)
    assert sim.state.status == Status.LIVE and sim.state.period == Period.FIRST_HALF
    with pytest.raises(DomainError) as exc:
        sim.status("delay", reason="tarde demais")
    assert exc.value.code == "invalid_status_action"


def test_delayed_match_can_be_postponed_or_cancelled():
    sim = Sim()
    sim.status("delay", reason="Gramado alagado")
    sim.status("postpone", reason="Sem condições")
    assert sim.state.status == Status.POSTPONED
    other = Sim()
    other.status("delay", reason="Gramado alagado")
    other.status("cancel")
    assert other.state.status == Status.CANCELLED


def test_suspend_keeps_period_and_resume():
    sim = Sim().to_second_half()
    sim.goal(NAUTICO, "Kieza", 60)
    suspended = sim.status("suspend", reason="Queda de energia")
    assert suspended.period == "second_half"
    assert sim.state.status == Status.SUSPENDED and sim.state.period == Period.SECOND_HALF
    started = sim.state.period_started_seq
    rejects("match_not_live", sim, EventType.GOAL, team_id=SPORT, minute=61, payload={"player": "Zé"})
    rejects("invalid_transition", sim, EventType.MATCH_END)
    assert available_actions(sim.state, CTX) == {"events": [], "status": ["resume", "cancel"]}
    resumed = sim.status("resume")
    assert resumed.period == "second_half" and resumed.payload == {}
    assert sim.state.status == Status.LIVE and sim.state.period == Period.SECOND_HALF
    assert sim.state.period_started_seq == started
    sim.goal(SPORT, "Zé", 75)
    assert (sim.state.home_score, sim.state.away_score) == (1, 1)


def test_cancel_from_suspended_and_not_from_live():
    sim = Sim().to_second_half()
    error = rejects("invalid_status_action", sim, EventType.CANCELLED)
    assert "Suspenda" in error.message
    rejects("invalid_status_action", sim, EventType.RESUMED)
    rejects("invalid_status_action", sim, EventType.POSTPONED)
    rejects("invalid_status_action", sim, EventType.RESCHEDULED, payload={"kickoff_at": "2026-10-10T19:30:00Z"})
    sim.status("suspend")
    cancelled = sim.status("cancel", reason="Briga generalizada")
    assert cancelled.period == "second_half"
    assert sim.state.status == Status.CANCELLED and sim.state.period is None
    assert available_actions(sim.state, CTX) == {"events": [], "status": []}
    for event_type in (EventType.RESUMED, EventType.CANCELLED, EventType.POSTPONED):
        rejects("invalid_status_action", sim, event_type)
    rejects("invalid_transition", sim, EventType.MATCH_START)


def test_finished_match_rejects_status_actions():
    sim = Sim().to_second_half()
    sim.post(EventType.MATCH_END)
    for event_type in (EventType.SUSPENDED, EventType.CANCELLED, EventType.POSTPONED):
        rejects("invalid_status_action", sim, event_type)


def test_status_action_event():
    assert status_action_event("suspend") == NewEvent("suspended")
    assert status_action_event(StatusAction.CANCEL, reason="WO") == NewEvent("cancelled", payload={"reason": "WO"})
    with pytest.raises(DomainError) as info:
        status_action_event("pause")
    assert info.value.code == "invalid_status_action"
    with pytest.raises(DomainError) as info:
        status_action_event("reschedule")
    assert info.value.code == "invalid_payload"
    with pytest.raises(DomainError) as info:
        status_action_event("reschedule", kickoff_at="amanhã às 16h")
    assert info.value.code == "invalid_payload"


def test_status_events_validated_in_apply():
    sim = Sim()
    rejects("invalid_payload", sim, EventType.RESCHEDULED, payload={})
    rejects("invalid_payload", sim, EventType.RESCHEDULED, payload={"kickoff_at": "10/10/2026"})
    rejects("invalid_minute", sim, EventType.POSTPONED, minute=10)
    event = sim.post(EventType.POSTPONED, team_id=SPORT, payload={"reason": "Gramado alagado", "x": 1})
    assert event.team_id is None and event.payload == {"reason": "Gramado alagado"}


# --- Replay -----------------------------------------------------------------------------


def busy_match() -> Sim:
    sim = Sim(LINEUP_CTX)
    sim.post(EventType.MATCH_START)
    goal = sim.goal(SPORT, "Diego Souza", 8)
    sim.post(EventType.VAR_REVIEW, minute=9, team_id=SPORT, payload={"incident": "Gol", "decision": "Anulado"})
    sim.post(EventType.GOAL_ANNULLED, minute=9, annuls_event_id=goal.id, payload={"reason": "Impedimento"})
    sim.post(EventType.PENALTY_AWARDED, minute=20, team_id=NAUTICO)
    sim.post(EventType.PENALTY_MISSED, minute=21, team_id=NAUTICO, payload={"player": "Kieza"})
    sim.card(EventType.YELLOW_CARD, NAUTICO, "Grafite", 30)
    sim.post(EventType.STOPPAGE_TIME, minute=45, payload={"minutes": 3})
    sim.post(EventType.HALF_TIME, minute=45, stoppage=3)
    sim.sub(SPORT, "Magrão", "Rithely", None)
    sim.post(EventType.SECOND_HALF_START)
    sim.card(EventType.YELLOW_CARD, NAUTICO, "grafite", 50)  # 2º amarelo → vermelho
    sim.post(EventType.GOAL, team_id=SPORT, minute=70, payload={"player": "Kieza", "origin": "own_goal"})
    sim.status("suspend")
    sim.status("resume")
    # do banco, sem ter entrado: aviso confirmado
    sim.post(EventType.GOAL, team_id=NAUTICO, minute=80, payload={"player": "Ademir"}, confirm=True)
    assert warning_codes(sim.last) == ["player_not_on_field"]
    sim.post(EventType.MATCH_END, minute=90, stoppage=5)
    return sim


def test_derive_state_equals_incremental_apply():
    sim = busy_match()
    assert sim.state.status == Status.FINISHED
    assert (sim.state.home_score, sim.state.away_score) == (1, 1)
    assert derive_state(sim.events, LINEUP_CTX) == sim.state
    shuffled = list(sim.events)
    random.Random(7).shuffle(shuffled)
    assert derive_state(shuffled, LINEUP_CTX) == sim.state  # ordem de entrada não importa


def test_derive_state_rejects_inconsistent_sequences():
    with pytest.raises(DomainError) as info:
        derive_state([Event(1, "goal", "first_half", 3, team_id=SPORT, payload={"player": "Zé"})], CTX)
    assert info.value.code == "match_not_live"
    events = [
        Event(1, "match_start", "first_half", 0),
        Event(2, "goal", "second_half", 3, team_id=SPORT, payload={"player": "Zé"}),
    ]
    with pytest.raises(DomainError) as info:
        derive_state(events, CTX)
    assert info.value.code == "period_mismatch"
    # período ausente (seed antigo) não é conferido
    events[1] = replace(events[1], period=None)
    assert derive_state(events, CTX).home_score == 1


# --- Ações disponíveis ---------------------------------------------------------------------


FIRST_HALF_EVENTS = [
    "half_time", "goal", "penalty_awarded", "penalty_missed", "yellow_card", "red_card",
    "substitution", "var_review", "stoppage_time", "goal_annulled",
]


def test_available_actions_by_phase():
    sim = Sim()
    assert available_actions(sim.state, CTX) == {"events": ["match_start"], "status": ["delay", "postpone", "reschedule", "cancel"]}
    sim.post(EventType.MATCH_START)
    assert available_actions(sim.state, CTX) == {"events": FIRST_HALF_EVENTS, "status": ["suspend"]}
    sim.post(EventType.HALF_TIME)
    assert available_actions(sim.state, CTX) == {
        "events": ["second_half_start", "yellow_card", "red_card", "substitution", "goal_annulled"],
        "status": ["suspend"],
    }
    sim.post(EventType.SECOND_HALF_START)
    actions = available_actions(sim.state, CTX)
    assert actions["events"][0] == "match_end" and actions["events"][1:] == FIRST_HALF_EVENTS[1:]
    assert "extra_time_start" not in actions["events"] and "penalties_start" not in actions["events"]
    sim.post(EventType.MATCH_END)
    assert available_actions(sim.state, CTX) == {"events": [], "status": []}


def test_available_actions_match_apply_event():
    """Todo tipo listado é aceito por apply_event e todo tipo fora da lista é recusado."""
    sim = busy_match()
    for upto in range(len(sim.events) + 1):
        events = sim.events[:upto]
        state = derive_state(events, LINEUP_CTX)
        allowed = set(available_actions(state, LINEUP_CTX, events)["events"])
        for event_type, spec in CATALOG.items():
            if spec.kind == "status":
                continue
            sample = SAMPLES[event_type]
            try:
                apply_event(state, events, sample(state), LINEUP_CTX, confirm=True)
                accepted = True
            except DomainError as exc:
                assert exc.code not in ("confirmation_required",)
                accepted = exc.code in ("annul_target_invalid", "invalid_minute")
            assert accepted == (event_type in allowed), (upto, event_type)


def _minute_for(state: MatchState) -> int | None:
    return {"first_half": 45, "second_half": 90, "extra_time": 120}.get(state.period)


SAMPLES = {
    "match_start": lambda s: NewEvent("match_start"),
    "half_time": lambda s: NewEvent("half_time"),
    "second_half_start": lambda s: NewEvent("second_half_start"),
    "extra_time_start": lambda s: NewEvent("extra_time_start"),
    "penalties_start": lambda s: NewEvent("penalties_start"),
    "match_end": lambda s: NewEvent("match_end"),
    "goal": lambda s: NewEvent("goal", _minute_for(s), team_id=SPORT, payload={"player": "Durval"}),
    "goal_annulled": lambda s: NewEvent("goal_annulled", _minute_for(s), team_id=SPORT, payload={"reason": "Falta"}),
    "penalty_awarded": lambda s: NewEvent("penalty_awarded", _minute_for(s), team_id=SPORT),
    "penalty_missed": lambda s: NewEvent("penalty_missed", _minute_for(s), team_id=SPORT, payload={"player": "Durval"}),
    "var_review": lambda s: NewEvent("var_review", _minute_for(s), payload={"incident": "Gol", "decision": "Mantido"}),
    "substitution": lambda s: NewEvent("substitution", _minute_for(s), team_id=NAUTICO,
                                       payload={"player_out": "Kieza", "player_in": "Bruno"}),
    "yellow_card": lambda s: NewEvent("yellow_card", _minute_for(s), team_id=SPORT, payload={"player": "Durval"}),
    "red_card": lambda s: NewEvent("red_card", _minute_for(s), team_id=SPORT, payload={"player": "Durval"}),
    "stoppage_time": lambda s: NewEvent("stoppage_time", _minute_for(s), payload={"minutes": 3}),
    "shootout_kick": lambda s: NewEvent("shootout_kick", team_id=SPORT, payload={"player": "Durval", "scored": True}),
}


# --- Casos de borda (revisão) ----------------------------------------------------------


def test_reschedule_needs_date_and_time():
    for value in ("2026-10-10", "20261010", "2026-W41-1", "2026-10-10T19", "2026-13-10T19:30"):
        with pytest.raises(DomainError) as info:
            status_action_event("reschedule", kickoff_at=value)
        assert info.value.code == "invalid_payload", value
    # sem fuso é aceito (o serviço aplica o horário de Brasília); datetime também
    assert status_action_event("reschedule", kickoff_at="2026-10-10 19:30").payload == {"kickoff_at": "2026-10-10T19:30:00"}
    aware = datetime(2026, 10, 10, 22, 30, tzinfo=UTC)
    assert status_action_event("reschedule", kickoff_at=aware).payload == {"kickoff_at": "2026-10-10T22:30:00+00:00"}
    rejects("invalid_payload", Sim(), EventType.RESCHEDULED, payload={"kickoff_at": "2026-10-10"})


def test_error_messages_use_the_right_preposition():
    sim = live_sim()
    sim.post(EventType.HALF_TIME)
    error = rejects("invalid_period_for_event", sim, EventType.VAR_REVIEW, payload={"incident": "a", "decision": "b"})
    assert error.message == "Não é possível lançar revisão do VAR no intervalo."
    state = MatchState(status=Status.LIVE, period=Period.EXTRA_TIME)
    with pytest.raises(DomainError) as info:
        apply_event(state, [], NewEvent("yellow_card", 121, team_id=SPORT, payload={"player": "Zé"}), CTX)
    assert info.value.message == "Na prorrogação, o minuto vai de 90 a 120."


def test_structural_minute_given_by_hand_is_compared_with_last_clock():
    sim = live_sim()
    sim.goal(SPORT, "Zé", 45, 4)
    error = rejects("confirmation_required", sim, EventType.HALF_TIME, minute=45, stoppage=2)
    assert warning_codes(error) == ["minute_decreasing"]
    # minuto natural (sem minuto informado) não avisa
    assert apply_event(sim.state, sim.events, NewEvent("half_time"), CTX).warnings == ()
    sim.post(EventType.HALF_TIME, minute=45, stoppage=2, confirm=True)
    assert warning_codes(sim.last) == ["minute_decreasing"]
    sim.card(EventType.YELLOW_CARD, NAUTICO, "Kieza", 45)  # intervalo, minuto 45
    sim.post(EventType.SECOND_HALF_START, minute=45)  # abre período: vem depois do intervalo
    assert sim.last.warnings == ()
    sim.goal(NAUTICO, "Kieza", 90, 5)
    error = rejects("confirmation_required", sim, EventType.MATCH_END, minute=90, stoppage=3)
    assert warning_codes(error) == ["minute_decreasing"]
    sim.post(EventType.MATCH_END, minute=90, stoppage=6)
    assert sim.last.warnings == ()


def test_void_that_turns_a_yellow_into_second_without_red_is_refused():
    sim, first, second, red = second_yellow_sim()
    third = sim.card(EventType.YELLOW_CARD, NAUTICO, "Ademir", 42, confirm=True)  # já expulso: sem vermelho
    for target in (second, first):
        with pytest.raises(DomainError) as info:
            check_void(sim.events, target.id, CTX)
        assert info.value.code == "void_breaks_sequence"
        assert info.value.details["cause"] == "second_yellow_without_red"
        assert info.value.details["sequence"] == third.sequence
    # cancelando o 3º antes, o 2º cai com o vermelho
    sim.void(third.id)
    assert sim.void(second.id).voided_ids == (second.id, red.id)
    assert player_key(NAUTICO, None, "Ademir") not in sim.state.sent_off


def test_void_direct_red_with_later_yellow_is_refused():
    sim = live_sim()
    sim.card(EventType.YELLOW_CARD, SPORT, "Durval", 10)
    red = sim.card(EventType.RED_CARD, SPORT, "Durval", 20)
    sim.card(EventType.YELLOW_CARD, SPORT, "Durval", 30, confirm=True)
    assert sim.last.derived == ()
    with pytest.raises(DomainError) as info:
        check_void(sim.events, red.id, CTX)
    assert info.value.details["cause"] == "second_yellow_without_red"


def test_period_pauses_track_suspensions_of_the_current_period():
    sim = live_sim()
    suspended = sim.status("suspend")
    assert sim.state.period_pauses == ((suspended.sequence, None),)
    resumed = sim.status("resume")
    assert sim.state.period_pauses == ((suspended.sequence, resumed.sequence),)
    again = sim.status("suspend")
    assert sim.state.period_pauses == ((suspended.sequence, resumed.sequence), (again.sequence, None))
    sim.status("resume")
    sim.post(EventType.HALF_TIME)  # período novo: zera as paradas
    assert sim.state.period_pauses == ()
    sim.post(EventType.SECOND_HALF_START)
    pause = sim.status("suspend")
    back = sim.status("resume")
    sim.void(back.id)  # cancelar a retomada reabre a parada
    assert sim.state.status == Status.SUSPENDED and sim.state.period_pauses == ((pause.sequence, None),)
    sim.status("cancel")
    assert sim.state.period_pauses == () and sim.state.period_started_seq is None


def test_minute_mode_depends_on_period():
    assert minute_mode("goal", "first_half") == "required"
    assert minute_mode("yellow_card", "half_time") == "optional"
    assert minute_mode("yellow_card", "penalties") == "optional"
    assert minute_mode("substitution", "extra_time") == "required"
    assert minute_mode("shootout_kick", "penalties") == "optional"
    assert minute_mode("match_end", "second_half") == "optional"
    assert minute_mode("suspended", "second_half") == "none"
    assert minute_mode("bicicleta", "first_half") == "none"


def test_text_and_name_payload_validation():
    sim = live_sim()
    rejects("invalid_payload", sim, EventType.VAR_REVIEW, minute=3, payload={"incident": 7, "decision": "b"})
    rejects("invalid_payload", sim, EventType.VAR_REVIEW, minute=3, payload={"incident": "a" * 281, "decision": "b"})
    rejects("invalid_payload", sim, EventType.YELLOW_CARD, minute=3, team_id=SPORT, payload={"player": 10})
    rejects("invalid_minute", sim, EventType.HALF_TIME, stoppage=2)  # acréscimo sem minuto
    event = sim.post(EventType.PENALTY_AWARDED, minute=4, team_id=SPORT, payload=None)
    assert event.payload == {}


def test_substitution_with_sent_off_player_coming_in():
    sim = live_sim()
    sim.card(EventType.RED_CARD, SPORT, "Durval", 10)
    error = rejects("confirmation_required", sim, EventType.SUBSTITUTION, team_id=SPORT, minute=12,
                    payload={"player_out": "Magrão", "player_in": "Durval"})
    assert warning_codes(error) == ["player_sent_off"]
