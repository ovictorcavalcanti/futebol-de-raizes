"""Seed (`manage.py seed`) e simulador (`manage.py simulate_match`).

O seed lança tudo pelos serviços; aqui se confere o resultado pelas mesmas leituras
das páginas (home, classificação, confrontos). O simulador roda com a pausa trocada
por uma função vazia.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Sum

from competitions.management.commands.seed import SEED_SLUGS
from competitions.models import Competition, Stage, StageCriterion, StandingZone, Team
from core.locks import locked_atomic
from core import timeutils
from matches import selectors
from matches.management.commands import simulate_match
from matches.models import Match, MatchEvent, MatchLineup, MatchLineupPlayer, MatchOfficial, MatchStat, Tie
from observability.models import AuditLog
from realtime.models import Outbox
from realtime.outbox import enqueue
from standings.models import Standing
from tests.factories import make_knockout, make_league, make_match, make_tie_matches

pytestmark = pytest.mark.django_db

SEED_DAY = date(2026, 5, 9)


def run_seed(*args) -> str:
    out = StringIO()
    call_command("seed", *args, stdout=out)
    return out.getvalue()


def played_sum(stage, kind="official") -> int:
    return Standing.objects.filter(group__stage=stage, kind=kind).aggregate(total=Sum("played"))["total"] or 0


def check_seeded_day(payload) -> None:
    """Home do dia do seed: as duas competições, jogos ao vivo e últimos gols."""
    competitions = payload["competitions"]
    assert [item["slug"] for item in competitions] == ["pernambucano-raiz", "copa-pernambuco"]
    league_block = competitions[0]["stages"][0]
    statuses = [match["status"] for match in league_block["matches"]]
    assert len(statuses) == 10
    assert statuses.count("finished") == 2 and statuses.count("live") == 2 and statuses.count("scheduled") == 6
    live = {match["period"]: match for match in league_block["matches"] if match["status"] == "live"}
    assert set(live) == {"first_half", "second_half"}
    classic = live["second_half"]
    assert (classic["home"]["short_name"], classic["away"]["short_name"]) == ("SPT", "NAU")
    assert (classic["home_score"], classic["away_score"]) == (2, 1)
    assert classic["clock"]["running"] is True
    assert classic["red_cards"] and classic["red_cards"][0]["team_side"] == "away"
    assert league_block["standings"]["legend"][0]["name"] == "Classificados"
    copa = competitions[1]["stages"]
    assert [block["format"] for block in copa] == ["knockout"]
    final = copa[0]["matches"][0]
    assert final["tie"]["round"]["name"] == "Final" and final["tie"]["legs"] == 1
    assert payload["latest_goals"]
    assert {goal["match"]["competition"]["slug"] for goal in payload["latest_goals"]} == {"pernambucano-raiz"}
    times = [goal["created_at"] for goal in payload["latest_goals"]]
    assert times == sorted(times, reverse=True)


def check_structure() -> None:
    league = Competition.objects.get(slug="pernambucano-raiz")
    stage = league.seasons.get().stages.get()
    assert (stage.points_win, stage.points_draw, stage.points_loss) == (3, 1, 0)
    assert list(StageCriterion.objects.filter(stage=stage).order_by("position").values_list("key", flat=True)) == [
        "points", "wins", "goal_difference", "goals_for", "head_to_head"
    ]
    zones = list(StandingZone.objects.filter(stage=stage).order_by("position_from").values_list("name", "color", "position_from", "position_to"))
    assert zones == [("Classificados", "#1B7F3B", 1, 4), ("Rebaixados", "#B3261E", 17, 20)]
    group = stage.groups.get()
    assert group.group_teams.count() == 20
    assert Match.objects.filter(stage=stage).count() == 190
    assert stage.rounds.count() == 19
    # Classificação coerente com os jogos: cada jogo encerrado conta para os dois times.
    finished = Match.objects.filter(stage=stage, status="finished").count()
    assert finished == 42
    assert played_sum(stage) == 2 * finished
    live_count = Match.objects.filter(stage=stage, status="live").count()
    assert played_sum(stage, "live") == 2 * (finished + live_count)

    copa = Competition.objects.get(slug="copa-pernambuco")
    groups_stage, knockout = copa.seasons.get().stages.order_by("position")
    assert groups_stage.format == "groups" and knockout.format == "knockout"
    assert sorted(groups_stage.groups.values_list("name", flat=True)) == ["Grupo A", "Grupo B"]
    assert Match.objects.filter(stage=groups_stage, status="finished").count() == 12
    assert played_sum(groups_stage) == 24
    semis = list(Tie.objects.filter(stage=knockout, round__name="Semifinal").order_by("position"))
    assert [(tie.legs, tie.extra_time) for tie in semis] == [(2, True), (2, True)]
    assert all(tie.winner_team_id for tie in semis)
    assert sorted(tie.decided_by for tie in semis) == ["aggregate", "extra_time"]
    final = Tie.objects.get(stage=knockout, round__name="Final")
    assert (final.legs, final.extra_time) == (1, False)
    assert {final.team_a_id, final.team_b_id} == {tie.winner_team_id for tie in semis}

    # Tudo pelos serviços, com autor; o 2º amarelo gerou o vermelho automático.
    assert set(MatchEvent.objects.values_list("source", flat=True)) == {"script", "system"}
    classic = Match.objects.get(home_team__name="Sport", away_team__name="Náutico", round__number=5)
    types = list(classic.events.values_list("type", flat=True))
    assert "goal_annulled" in types and "var_review" in types
    red = classic.events.get(type="red_card")
    assert red.payload["reason"] == "second_yellow" and red.source == "system"
    # Enriquecimento dos jogos do dia.
    today_matches = Match.objects.filter(round__number=5, stage=stage)
    assert MatchOfficial.objects.filter(match__in=today_matches, role="var").count() == 10
    started = today_matches.exclude(status="scheduled")
    assert MatchLineup.objects.filter(match__in=started).count() == 8
    lineup = MatchLineup.objects.filter(match=classic).first()
    assert lineup.entries.filter(starter=True).count() == 11 and lineup.entries.filter(starter=False).count() == 5
    assert MatchStat.objects.filter(match__in=started).exists()
    assert all(match.attendance and match.revenue_cents for match in started)
    # Gols dos jogos com escalação saíram de quem estava escalado (só nomes: jogador não tem cadastro).
    for goal in MatchEvent.objects.filter(match__in=started, type="goal", voided_at__isnull=True).exclude(payload__origin="own_goal"):
        names = set(MatchLineupPlayer.objects.filter(lineup__match=goal.match, lineup__team_id=goal.team_id).values_list("name", flat=True))
        assert goal.payload["player"] in names


def test_seed_runs_on_empty_db_and_reruns_with_reset(monkeypatch):
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SEED_OPERATOR_PASSWORD", raising=False)
    output = run_seed()
    assert "raizes-admin-2026" in output and "raizes-operador-2026" in output
    for match in Match.objects.filter(status="live"):
        assert f"simulate_match {match.pk}" in output  # o aviso final aponta os jogos ao vivo
    User = get_user_model()
    admin_user, operator = User.objects.get(username="admin"), User.objects.get(username="operador")
    assert admin_user.is_staff and admin_user.check_password("raizes-admin-2026")
    assert admin_user.groups.filter(name="Administrador").exists()
    assert operator.is_staff and operator.groups.filter(name="Operador").exists()
    check_structure()
    check_seeded_day(selectors.home_payload())  # hoje, agora

    with pytest.raises(CommandError, match="--reset"):
        run_seed()

    counts = (Match.objects.count(), MatchEvent.objects.count(), Team.objects.count())
    output = run_seed("--reset", "--date", SEED_DAY.isoformat())
    assert "mantida" in output  # usuários existentes mantêm a senha
    assert (Match.objects.count(), MatchEvent.objects.count(), Team.objects.count()) == counts
    assert User.objects.get(username="admin").pk == admin_user.pk
    assert Competition.objects.filter(slug__in=["pernambucano-raiz", "copa-pernambuco"]).count() == 2
    check_structure()
    reference = datetime.combine(SEED_DAY, time(17, 41), tzinfo=timeutils.app_tz())
    check_seeded_day(selectors.home_payload(SEED_DAY, reference))
    season = Competition.objects.get(slug="pernambucano-raiz").seasons.get()
    assert season.year == 2026

    # --clear: apaga tudo o que o seed criou (outbox das partidas e fases inclusive), sem
    # recriar; usuários, auditoria e o que não é do seed ficam.
    other = make_league(n_teams=2, team_names=["Clube de Fora", "Outro de Fora"])
    outside = make_match(other["stage"], *other["teams"])
    with locked_atomic():
        enqueue("match", {"stage_id": other["stage"].pk, "competition_id": other["competition"].pk, "match": {"id": outside.pk}})
    seed_ids = set(Match.objects.filter(stage__season__competition__slug__in=SEED_SLUGS).values_list("id", flat=True))
    seed_stages = set(Stage.objects.filter(season__competition__slug__in=SEED_SLUGS).values_list("id", flat=True))
    assert Outbox.objects.filter(topic="match", payload__match__id__in=list(seed_ids)).exists()
    audit_rows = AuditLog.objects.count()
    output = run_seed("--clear")
    assert "Dados do seed apagados" in output and "Usuários e auditoria mantidos" in output
    assert not Competition.objects.filter(slug__in=SEED_SLUGS).exists()
    assert set(Match.objects.values_list("id", flat=True)) == {outside.pk}
    assert not MatchEvent.objects.exists() and not Standing.objects.filter(group__stage_id__in=seed_stages).exists()
    assert not Team.objects.filter(name="Sport").exists()
    assert Team.objects.filter(name="Clube de Fora").exists()
    assert not Outbox.objects.filter(topic="match", payload__match__id__in=list(seed_ids)).exists()
    assert not Outbox.objects.filter(topic="standings", payload__stage_id__in=list(seed_stages)).exists()
    for payload in Outbox.objects.filter(topic="goals").values_list("payload", flat=True):
        assert not {goal["match_id"] for goal in payload["latest_goals"]} & seed_ids
    assert Outbox.objects.filter(topic="match", payload__match__id=outside.pk).exists()
    assert User.objects.filter(username__in=["admin", "operador"]).count() == 2
    assert AuditLog.objects.count() == audit_rows
    assert "Nada a apagar" in run_seed("--clear")
    with pytest.raises(CommandError):
        run_seed("--clear", "--reset")


def test_seed_rejects_bad_date():
    with pytest.raises(CommandError, match="YYYY-MM-DD"):
        run_seed("--date", "09/05/2026")


# --- simulate_match ------------------------------------------------------------------------------


@pytest.fixture
def no_sleep(monkeypatch):
    pauses = []
    monkeypatch.setattr(simulate_match, "sleep", pauses.append)
    return pauses


def simulate(match, *args) -> str:
    out = StringIO()
    call_command("simulate_match", str(match.pk), "--speed", "600", *args, stdout=out)
    return out.getvalue()


def test_simulate_match_finishes_a_scheduled_match(operator_user, no_sleep):
    league = make_league(n_teams=2, team_names=["Sport", "Náutico"])
    sport, nautico = league["teams"]
    match = make_match(league["stage"], sport, nautico)
    output = simulate(match, "--seed", "3")
    match.refresh_from_db()
    assert match.status == "finished" and match.finished_at is not None
    assert "Início de jogo" in output and "Fim de jogo" in output and "Fim da simulação" in output
    events = MatchEvent.objects.filter(match=match)
    assert set(events.values_list("source", flat=True)) <= {"script", "system"}
    assert set(events.values_list("created_by__username", flat=True)) == {"operador"}
    assert no_sleep and abs(no_sleep[0] - 0.1) < 1e-9  # 600 min de jogo por min real → 0,1 s por minuto
    assert Standing.objects.filter(group=league["group"], kind="official", played=1).count() == 2


def test_simulate_match_until_minute_then_resume(operator_user, no_sleep):
    league = make_league(n_teams=2)
    home, away = league["teams"]
    match = make_match(league["stage"], home, away)
    simulate(match, "--seed", "5", "--until-minute", "30")
    match.refresh_from_db()
    assert (match.status, match.period) == ("live", "first_half")
    minutes = [minute for minute in match.events.values_list("minute", flat=True) if minute is not None]
    assert max(minutes) <= 30
    simulate(match, "--seed", "6")
    match.refresh_from_db()
    assert match.status == "finished"


def test_simulate_knockout_goes_to_extra_time_and_penalties(operator_user, no_sleep, monkeypatch):
    monkeypatch.setattr(simulate_match.LiveSimulation, "GOAL_RATE", {"home": 0.0, "away": 0.0})
    monkeypatch.setattr(simulate_match.LiveSimulation, "PENALTY_RATE", 0.0)
    league = make_league(n_teams=2)
    knockout = make_knockout(league["season"], legs=1, extra_time=True)
    match = make_tie_matches(knockout["tie"])[0]
    output = simulate(match, "--seed", "11")
    match.refresh_from_db()
    assert match.status == "finished"
    assert match.home_penalties is not None and match.home_penalties != match.away_penalties
    assert "Início da prorrogação" in output and "Início dos pênaltis" in output
    tie = Tie.objects.get(pk=knockout["tie"].pk)
    assert tie.winner_team_id is not None and tie.decided_by == "penalties"


@pytest.mark.parametrize("seed", range(12))
def test_shootout_plays_always_end_decided(seed):
    import random

    def totals(plays, start):
        scored = dict(start)
        for kick in plays:
            scored[kick.side] += int(kick.extra["scored"])
        return scored

    fresh = simulate_match.shootout_plays(random.Random(seed), "home")
    result = totals(fresh, {"home": 0, "away": 0})
    assert result["home"] != result["away"]
    sides = [kick.side for kick in fresh]
    assert sides[:2] == ["home", "away"]
    # Continuação de uma disputa começada (3 a 3 em quatro cobranças cada... e a vez do mandante).
    resumed = simulate_match.shootout_plays(
        random.Random(seed), "away", scored={"home": 3, "away": 3}, taken={"home": 4, "away": 4}
    )
    result = totals(resumed, {"home": 3, "away": 3})
    assert result["home"] != result["away"]
    assert resumed[0].side == "away"


def test_simulate_match_refuses_finished_match_and_unknown_user(operator_user, no_sleep):
    league = make_league(n_teams=2)
    home, away = league["teams"]
    match = make_match(league["stage"], home, away)
    with pytest.raises(CommandError, match="não existe"):
        call_command("simulate_match", str(match.pk), "--user", "ninguem", stdout=StringIO())
    simulate(match, "--seed", "1")
    out = StringIO()
    with pytest.raises(CommandError, match="já terminou"):
        call_command("simulate_match", str(match.pk), stdout=out)
    assert "Simulando" not in out.getvalue()  # recusa antes de anunciar a simulação


def test_simulate_match_resumes_a_suspended_match(operator_user, no_sleep):
    from matches import services
    from matches.domain import NewEvent

    league = make_league(n_teams=2)
    match = make_match(league["stage"], *league["teams"])
    services.post_event(match.id, operator_user, NewEvent(type="match_start"), idempotency_key="k1")
    services.change_status(match.id, operator_user, "suspend", idempotency_key="k2", reason="Chuva forte")
    output = simulate(match, "--seed", "2")
    match.refresh_from_db()
    assert match.status == "finished"
    assert "Retomado" in output


def test_simulate_match_interrupted_keeps_what_was_posted(operator_user, monkeypatch):
    calls = []

    def interrupt_after_a_while(seconds):
        calls.append(seconds)
        if len(calls) > 20:
            raise KeyboardInterrupt

    monkeypatch.setattr(simulate_match, "sleep", interrupt_after_a_while)
    league = make_league(n_teams=2)
    match = make_match(league["stage"], *league["teams"])
    output = simulate(match, "--seed", "3")
    match.refresh_from_db()
    assert "Simulação interrompida" in output and "Fim da simulação" in output
    assert match.status == "live" and match.events.exists()


def test_seed_kickoffs_never_land_in_the_past_near_midnight(monkeypatch):
    """Perto da meia-noite, os jogos "mais tarde" ficam depois de agora; logo depois da
    meia-noite, os jogos "já encerrados" terminam antes de agora."""
    from competitions.management.commands.seed import Seeder

    late = datetime.combine(SEED_DAY, time(23, 50), tzinfo=timeutils.app_tz())
    monkeypatch.setattr(timeutils, "now", lambda: late)
    seeder = Seeder(SEED_DAY, StringIO())
    seeder.ref_now = late
    for minutes in (0, 60, 90, 180):
        kickoff = seeder.later_today(timedelta(minutes=minutes))
        assert late < kickoff < seeder.day_end

    early = datetime.combine(SEED_DAY, time(0, 3), tzinfo=timeutils.app_tz())
    seeder.ref_now = early
    kickoff, scale = seeder.earlier_today(timedelta(hours=4, minutes=40))
    assert kickoff == seeder.day_start
    assert kickoff + timedelta(minutes=125 * scale) <= early
