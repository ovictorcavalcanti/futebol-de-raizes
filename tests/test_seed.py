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

from competitions.management.commands.seed import SEED_SLUGS, SLOTS
from competitions.models import Competition, Stage, StageCriterion, StandingZone, Team
from core.locks import locked_atomic
from core import timeutils
from matches import selectors
from matches.management.commands import simulate_match
from matches.models import Match, MatchEvent, MatchLineup, MatchLineupPlayer, MatchOfficial, MatchStat, Tie
from observability.models import AuditLog
from realtime.models import Outbox
from realtime.outbox import enqueue
from standings import services as standings_services
from standings.models import PointAdjustment, Ranking, Standing
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
    assert [item["slug"] for item in competitions] == list(SEED_SLUGS)  # todas com jogo no dia
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
    assert finished == 92  # 9 rodadas encerradas + 2 jogos do dia
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
    classic = Match.objects.get(home_team__name="Sport", away_team__name="Náutico", round__number=10)
    types = list(classic.events.values_list("type", flat=True))
    assert "goal_annulled" in types and "var_review" in types
    red = classic.events.get(type="red_card")
    assert red.payload["reason"] == "second_yellow" and red.source == "system"
    # Enriquecimento dos jogos do dia.
    today_matches = Match.objects.filter(round__number=10, stage=stage)
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
    # Horários razoáveis: todo jogo que não está ao vivo começa num dos horários do seed.
    for kickoff in Match.objects.exclude(status="live").values_list("kickoff_at", flat=True):
        assert kickoff.astimezone(timeutils.app_tz()).time() in SLOTS, kickoff
    check_other_competitions()


def check_other_competitions() -> None:
    """Jogos entre grupos, "posição nos grupos" com empate em tudo e turnos com classificação geral."""
    stage = Stage.objects.get(season__competition__slug="copa-do-nordeste-raiz")
    group_of = dict(stage.groups.values_list("group_teams__team_id", "name"))
    matches = list(Match.objects.filter(stage=stage))
    assert len(matches) == 16 and all(group_of[m.home_team_id] != group_of[m.away_team_id] for m in matches)
    assert Match.objects.filter(stage=stage, status="finished").count() == 12
    # cada jogo entre grupos conta para a tabela de cada time, no grupo dele
    assert played_sum(stage) == 2 * 12

    interior = Stage.objects.get(season__competition__slug="copa-do-interior")
    table = standings_services.stage_standings(interior, live=False)
    group_c = next(group for group in table["groups"] if group["name"] == "Grupo C")
    assert [row["rank"] for row in group_c["rows"]] == [1, 1, 3, 3] and [row["position"] for row in group_c["rows"]] == [1, 2, 3, 4]
    seconds = Ranking.objects.get(season__competition__slug="copa-do-interior")
    assert seconds.scope == "position" and seconds.group_position == 2
    assert len(standings_services.ranking_standings(seconds, live=False)["groups"][0]["rows"]) == 3
    assert StandingZone.objects.filter(stage=interior, ranking=seconds).exists()  # "Melhor 2º", condicional

    overall = Ranking.objects.get(season__competition__slug="serie-a2-raiz")
    assert overall.scope == "overall" and overall.show_on_competition and overall.stages.count() == 2
    rows = standings_services.ranking_standings(overall, live=False)["groups"][0]["rows"]
    assert len(rows) == 6 and all(row["played"] == 7 for row in rows)  # 5 do 1º turno + 2 do 2º
    punished = PointAdjustment.objects.get(stage__season__competition__slug="serie-a2-raiz")
    second_turn = standings_services.stage_standings(punished.stage, live=False)["groups"][0]["rows"]
    assert next(row for row in second_turn if row["team"]["id"] == punished.team_id)["points_adjustment"] == -3


def test_seed_runs_on_empty_db_and_reruns_with_reset(monkeypatch):
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SEED_OPERATOR_PASSWORD", raising=False)
    # "agora" fixo no fim da tarde de hoje: o dia do seed não depende da hora em que o teste roda
    afternoon = datetime.combine(timeutils.local_today(), time(17, 41), tzinfo=timeutils.app_tz())
    monkeypatch.setattr(timeutils, "now", lambda: afternoon)
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
    assert Competition.objects.filter(slug__in=SEED_SLUGS).count() == len(SEED_SLUGS)
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
    assert not Team.objects.filter(name__in=["Sport", "Bahia"]).exists()
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


def test_seed_kickoffs_follow_the_slots_and_skip_live_games_at_night(monkeypatch):
    """Jogos do dia nos horários do seed: os encerrados nos que já terminaram (ou de ontem),
    os agendados nos próximos (ou de amanhã), dois por horário; ao vivo só entre 15h e 22h30."""
    from competitions.management.commands.seed import Seeder

    def at(day, hour, minute):
        return datetime.combine(day, time(hour, minute), tzinfo=timeutils.app_tz())

    seeder = Seeder(SEED_DAY, StringIO())
    yesterday, tomorrow = SEED_DAY - timedelta(days=1), SEED_DAY + timedelta(days=1)

    seeder.ref_now = at(SEED_DAY, 17, 41)
    assert seeder.live_allowed()
    assert seeder.finished_kickoffs(2) == [at(SEED_DAY, 15, 0)] * 2
    assert seeder.scheduled_kickoffs(3) == [at(SEED_DAY, 18, 30)] * 2 + [at(SEED_DAY, 19, 0)]

    seeder.ref_now = at(SEED_DAY, 23, 50)  # tarde da noite: nada ao vivo, o resto fica para amanhã
    assert not seeder.live_allowed()
    assert seeder.finished_kickoffs(2) == [at(SEED_DAY, 21, 0)] * 2
    assert seeder.scheduled_kickoffs(2) == [at(tomorrow, 15, 0)] * 2
    assert seeder.final_kickoff() == at(tomorrow, 16, 0)

    seeder.ref_now = at(SEED_DAY, 0, 3)  # madrugada: os encerrados ficam de ontem, os agendados para a tarde
    assert not seeder.live_allowed()
    assert seeder.finished_kickoffs(2) == [at(yesterday, 21, 0)] * 2
    assert seeder.scheduled_kickoffs(2) == [at(SEED_DAY, 15, 0)] * 2
    assert seeder.final_kickoff() == at(SEED_DAY, 21, 0)
