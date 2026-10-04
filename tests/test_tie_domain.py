"""Testes do mata-mata em funções puras (matches/domain.py), sem banco.

Cobrem os dois confrontos da fase 5: ida e volta com prorrogação e jogo único
direto nos pênaltis, além de agregado decidido, confronto incompleto e a regra
de que gol fora não desempata.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from matches.domain import (
    DecidedBy,
    DomainError,
    Event,
    EventType,
    LegScore,
    MatchContext,
    MatchState,
    NewEvent,
    Period,
    Status,
    TieContext,
    TieInfo,
    TieLeg,
    apply_event,
    available_actions,
    check_void,
    compute_tie_result,
    derive_state,
    score,
    valid_goals,
)

SPORT, NAUTICO = 10, 20  # time A e time B do confronto


class Sim:
    """Partida do confronto em memória, lançada pelo domínio."""

    def __init__(self, ctx: MatchContext):
        self.ctx = ctx
        self.events: list[Event] = []
        self.state = MatchState()
        self._ids = 0

    def post(self, type_: str, **fields) -> Event:
        result = apply_event(self.state, self.events, NewEvent(type=type_, **fields), self.ctx)
        for event in (result.event, *result.derived):
            self._ids += 1
            self.events.append(replace(event, id=self._ids))
        self.state = result.state
        assert derive_state(self.events, self.ctx) == self.state
        return self.events[-1 - len(result.derived)]

    def rejects(self, code: str, type_: str, **fields) -> None:
        with pytest.raises(DomainError) as info:
            self.post(type_, **fields)
        assert info.value.code == code, info.value.message

    def goal(self, team: int, player: str, minute: int, stoppage: int | None = None) -> Event:
        return self.post(EventType.GOAL, team_id=team, minute=minute, stoppage=stoppage, payload={"player": player})

    def kick(self, team: int, player: str, scored: bool) -> Event:
        return self.post(EventType.SHOOTOUT_KICK, team_id=team, payload={"player": player, "scored": scored})

    def to_second_half(self) -> Sim:
        for event_type in (EventType.MATCH_START, EventType.HALF_TIME, EventType.SECOND_HALF_START):
            self.post(event_type)
        return self

    def types(self) -> list[str]:
        return [event.type for event in self.events if not event.voided]

    def leg(self, number: int) -> TieLeg:
        return TieLeg(
            number, self.ctx.home_team_id, self.ctx.away_team_id, self.state.status,
            self.state.home_score, self.state.away_score, self.state.home_penalties, self.state.away_penalties,
        )


def events_of(sim: Sim) -> list[str]:
    return available_actions(sim.state, sim.ctx, sim.events)["events"]


# --- Ida e volta com prorrogação --------------------------------------------------------

TWO_LEGS_ET = TieInfo(legs=2, extra_time=True, team_a_id=SPORT, team_b_id=NAUTICO)
FIRST_LEG = TieLeg(1, SPORT, NAUTICO, Status.FINISHED, 2, 1)  # Sport 2x1 Náutico na Ilha do Retiro


def second_leg_ctx(first: TieLeg = FIRST_LEG, *, extra_time: bool = True) -> MatchContext:
    tie = TieContext(
        legs=2, extra_time=extra_time, leg=2, team_a_id=SPORT, team_b_id=NAUTICO,
        other_legs=(LegScore(first.home_team_id, first.away_team_id, first.home_score, first.away_score),),
    )
    return MatchContext(home_team_id=NAUTICO, away_team_id=SPORT, tie=tie)  # volta nos Aflitos


def test_two_legs_with_extra_time_decided_in_extra_time():
    sim = Sim(second_leg_ctx()).to_second_half()
    sim.goal(NAUTICO, "Kieza", 70)  # Náutico 1x0: agregado 2-2
    assert (sim.state.home_score, sim.state.away_score) == (1, 0)

    actions = events_of(sim)
    assert actions[0] == "extra_time_start"
    assert "match_end" not in actions and "penalties_start" not in actions
    sim.rejects("tie_level_requires_extra_time", EventType.MATCH_END)
    sim.rejects("penalties_not_allowed", EventType.PENALTIES_START)

    start = sim.post(EventType.EXTRA_TIME_START)
    assert (start.period, start.minute) == ("extra_time", 90)
    assert sim.state.period == Period.EXTRA_TIME and sim.state.period_started_seq == start.sequence
    sim.rejects("extra_time_not_allowed", EventType.EXTRA_TIME_START)
    # o tempo normal fechou com o agregado igual: lance esquecido dele não entra mais
    sim.rejects("invalid_minute", EventType.GOAL, team_id=SPORT, minute=80, payload={"player": "Zé"})

    goal = sim.goal(SPORT, "Diego Souza", 105, 1)  # gol da prorrogação entra no placar
    assert goal.period == "extra_time"
    assert (sim.state.home_score, sim.state.away_score) == (1, 1)
    # A prorrogação tem dois tempos e intervalo: o fim de jogo só vem no 2º tempo dela.
    assert events_of(sim)[0] == "extra_half_time" and "match_end" not in events_of(sim)
    sim.rejects("invalid_transition", EventType.MATCH_END)
    sim.rejects("invalid_transition", EventType.EXTRA_SECOND_HALF_START)
    interval = sim.post(EventType.EXTRA_HALF_TIME, minute=105, stoppage=1)
    assert (interval.period, interval.minute, interval.stoppage) == ("extra_time", 105, 1)
    assert sim.state.period == Period.EXTRA_HALF_TIME
    assert events_of(sim)[0] == "extra_second_half_start" and "goal" in events_of(sim)  # gol esquecido do 1º tempo dela
    sim.rejects("invalid_minute", EventType.GOAL, team_id=SPORT, minute=80, payload={"player": "Zé"})  # tempo normal fechado
    sim.rejects("invalid_transition", EventType.MATCH_END)
    second = sim.post(EventType.EXTRA_SECOND_HALF_START)
    assert (second.period, second.minute) == ("extra_second_half", 105)
    assert sim.state.period == Period.EXTRA_SECOND_HALF and sim.state.period_started_seq == second.sequence
    late = sim.goal(SPORT, "Hernane", 118)
    assert late.period == "extra_second_half" and (sim.state.home_score, sim.state.away_score) == (1, 2)
    assert events_of(sim)[0] == "match_end" and "penalties_start" not in events_of(sim)

    end = sim.post(EventType.MATCH_END)
    assert (end.period, end.minute) == ("extra_second_half", 120)
    assert sim.state.status == Status.FINISHED and sim.state.home_penalties is None

    result = compute_tie_result(TWO_LEGS_ET, [FIRST_LEG, sim.leg(2)], sim.types())
    assert (result.aggregate_a, result.aggregate_b) == (4, 2)
    assert result.winner_team_id == SPORT
    assert result.decided_by == DecidedBy.EXTRA_TIME and result.complete


def test_extra_time_still_level_goes_to_penalties():
    sim = Sim(second_leg_ctx()).to_second_half()
    sim.goal(NAUTICO, "Kieza", 70)
    sim.rejects("penalties_not_allowed", EventType.PENALTIES_START)  # com prorrogação, ela vem antes
    sim.post(EventType.EXTRA_TIME_START)
    sim.rejects("penalties_not_allowed", EventType.PENALTIES_START)  # só ao fim do 2º tempo dela
    sim.post(EventType.EXTRA_HALF_TIME)
    sim.post(EventType.EXTRA_SECOND_HALF_START)
    sim.rejects("tie_level_requires_penalties", EventType.MATCH_END)
    assert events_of(sim)[0] == "penalties_start"
    start = sim.post(EventType.PENALTIES_START)
    assert (start.period, start.minute) == ("penalties", 120)
    assert (sim.state.home_penalties, sim.state.away_penalties) == (0, 0)
    sim.kick(NAUTICO, "Kieza", True)
    sim.kick(SPORT, "Diego Souza", False)
    end = sim.post(EventType.MATCH_END)
    assert (end.period, end.minute) == ("penalties", 120)

    result = compute_tie_result(TWO_LEGS_ET, [FIRST_LEG, sim.leg(2)], sim.types())
    assert (result.aggregate_a, result.aggregate_b) == (2, 2)
    assert result.winner_team_id == NAUTICO and result.decided_by == DecidedBy.PENALTIES and result.complete


def test_decisive_leg_with_aggregate_decided_ends_normally():
    sim = Sim(second_leg_ctx()).to_second_half()
    assert events_of(sim)[0] == "match_end"  # Sport já leva 2-1 no agregado
    sim.rejects("extra_time_not_allowed", EventType.EXTRA_TIME_START)
    sim.rejects("penalties_not_allowed", EventType.PENALTIES_START)
    sim.goal(NAUTICO, "Kieza", 60)  # 2-2
    sim.goal(NAUTICO, "Bruno", 88)  # Náutico vira o agregado: 3-2
    sim.post(EventType.MATCH_END)

    result = compute_tie_result(TWO_LEGS_ET, [FIRST_LEG, sim.leg(2)], sim.types())
    assert (result.aggregate_a, result.aggregate_b) == (2, 3)
    assert result.winner_team_id == NAUTICO and result.decided_by == DecidedBy.AGGREGATE and result.complete


def test_first_leg_is_never_decisive():
    ctx = MatchContext(SPORT, NAUTICO, tie=TieContext(legs=2, extra_time=True, leg=1, team_a_id=SPORT, team_b_id=NAUTICO))
    sim = Sim(ctx).to_second_half()
    assert events_of(sim)[0] == "match_end"  # 0x0 na ida pode acabar
    sim.rejects("extra_time_not_allowed", EventType.EXTRA_TIME_START)
    sim.rejects("penalties_not_allowed", EventType.PENALTIES_START)
    sim.post(EventType.MATCH_END)

    partial = compute_tie_result(TWO_LEGS_ET, [sim.leg(1)], sim.types())
    assert partial == partial.__class__(0, 0, None, None, False)


def test_away_goals_do_not_break_the_tie():
    first = TieLeg(1, SPORT, NAUTICO, Status.FINISHED, 1, 1)
    sim = Sim(second_leg_ctx(first, extra_time=False)).to_second_half()
    sim.goal(NAUTICO, "Kieza", 50)
    sim.goal(SPORT, "Hernane", 55)  # Sport tem 2 gols fora; agregado 2-2 mesmo assim
    sim.rejects("tie_level_requires_penalties", EventType.MATCH_END)
    sim.rejects("extra_time_not_allowed", EventType.EXTRA_TIME_START)
    sim.post(EventType.PENALTIES_START)

    unfinished = compute_tie_result(TWO_LEGS_ET, [first, sim.leg(2)], sim.types())
    assert unfinished.winner_team_id is None and not unfinished.complete


# --- Jogo único direto nos pênaltis -------------------------------------------------------

SINGLE_NO_ET = TieInfo(legs=1, extra_time=False, team_a_id=SPORT, team_b_id=NAUTICO)
SINGLE_CTX = MatchContext(SPORT, NAUTICO, tie=TieContext(legs=1, extra_time=False, leg=1, team_a_id=SPORT, team_b_id=NAUTICO))


def test_single_leg_without_extra_time_goes_straight_to_penalties():
    sim = Sim(SINGLE_CTX).to_second_half()
    sim.goal(SPORT, "Diego Souza", 47)
    sim.goal(NAUTICO, "Kieza", 89)
    assert events_of(sim)[0] == "penalties_start" and "extra_time_start" not in events_of(sim)
    sim.rejects("tie_level_requires_penalties", EventType.MATCH_END)
    sim.rejects("extra_time_not_allowed", EventType.EXTRA_TIME_START)

    start = sim.post(EventType.PENALTIES_START)
    assert (start.period, start.minute) == ("penalties", 90)
    assert sim.state.period == Period.PENALTIES and (sim.state.home_penalties, sim.state.away_penalties) == (0, 0)
    sim.rejects("invalid_period_for_event", EventType.GOAL, team_id=SPORT, minute=90, payload={"player": "Zé"})

    sim.kick(SPORT, "Diego Souza", True)
    sim.kick(NAUTICO, "Kieza", True)
    sim.kick(SPORT, "Durval", True)
    sim.kick(NAUTICO, "Bruno", True)
    assert (sim.state.home_penalties, sim.state.away_penalties) == (2, 2)
    assert "match_end" not in events_of(sim)
    sim.rejects("penalties_level", EventType.MATCH_END)

    sim.kick(SPORT, "Magrão", True)
    sim.kick(NAUTICO, "Ademir", False)
    assert (sim.state.home_penalties, sim.state.away_penalties) == (3, 2)
    assert events_of(sim) == ["match_end", "shootout_kick", "yellow_card", "red_card"]

    # cobranças não são gols
    assert (sim.state.home_score, sim.state.away_score) == (1, 1)
    assert score(sim.events, sim.ctx) == (1, 1) and len(valid_goals(sim.events)) == 2

    end = sim.post(EventType.MATCH_END)
    assert (end.period, end.minute) == ("penalties", 90)
    assert sim.state.status == Status.FINISHED and sim.state.period is None
    assert (sim.state.home_penalties, sim.state.away_penalties) == (3, 2)

    result = compute_tie_result(SINGLE_NO_ET, [sim.leg(1)], sim.types())
    assert (result.aggregate_a, result.aggregate_b) == (1, 1)
    assert result.winner_team_id == SPORT and result.decided_by == DecidedBy.PENALTIES and result.complete


def test_single_leg_won_in_regular_time():
    sim = Sim(SINGLE_CTX).to_second_half()
    sim.goal(NAUTICO, "Kieza", 80)
    sim.post(EventType.MATCH_END)
    result = compute_tie_result(SINGLE_NO_ET, [sim.leg(1)], sim.types())
    assert result == result.__class__(0, 1, NAUTICO, "aggregate", True)


def test_voiding_shootout_kick_reopens_penalties():
    sim = Sim(SINGLE_CTX).to_second_half()
    sim.post(EventType.PENALTIES_START)
    sim.kick(SPORT, "Diego Souza", True)
    kick = sim.kick(NAUTICO, "Kieza", True)
    result = check_void(sim.events, kick.id, sim.ctx)
    assert result.voided_ids == (kick.id,)
    assert (result.state.home_penalties, result.state.away_penalties) == (1, 0)


# --- compute_tie_result ----------------------------------------------------------------------


def test_aggregate_decided_without_extra_time():
    legs = [
        TieLeg(1, SPORT, NAUTICO, Status.FINISHED, 2, 0),
        TieLeg(2, NAUTICO, SPORT, Status.FINISHED, 1, 0),
    ]
    result = compute_tie_result(TWO_LEGS_ET, legs, ["match_start", "half_time", "second_half_start", "goal", "match_end"])
    assert (result.aggregate_a, result.aggregate_b) == (2, 1)
    assert result.winner_team_id == SPORT and result.decided_by == "aggregate" and result.complete


def test_incomplete_tie_includes_leg_in_progress():
    legs = [
        TieLeg(1, SPORT, NAUTICO, Status.FINISHED, 1, 0),
        TieLeg(2, NAUTICO, SPORT, Status.LIVE, 2, 0),
    ]
    result = compute_tie_result(TWO_LEGS_ET, legs, ["match_start"])
    assert (result.aggregate_a, result.aggregate_b) == (1, 2)
    assert result.winner_team_id is None and result.decided_by is None and not result.complete
    # volta ainda não cadastrada
    result = compute_tie_result(TWO_LEGS_ET, legs[:1], [])
    assert (result.aggregate_a, result.aggregate_b, result.complete) == (1, 0, False)


def test_level_aggregate_needs_penalty_winner():
    legs = [
        TieLeg(1, SPORT, NAUTICO, Status.FINISHED, 1, 0),
        TieLeg(2, NAUTICO, SPORT, Status.FINISHED, 1, 0),
    ]
    missing = compute_tie_result(TWO_LEGS_ET, legs, ["extra_time_start"])
    assert missing.winner_team_id is None and missing.decided_by is None and not missing.complete
    level = [legs[0], replace(legs[1], home_penalties=4, away_penalties=4)]
    assert not compute_tie_result(TWO_LEGS_ET, level, ["penalties_start"]).complete
    decided = [legs[0], replace(legs[1], home_penalties=4, away_penalties=5)]
    result = compute_tie_result(TWO_LEGS_ET, decided, ["extra_time_start", "penalties_start"])
    assert result.winner_team_id == SPORT and result.decided_by == "penalties" and result.complete


def test_cancelled_leg_does_not_count():
    legs = [
        TieLeg(1, SPORT, NAUTICO, Status.CANCELLED, 3, 0),
        TieLeg(1, SPORT, NAUTICO, Status.FINISHED, 0, 1),
    ]
    result = compute_tie_result(SINGLE_NO_ET, legs, [])
    assert (result.aggregate_a, result.aggregate_b) == (0, 1)
    assert result.winner_team_id == NAUTICO and result.complete


# --- Minuto na disputa de pênaltis ---------------------------------------------------------


def test_shootout_minute_is_the_minute_the_shootout_started():
    sim = Sim(SINGLE_CTX).to_second_half()
    sim.post(EventType.PENALTIES_START)
    sim.rejects("invalid_minute", EventType.SHOOTOUT_KICK, minute=120, team_id=SPORT,
                payload={"player": "Diego Souza", "scored": True})
    kick = sim.post(EventType.SHOOTOUT_KICK, minute=90, team_id=SPORT, payload={"player": "Diego Souza", "scored": "true"})
    assert (kick.minute, kick.payload["scored"]) == (90, True)
    sim.rejects("invalid_payload", EventType.SHOOTOUT_KICK, team_id=NAUTICO, payload={"player": "Kieza", "scored": "sim"})

    ctx = second_leg_ctx()
    with_et = Sim(ctx).to_second_half()
    with_et.goal(NAUTICO, "Kieza", 70)
    with_et.post(EventType.EXTRA_TIME_START)
    with_et.post(EventType.EXTRA_HALF_TIME)
    with_et.post(EventType.EXTRA_SECOND_HALF_START)
    with_et.post(EventType.PENALTIES_START)
    with_et.rejects("invalid_minute", EventType.YELLOW_CARD, minute=90, team_id=SPORT, payload={"player": "Durval"})
    card = with_et.post(EventType.YELLOW_CARD, minute=120, team_id=SPORT, payload={"player": "Durval"})
    assert (card.period, card.minute) == ("penalties", 120)


def test_voiding_penalties_start_with_kicks_breaks_sequence():
    sim = Sim(SINGLE_CTX).to_second_half()
    start = sim.post(EventType.PENALTIES_START)
    sim.kick(SPORT, "Diego Souza", True)
    with pytest.raises(DomainError) as info:
        check_void(sim.events, start.id, sim.ctx)
    assert info.value.code == "void_breaks_sequence" and info.value.details["cause"] == "invalid_period_for_event"


def test_voiding_leveling_goal_after_extra_time_started_breaks_sequence():
    sim = Sim(second_leg_ctx()).to_second_half()
    goal = sim.goal(NAUTICO, "Kieza", 70)
    sim.post(EventType.EXTRA_TIME_START)
    with pytest.raises(DomainError) as info:
        check_void(sim.events, goal.id, sim.ctx)
    assert info.value.details["cause"] == "extra_time_not_allowed"
