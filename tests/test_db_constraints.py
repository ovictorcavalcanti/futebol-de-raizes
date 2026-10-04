"""Restrições do plano garantidas pelo banco ("Restrições que sustentam as regras"),
não só pelo `clean()` do admin: um jogo por (confronto, ida/volta), `leg` ≤
`ties.legs`, grupo/rodada/confronto da mesma fase da partida, mata-mata ⇔ confronto,
fase com tabela ⇒ grupo e zonas da legenda que não se sobrepõem. Escritas que pulam
o `clean()` (seed, shell, dois "Salvar" simultâneos) são recusadas com IntegrityError.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction

from competitions.models import Group, Round, Stage, StandingZone
from matches.models import Match, Tie
from tests.factories import make_knockout, make_league, make_match, make_rounds, make_stage, make_team, make_tie_matches, utc

pytestmark = pytest.mark.django_db


def rejected(write) -> str:
    """Roda `write` num savepoint e devolve a mensagem do IntegrityError (falha se passar)."""
    with pytest.raises(IntegrityError) as info:
        with transaction.atomic():
            write()
            connection.check_constraints()  # restrições DEFERRED conferidas aqui (como no commit)
    return str(info.value)


@pytest.fixture
def league(db):
    return make_league(
        n_teams=4,
        zones=[
            {"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 2},
            {"name": "Rebaixados", "color": "#B3261E", "position_from": 4, "position_to": 4},
        ],
    )


@pytest.fixture
def knockout(league):
    return make_knockout(league["season"], legs=1)


# --- Partida de confronto ---------------------------------------------------------------------


def test_second_match_for_the_same_tie_leg_is_rejected(knockout):
    tie = knockout["tie"]
    make_tie_matches(tie)
    message = rejected(lambda: make_match(tie.stage, tie.team_b, tie.team_a, round=tie.round, tie=tie, leg=1))
    assert "uniq_match_tie_leg" in message
    assert Match.objects.filter(tie=tie).count() == 1


def test_leg_beyond_tie_legs_is_rejected(knockout):
    tie = knockout["tie"]  # jogo único
    make_tie_matches(tie)
    message = rejected(lambda: make_match(tie.stage, tie.team_b, tie.team_a, round=tie.round, tie=tie, leg=2))
    assert "número de jogos" in message


def test_knockout_match_needs_a_tie(knockout):
    tie = knockout["tie"]
    message = rejected(lambda: make_match(tie.stage, tie.team_a, tie.team_b, round=tie.round))
    assert "precisa de um confronto" in message


def test_match_with_tie_of_another_stage_is_rejected(league, knockout):
    other_ko = make_knockout(league["season"], legs=1, name="Outro mata-mata", position=3)
    tie = other_ko["tie"]
    message = rejected(lambda: make_match(knockout["stage"], tie.team_a, tie.team_b, tie=tie, leg=1))
    assert "confronto precisa ser da mesma fase" in message


# --- Grupo, rodada e formato da fase ----------------------------------------------------------


def test_league_match_needs_a_group(league):
    a, b = league["teams"][:2]
    message = rejected(
        lambda: Match.objects.create(stage=league["stage"], home_team=a, away_team=b, kickoff_at=utc(2026, 10, 3, 19))
    )
    assert "precisa de um grupo" in message


def test_match_with_group_of_another_stage_is_rejected(league):
    other = make_league(n_teams=2)
    a, b = league["teams"][:2]
    message = rejected(lambda: make_match(league["stage"], a, b, group=other["group"]))
    assert "grupo precisa ser da mesma fase" in message


def test_match_with_round_of_another_stage_is_rejected(league):
    other = make_league(n_teams=2)
    a, b = league["teams"][:2]
    message = rejected(lambda: make_match(league["stage"], a, b, round=other["rounds"][0]))
    assert "rodada precisa ser da mesma fase" in message


def test_moving_a_match_to_another_stage_keeping_group_and_round_is_rejected(league):
    other = make_league(n_teams=2)
    match = make_match(league["stage"], *league["teams"][:2], round=league["rounds"][0])
    message = rejected(lambda: Match.objects.filter(pk=match.pk).update(stage=other["stage"], group=other["group"]))
    assert "rodada precisa ser da mesma fase" in message
    # com grupo e rodada da fase nova, a mudança passa
    Match.objects.filter(pk=match.pk).update(stage=other["stage"], group=other["group"], round=other["rounds"][0])


def test_group_and_round_with_matches_keep_their_stage(league):
    other = make_league(n_teams=2)
    make_match(league["stage"], *league["teams"][:2], round=league["rounds"][0])
    assert "grupo já tem partidas" in rejected(lambda: Group.objects.filter(pk=league["group"].pk).update(stage=other["stage"]))
    assert "rodada já tem partidas" in rejected(lambda: Round.objects.filter(pk=league["rounds"][0].pk).update(stage=other["stage"]))
    # rodada sem partidas muda de fase normalmente
    Round.objects.filter(pk=league["rounds"][1].pk).update(stage=other["stage"], number=99)


def test_stage_format_cannot_leave_its_matches_invalid(league, knockout):
    make_match(league["stage"], *league["teams"][:2])
    stage = league["stage"]
    stage.format = Stage.Format.KNOCKOUT
    assert "não vira mata-mata" in rejected(stage.save)
    ko_stage = Stage.objects.get(pk=knockout["stage"].pk)
    ko_stage.format = Stage.Format.GROUPS
    assert "só pode ser de mata-mata" in rejected(ko_stage.save)


# --- Confronto --------------------------------------------------------------------------------


def test_tie_legs_cannot_drop_below_the_registered_return_leg(league):
    ko = make_knockout(league["season"], legs=2)
    tie = ko["tie"]
    make_tie_matches(tie)
    message = rejected(lambda: Tie.objects.filter(pk=tie.pk).update(legs=1))
    assert "jogo de volta" in message
    # sem a volta cadastrada, vira jogo único
    Match.objects.filter(tie=tie, leg=2).delete()
    Tie.objects.filter(pk=tie.pk).update(legs=1)


def test_tie_with_matches_keeps_its_stage_and_round_must_match(league, knockout):
    tie = knockout["tie"]
    make_tie_matches(tie)
    other = make_stage(league["season"], Stage.Format.KNOCKOUT, name="Outra fase", position=5)
    (other_round,) = make_rounds(other, 1)
    assert "não muda de fase" in rejected(lambda: Tie.objects.filter(pk=tie.pk).update(stage=other, round=other_round))
    assert "rodada precisa ser da mesma fase do confronto" in rejected(lambda: Tie.objects.filter(pk=tie.pk).update(round=other_round))


def test_tie_only_in_knockout_stage(league):
    rnd = league["rounds"][0]
    message = rejected(
        lambda: Tie.objects.create(
            stage=league["stage"], round=rnd, legs=1, extra_time=False, team_a=make_team(), team_b=make_team()
        )
    )
    assert "só existe em fase de mata-mata" in message


def test_valid_structure_still_saves(league, knockout):
    """O caminho feliz continua passando: partida de grupo, ida e volta, edição de campos
    que não são de estrutura (o gatilho só olha fase/grupo/rodada/confronto/jogo)."""
    match = make_match(league["stage"], *league["teams"][:2], round=league["rounds"][0])
    match.venue = "Ilha do Retiro"
    match.save()
    ko = make_knockout(league["season"], legs=2, name="Semifinal", position=4)
    first, second = make_tie_matches(ko["tie"])
    assert {first.leg, second.leg} == {1, 2}
    Tie.objects.filter(pk=ko["tie"].pk).update(extra_time=True, position=3)


# --- Zonas da legenda -------------------------------------------------------------------------


def test_overlapping_zone_is_rejected(league):
    message = rejected(
        lambda: StandingZone.objects.create(stage=league["stage"], name="Sobreposta", color="#000000", position_from=2, position_to=3)
    )
    assert "zone_no_overlap" in message
    # mesma faixa em outra fase: sem problema
    other = make_league(n_teams=2)
    StandingZone.objects.create(stage=other["stage"], name="Classificados", color="#1B7F3B", position_from=1, position_to=2)
    connection.check_constraints()


def test_zone_reshuffle_inside_one_transaction_is_accepted(league):
    """O inline grava as zonas uma a uma: a troca 1-2/3-4 → 1-3/4-4 se sobrepõe no meio
    do caminho (1-3 × 3-4), e o banco só confere no fim da transação (DEFERRED)."""
    stage = league["stage"]
    StandingZone.objects.filter(stage=stage, name="Rebaixados").update(position_from=3, position_to=4)
    with transaction.atomic():
        StandingZone.objects.filter(stage=stage, name="Classificados").update(position_to=3)  # 1-3 × 3-4: sobreposta agora
        StandingZone.objects.filter(stage=stage, name="Rebaixados").update(position_from=4)  # 1-3 × 4-4: ok no fim
        connection.check_constraints()
    assert list(StandingZone.objects.filter(stage=stage).order_by("position_from").values_list("position_from", "position_to")) == [
        (1, 3),
        (4, 4),
    ]


@pytest.mark.django_db(transaction=True)
def test_zone_constraint_is_checked_at_real_commit():
    """Com commit de verdade (fora da transação do teste): a troca que se sobrepõe no meio
    do caminho é aceita e a sobreposição que fica é recusada no COMMIT."""
    data = make_league(n_teams=2, zones=[{"name": "A", "color": "#1B7F3B", "position_from": 1, "position_to": 4},
                                         {"name": "B", "color": "#B3261E", "position_from": 5, "position_to": 8}])
    stage = data["stage"]
    with transaction.atomic():
        StandingZone.objects.filter(stage=stage, name="B").update(position_from=4)  # 1-4 × 4-8 no meio
        StandingZone.objects.filter(stage=stage, name="A").update(position_to=3)  # 1-3 × 4-8 no commit
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StandingZone.objects.create(stage=stage, name="C", color="#000000", position_from=8, position_to=9)
    assert StandingZone.objects.filter(stage=stage).count() == 2
