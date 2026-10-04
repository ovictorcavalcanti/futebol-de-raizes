"""Leitura (matches/selectors.py): formatos do CONTRACT §3–4 — EventOut, MatchOut
(resumo e detalhe), TieOut, catálogo, menu, página da competição, lista de partidas,
detalhe com `available` e corpos das respostas do operador."""

from __future__ import annotations

from datetime import timedelta

import pytest

from core import timeutils
from matches import selectors
from matches.models import Match, MatchBroadcast, MatchLineup, MatchLineupPlayer, MatchOfficial, MatchStat
from realtime.models import Outbox
from tests.factories import make_knockout, make_league, make_match, make_tie_matches, utc
from tests.test_services import Op

pytestmark = pytest.mark.django_db

TEAM_KEYS = {"id", "name", "short_name", "city", "color_primary", "color_secondary", "crest_url"}
EVENT_KEYS = {
    "id", "sequence", "type", "type_label", "kind", "icon", "period", "period_label", "period_short", "minute",
    "stoppage", "minute_label", "team_id", "team_side", "player", "payload", "annuls_event_id", "annulled",
    "derived", "score_after", "created_at",
}
GOAL_KEYS = {
    "event_id", "match_id", "team_id", "team_side", "player", "origin", "period", "minute", "stoppage",
    "minute_label", "score_after", "created_at",
}
MATCH_KEYS = {
    "id", "competition", "stage", "group", "round", "kickoff_at", "finished_at", "venue", "city", "status",
    "status_label", "status_note", "partial_info", "period", "period_label", "period_short", "period_started_at", "clock", "home", "away",
    "home_score", "away_score", "home_penalties", "away_penalties", "winner", "version", "tie", "goals", "cards",
    "red_cards",
}
DETAIL_KEYS = MATCH_KEYS | {"events", "lineups", "officials", "broadcasts", "stats", "attendance", "revenue_cents"}
TIE_KEYS = {
    "id", "legs", "extra_time", "position", "round", "team_a", "team_b", "aggregate", "winner_team_id",
    "decided_by", "decided_by_label", "complete",
}


def load(match):
    return Match.objects.select_related(*selectors.MATCH_RELATED).get(pk=match.pk)


@pytest.fixture
def league(db):
    return make_league(n_teams=4, team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"])


@pytest.fixture
def played(league, operator_user):
    """Partida ao vivo no 2T com gols (um anulado), pênalti, contra, cartões e 2º amarelo."""
    sport, nautico = league["teams"][0], league["teams"][1]
    match = make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now() - timedelta(minutes=90), round=league["rounds"][0])
    op = Op(match, operator_user, start=timeutils.now() - timedelta(minutes=89))
    ids = {}
    op.post("match_start")
    ids["g1"] = op.goal(sport, 10, "Zé Roberto", origin="penalty").event.id
    ids["g2"] = op.goal(nautico, 20, "Kayo Lima").event.id
    ids["annul"] = op.post("goal_annulled", minute=21, annuls_event_id=ids["g2"], payload={"reason": "Impedimento"}).event.id
    ids["own"] = op.goal(nautico, 30, "Rafael Thyere", origin="own_goal").event.id
    op.post("yellow_card", team_id=nautico.id, minute=33, payload={"player": "Thiago"})
    op.post("stoppage_time", minute=45, payload={"minutes": 2})
    op.post("half_time", minute=45, stoppage=2)
    op.post("second_half_start", after=15)
    second = op.post("yellow_card", team_id=nautico.id, minute=60, stoppage=None, payload={"player": "Thiago"})
    ids["y2"], ids["red"] = second.event.id, second.derived[0].id
    ids["var"] = op.post("var_review", minute=62, payload={"incident": "Pênalti?", "decision": "Segue"}).event.id
    return {"match": match, "ids": ids, "op": op, **league}


# --- EventOut / MatchOut ------------------------------------------------------------------


def test_chronological_orders_by_clock_and_keeps_minuteless_events_in_place():
    from types import SimpleNamespace as Row

    rows = [
        Row(sequence=1, period=None, minute=None, stoppage=None, name="atrasado"),
        Row(sequence=2, period="first_half", minute=0, stoppage=None, name="início"),
        Row(sequence=3, period="first_half", minute=40, stoppage=None, name="gol 40"),
        Row(sequence=4, period="first_half", minute=30, stoppage=None, name="gol 30 (lançado depois)"),
        Row(sequence=5, period="first_half", minute=45, stoppage=2, name="fim 1T"),
        Row(sequence=6, period="half_time", minute=None, stoppage=None, name="troca no intervalo"),
        Row(sequence=7, period="second_half", minute=45, stoppage=None, name="início 2T"),
        Row(sequence=8, period="second_half", minute=None, stoppage=None, name="suspenso"),
        Row(sequence=9, period="second_half", minute=46, stoppage=None, name="gol 46"),
    ]
    assert [row.name for row in selectors.chronological(reversed(rows))] == [
        "atrasado", "início", "gol 30 (lançado depois)", "gol 40", "fim 1T", "troca no intervalo",
        "início 2T", "suspenso", "gol 46",
    ]


def test_late_goal_takes_its_minute_in_the_timeline_and_the_score(league, operator_user):
    sport, nautico = league["teams"][0], league["teams"][1]
    match = make_match(league["stage"], sport, nautico, kickoff_at=timeutils.now() - timedelta(minutes=50), round=league["rounds"][0])
    op = Op(match, operator_user, start=timeutils.now() - timedelta(minutes=49))
    op.post("match_start")
    late_entry = op.goal(sport, 40, "Zé Roberto").event.id
    earlier = op.post("goal", team_id=nautico.id, minute=30, payload={"player": "Kayo Lima"}, confirm=True).event.id
    detail = selectors.serialize_match(load(match), True)
    assert [e["minute"] for e in detail["events"]] == [0, 30, 40]
    scores = {e["id"]: e["score_after"] for e in detail["events"] if e["type"] == "goal"}
    assert scores == {earlier: {"home": 0, "away": 1}, late_entry: {"home": 1, "away": 1}}
    assert [g["event_id"] for g in detail["goals"]] == [earlier, late_entry]


def test_event_out_shape_labels_icons_and_flags(played):
    match, ids = load(played["match"]), played["ids"]
    detail = selectors.serialize_match(match, detail=True)
    events = {event["id"]: event for event in detail["events"]}
    assert all(set(event) == EVENT_KEYS for event in events.values())

    g1 = events[ids["g1"]]
    assert g1["type_label"] == "Gol" and g1["kind"] == "game" and g1["icon"] == "ball-penalty"
    assert g1["period"] == "first_half" and g1["period_label"] == "1º tempo" and g1["period_short"] == "1T"
    assert g1["minute_label"] == "10'" and g1["team_side"] == "home"
    assert g1["player"] == {"id": None, "name": "Zé Roberto"}
    assert g1["score_after"] == {"home": 1, "away": 0} and not g1["annulled"] and not g1["derived"]

    g2 = events[ids["g2"]]
    assert g2["annulled"] and g2["score_after"] is None and g2["team_side"] == "away"
    annul = events[ids["annul"]]
    assert annul["annuls_event_id"] == ids["g2"] and annul["icon"] == "ball-x" and annul["player"]["name"] == "Kayo Lima"

    own = events[ids["own"]]
    assert own["icon"] == "ball-own" and own["score_after"] == {"home": 1, "away": 1}
    red = events[ids["red"]]
    assert red["derived"] and red["icon"] == "card-second-yellow" and red["payload"]["reason"] == "second_yellow"
    var = events[ids["var"]]
    assert var["team_id"] is None and var["team_side"] is None and var["minute_label"] == "62'"

    half = next(event for event in detail["events"] if event["type"] == "half_time")
    assert half["kind"] == "structural" and half["minute_label"] == "45+2'" and half["created_at"].endswith("Z")
    assert [event["sequence"] for event in detail["events"]] == sorted(event["sequence"] for event in detail["events"])


def test_match_out_compact_and_detail(played):
    match = load(played["match"])
    compact = selectors.serialize_match(match)
    assert set(compact) == MATCH_KEYS
    assert compact["competition"] == {
        "id": played["competition"].id, "name": played["competition"].name,
        "slug": played["competition"].slug, "short_name": played["competition"].short_name,
    }
    assert compact["stage"] == {"id": played["stage"].id, "name": "1ª fase", "format": "league"}
    assert compact["group"] == {"id": played["group"].id, "name": "Tabela"}
    assert compact["round"] == {"id": played["rounds"][0].id, "number": 1, "name": "Rodada 1"}
    assert set(compact["home"]) == TEAM_KEYS and compact["home"]["name"] == "Sport"
    assert compact["status"] == "live" and compact["status_label"] == "Ao vivo"
    assert compact["period"] == "second_half" and compact["period_label"] == "2º tempo" and compact["period_short"] == "2T"
    assert compact["clock"] == {"running": True, "offset": 45, "regular_end": 90, "stoppage_announced": None, "paused_at": None}
    assert (compact["home_score"], compact["away_score"]) == (1, 1)
    assert compact["home_penalties"] is None and compact["winner"] is None and compact["tie"] is None
    assert [set(goal) for goal in compact["goals"]] == [GOAL_KEYS, GOAL_KEYS]
    assert [goal["origin"] for goal in compact["goals"]] == ["penalty", "own_goal"]
    assert compact["cards"] == {"home": {"yellow": 0, "red": 0}, "away": {"yellow": 2, "red": 1}}
    assert compact["red_cards"] == [{"team_side": "away", "player": "Thiago", "minute_label": "60'"}]
    assert compact["version"] == match.version

    detail = selectors.serialize_match(match, detail=True)
    assert set(detail) == DETAIL_KEYS
    assert detail["lineups"] == {"home": None, "away": None}
    assert detail["officials"] == [] and detail["broadcasts"] == [] and detail["stats"] == []
    assert detail["attendance"] is None and detail["revenue_cents"] is None


def test_clock_is_null_outside_running_periods(league, operator_user):
    match = make_match(league["stage"], league["teams"][2], league["teams"][3], kickoff_at=timeutils.now() + timedelta(hours=1))
    assert selectors.serialize_match(load(match))["clock"] is None
    op = Op(match, operator_user)
    op.post("match_start")
    op.post("half_time")
    out = selectors.serialize_match(load(match))
    assert out["period"] == "half_time" and out["clock"] is None and out["period_short"] == "INT"


def test_detail_enrichment(league):
    sport, nautico = league["teams"][0], league["teams"][1]
    match = make_match(league["stage"], sport, nautico, kickoff_at=utc(2026, 10, 3, 19))
    home = MatchLineup.objects.create(match=match, team=sport, formation="4-3-3", coach="Mano")
    MatchLineupPlayer.objects.create(lineup=home, name="Caíque França", number=1, starter=True, order=1, position="GK")
    MatchLineupPlayer.objects.create(lineup=home, name="Lucas Arcanjo", number=14, starter=False, order=1, position="MF")
    MatchOfficial.objects.create(match=match, role="referee", name="Anderson Bezerra", state="PE")
    MatchBroadcast.objects.create(match=match, name="TV Capibaribe", url="https://example.com", kind="open_tv")
    MatchStat.objects.create(match=match, team=nautico, key="shots", value=9)
    MatchStat.objects.create(match=match, team=sport, key="possession", value=58)
    MatchStat.objects.create(match=match, team=nautico, key="possession", value=42)
    Match.objects.filter(pk=match.pk).update(attendance=28417, revenue_cents=98765400)

    detail = selectors.serialize_match(load(match), detail=True)
    assert detail["lineups"]["home"] == {
        "formation": "4-3-3", "coach": "Mano",
        "starters": [{"name": "Caíque França", "number": 1, "position": "GK"}],
        "substitutes": [{"name": "Lucas Arcanjo", "number": 14, "position": "MF"}],
    }
    assert detail["lineups"]["away"] is None
    assert detail["officials"] == [{"role": "referee", "role_label": "Árbitro", "name": "Anderson Bezerra", "state": "PE"}]
    assert detail["broadcasts"] == [{"name": "TV Capibaribe", "url": "https://example.com", "kind": "open_tv", "kind_label": "TV aberta"}]
    assert detail["stats"] == [
        {"key": "possession", "label": "Posse de bola (%)", "home": 58, "away": 42},
        {"key": "shots", "label": "Finalizações", "home": None, "away": 9},
    ]
    assert (detail["attendance"], detail["revenue_cents"]) == (28417, 98765400)


def test_serialize_matches_detail_list_uses_prefetch(league, django_assert_max_num_queries):
    teams = league["teams"]
    for index in range(4):
        match = make_match(league["stage"], teams[index % 4], teams[(index + 1) % 4], kickoff_at=utc(2026, 10, 3, 19 + index))
        MatchOfficial.objects.create(match=match, role="referee", name=f"Árbitro {index}")
    with django_assert_max_num_queries(9):
        out = selectors.serialize_matches(Match.objects.all(), detail=True)
    assert len(out) == 4 and all(set(item) == DETAIL_KEYS for item in out)
    assert [item["officials"][0]["name"] for item in out] == [f"Árbitro {index}" for index in range(4)]


def test_tie_out_embedded_and_winner(league, operator_user):
    ko = make_knockout(league["season"], legs=2, extra_time=False, team_a=league["teams"][0], team_b=league["teams"][1])
    leg1, leg2 = make_tie_matches(ko["tie"], kickoff=timeutils.now() - timedelta(days=7))
    op = Op(leg1, operator_user)
    op.post("match_start")
    op.goal(ko["team_b"], 5)
    out = selectors.serialize_match(load(leg1))
    tie = out["tie"]
    assert set(tie) == TIE_KEYS | {"leg"} and list(tie)[:3] == ["id", "legs", "leg"]
    assert tie["leg"] == 1 and tie["legs"] == 2 and tie["round"] == {"id": ko["round"].id, "number": 1, "name": "Final"}
    assert tie["aggregate"] == {"team_a": 0, "team_b": 1}
    assert tie["winner_team_id"] is None and tie["decided_by"] is None and tie["decided_by_label"] is None
    assert not tie["complete"] and out["group"] is None
    op.post("half_time")
    op.post("second_half_start", after=15)
    op.post("match_end", after=50)
    assert selectors.serialize_match(load(leg1))["winner"] == "away"
    assert selectors.serialize_match(load(leg2))["tie"]["leg"] == 2


# --- Operador ---------------------------------------------------------------------------------


def test_match_detail_includes_available_and_cursor(played):
    detail = selectors.match_detail(played["match"].id)
    assert set(detail) == {"server_time", "timezone", "cursor", "match", "available"}
    assert detail["timezone"] == "America/Sao_Paulo" and detail["server_time"].endswith("Z")
    assert detail["cursor"] == Outbox.objects.order_by("-id").first().id
    assert set(detail["match"]) == DETAIL_KEYS
    available = detail["available"]
    assert "match_end" in available["events"] and "goal" in available["events"] and "half_time" not in available["events"]
    assert available["status"] == ["suspend"]
    with pytest.raises(Match.DoesNotExist):
        selectors.match_detail(987654)


def test_operator_response_payloads(played):
    op = played["op"]
    result = op.post("yellow_card", team_id=played["teams"][0].id, minute=70, payload={"player": "Zé"})
    body = selectors.post_payload(result)
    assert set(body) == {"event", "derived", "match", "available", "warnings", "replayed"}
    assert body["event"]["id"] == result.event.id and body["derived"] == [] and not body["replayed"]
    assert body["match"]["version"] == result.match.version
    replay = op.post("yellow_card", key=result.event.idempotency_key, team_id=played["teams"][0].id, minute=70, payload={"player": "Zé"})
    assert selectors.post_payload(replay)["replayed"]

    second = op.post("yellow_card", team_id=played["teams"][0].id, minute=72, payload={"player": "Zé"})
    body = selectors.post_payload(second)
    assert [event["type"] for event in body["derived"]] == ["red_card"] and body["derived"][0]["derived"]

    status = op.status("suspend")
    body = selectors.status_payload(status)
    assert set(body) == {"event", "match", "available", "replayed"}
    assert body["event"]["type"] == "suspended" and body["event"]["kind"] == "status"
    assert body["available"]["status"] == ["resume", "cancel"]

    outcome = op.void(second.event.id)
    body = selectors.void_payload(outcome)
    assert body["voided"] == [second.event.id, second.derived[0].id] and not body["already"]
    assert second.event.id not in {event["id"] for event in body["match"]["events"]}


def test_catalog_payload():
    catalog = selectors.catalog_payload()
    assert set(catalog) == {"events", "status_actions", "periods", "statuses"}
    goal = next(spec for spec in catalog["events"] if spec["type"] == "goal")
    assert set(goal) == {"type", "label", "kind", "icon", "minute", "periods", "fields"}
    assert goal["periods"] == [
        "first_half", "half_time", "second_half", "extra_time", "extra_half_time", "extra_second_half",
    ] and goal["minute"] == "required"
    origin = next(item for item in goal["fields"] if item["name"] == "payload.origin")
    assert origin == {
        "name": "payload.origin", "kind": "choice", "label": "Origem", "required": False,
        "choices": [["open_play", "Jogada"], ["penalty", "Pênalti"], ["own_goal", "Contra"]],
    }
    assert catalog["status_actions"][0] == {"action": "delay", "label": "Marcar atraso"}
    assert {"key": "half_time", "label": "Intervalo", "short": "INT"} in catalog["periods"]
    assert {"key": "suspended", "label": "Suspenso"} in catalog["statuses"]
    reschedule = next(spec for spec in catalog["events"] if spec["type"] == "rescheduled")
    assert reschedule["kind"] == "status" and reschedule["fields"][0]["kind"] == "datetime"


# --- Competições e listas --------------------------------------------------------------------


def test_competitions_menu_in_position_order():
    second = make_league(name="Série A2", slug="serie-a2", position=2)
    first = make_league(name="Pernambucano", slug="pernambucano", position=1)
    menu = selectors.competitions_menu()
    assert [item["slug"] for item in menu["competitions"]] == ["pernambucano", "serie-a2"]
    assert menu["competitions"][0] == {
        "id": first["competition"].id, "name": "Pernambucano", "slug": "pernambucano", "short_name": "", "position": 1,
    }
    assert second["competition"].id in {item["id"] for item in menu["competitions"]}


def test_competition_payload_current_stage_round_and_params(league, operator_user):
    teams, rounds, stage = league["teams"], league["rounds"], league["stage"]
    past = make_match(stage, teams[0], teams[1], kickoff_at=timeutils.now() - timedelta(days=7), round=rounds[0])
    make_match(stage, teams[2], teams[3], kickoff_at=timeutils.now() - timedelta(days=7), round=rounds[0])
    op = Op(past, operator_user, start=timeutils.now() - timedelta(days=7))
    for type_ in ("match_start", "half_time", "second_half_start", "match_end"):
        op.post(type_, after=20)
    Op(Match.objects.get(round=rounds[0], home_team=teams[2]), operator_user).status("cancel")
    upcoming = make_match(stage, teams[0], teams[2], kickoff_at=timeutils.now() + timedelta(days=1), round=rounds[1])
    ko = make_knockout(league["season"], legs=1, extra_time=True, team_a=teams[0], team_b=teams[3], name="Final", position=2)
    (final,) = make_tie_matches(ko["tie"], kickoff=timeutils.now() + timedelta(days=14))

    page = selectors.competition_payload(league["competition"].slug)
    assert set(page) == {"server_time", "timezone", "cursor", "competition", "season", "stages", "current_stage_id", "current_round_id", "stage"}
    assert page["season"] == {"id": league["season"].id, "year": 2026}
    assert [item["id"] for item in page["stages"]] == [stage.id, ko["stage"].id]
    assert page["stages"][0]["rounds"][1] == {"id": rounds[1].id, "number": 2, "name": "Rodada 2"}
    assert (page["current_stage_id"], page["current_round_id"]) == (stage.id, rounds[1].id)
    assert [item["id"] for item in page["stage"]["matches"]] == [upcoming.id]
    assert page["stage"]["standings"]["stage_id"] == stage.id and page["stage"]["ties"] == []

    by_round = selectors.competition_payload(league["competition"].slug, round_id=rounds[0].id)
    assert by_round["current_round_id"] == rounds[0].id and len(by_round["stage"]["matches"]) == 2

    knockout = selectors.competition_payload(league["competition"].slug, stage_id=ko["stage"].id)
    assert knockout["current_stage_id"] == ko["stage"].id and knockout["stage"]["standings"] is None
    (tie,) = knockout["stage"]["ties"]
    assert set(tie) == TIE_KEYS | {"matches"} and "leg" not in tie
    assert [item["id"] for item in tie["matches"]] == [final.id]
    assert [item["id"] for item in knockout["stage"]["matches"]] == [final.id]

    with pytest.raises(type(league["competition"]).DoesNotExist):
        selectors.competition_payload("nao-existe")
    with pytest.raises(type(stage).DoesNotExist):
        selectors.competition_payload(league["competition"].slug, stage_id=987654)
    with pytest.raises(type(rounds[0]).DoesNotExist):
        selectors.competition_payload(league["competition"].slug, stage_id=ko["stage"].id, round_id=rounds[0].id)


def test_competition_payload_falls_back_to_last_stage_and_round(league, operator_user):
    match = make_match(league["stage"], league["teams"][0], league["teams"][1], round=league["rounds"][0])
    Op(match, operator_user).status("cancel")
    page = selectors.competition_payload(league["competition"].slug)
    assert page["current_stage_id"] == league["stage"].id
    assert page["current_round_id"] == league["rounds"][-1].id
    assert page["stage"]["matches"] == []


def test_matches_list_filters(league, operator_user):
    teams, rounds = league["teams"], league["rounds"]
    a = make_match(league["stage"], teams[0], teams[1], kickoff_at=utc(2026, 10, 3, 2, 30), round=rounds[0])  # 02/10 23:30 BRT
    b = make_match(league["stage"], teams[2], teams[3], kickoff_at=utc(2026, 10, 3, 19), round=rounds[0])
    c = make_match(league["stage"], teams[0], teams[2], kickoff_at=utc(2026, 10, 10, 19), round=rounds[1])
    Op(c, operator_user).status("postpone")
    ids = lambda data: [item["id"] for item in data["matches"]]  # noqa: E731
    everything = selectors.matches_list()
    assert ids(everything) == [a.id, b.id, c.id] and everything["timezone"] == "America/Sao_Paulo"
    assert ids(selectors.matches_list(round_id=rounds[0].id)) == [a.id, b.id]
    assert ids(selectors.matches_list(date="2026-10-03")) == [b.id]
    assert ids(selectors.matches_list(date="2026-10-02")) == [a.id]
    assert ids(selectors.matches_list(status="postponed")) == [c.id]
    assert ids(selectors.matches_list(status="scheduled,postponed", stage_id=league["stage"].id)) == [a.id, b.id, c.id]
    with pytest.raises(ValueError):
        selectors.matches_list(status="playing")
    with pytest.raises(ValueError):
        selectors.matches_list(date="03/10/2026")
