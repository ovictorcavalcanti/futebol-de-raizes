"""Construtores de dados para os testes (sem dependência externa)."""

from datetime import datetime, timedelta, timezone as dt_timezone
from itertools import count

from competitions.models import Competition, Group, GroupTeam, Round, Season, Stage, StageCriterion, StandingZone, Team
from matches.models import Match, Tie

_seq = count(1)

DEFAULT_CRITERIA = ["points", "wins", "goal_difference", "goals_for", "head_to_head"]


def utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


def make_team(name=None, short_name=None, **kwargs):
    n = next(_seq)
    name = name or f"Time {n}"
    short = short_name or (name.replace(" ", "")[:3].upper() if name else f"T{n}")
    return Team.objects.create(name=name, short_name=short[:4], **kwargs)


def make_competition(name=None, slug=None, position=0, year=2026):
    n = next(_seq)
    comp = Competition.objects.create(name=name or f"Competição {n}", slug=slug or f"comp-{n}", position=position)
    season = Season.objects.create(competition=comp, year=year)
    return comp, season


def make_stage(season, fmt=Stage.Format.LEAGUE, name="1ª fase", position=1, criteria=None, zones=None, points=(3, 1, 0)):
    stage = Stage.objects.create(
        season=season, name=name, position=position, format=fmt,
        points_win=points[0], points_draw=points[1], points_loss=points[2],
    )
    if fmt != Stage.Format.KNOCKOUT:
        for i, key in enumerate(criteria or DEFAULT_CRITERIA, start=1):
            StageCriterion.objects.create(stage=stage, position=i, key=key)
        for zone in zones or []:
            StandingZone.objects.create(stage=stage, **zone)
    return stage


def make_rounds(stage, n=3):
    return [Round.objects.create(stage=stage, number=i, name=f"Rodada {i}") for i in range(1, n + 1)]


def make_league(n_teams=4, name=None, slug=None, position=0, criteria=None, zones=None, team_names=None):
    """Competição de pontos corridos com grupo único, times e rodadas."""
    comp, season = make_competition(name=name, slug=slug, position=position)
    stage = make_stage(season, Stage.Format.LEAGUE, criteria=criteria, zones=zones)
    group = stage.groups.get()
    names = team_names or [None] * n_teams
    teams = [make_team(nm) for nm in names]
    for team in teams:
        GroupTeam.objects.create(group=group, team=team)
    rounds = make_rounds(stage, max(1, len(teams) - 1))
    return {"competition": comp, "season": season, "stage": stage, "group": group, "teams": teams, "rounds": rounds}


def make_match(stage, home, away, kickoff_at=None, group=None, round=None, tie=None, leg=None, venue="Arena de Pernambuco", city="São Lourenço da Mata, PE"):
    if group is None and tie is None and stage.format != Stage.Format.KNOCKOUT:
        group = Group.objects.filter(stage=stage).first()
    return Match.objects.create(
        stage=stage, group=group, round=round, tie=tie, leg=leg,
        home_team=home, away_team=away,
        kickoff_at=kickoff_at or utc(2026, 10, 3, 19, 0),
        venue=venue, city=city,
    )


def make_knockout(season, legs=1, extra_time=False, team_a=None, team_b=None, name="Mata-mata", position=2, round_number=1):
    stage = Stage.objects.filter(season=season, format=Stage.Format.KNOCKOUT, name=name).first() or make_stage(
        season, Stage.Format.KNOCKOUT, name=name, position=position
    )
    rnd, _ = Round.objects.get_or_create(stage=stage, number=round_number, defaults={"name": "Final" if round_number == 1 else f"Rodada {round_number}"})
    team_a = team_a or make_team()
    team_b = team_b or make_team()
    tie = Tie.objects.create(stage=stage, round=rnd, position=Tie.objects.filter(stage=stage).count() + 1, legs=legs, extra_time=extra_time, team_a=team_a, team_b=team_b)
    return {"stage": stage, "round": rnd, "tie": tie, "team_a": team_a, "team_b": team_b}


def make_tie_matches(tie, kickoff=None):
    """Cria os jogos do confronto: ida (A em casa) e, se houver, volta (B em casa)."""
    kickoff = kickoff or utc(2026, 10, 3, 19, 0)
    matches = [make_match(tie.stage, tie.team_a, tie.team_b, kickoff_at=kickoff, round=tie.round, tie=tie, leg=1)]
    if tie.legs == 2:
        matches.append(make_match(tie.stage, tie.team_b, tie.team_a, kickoff_at=kickoff + timedelta(days=7), round=tie.round, tie=tie, leg=2))
    return matches
