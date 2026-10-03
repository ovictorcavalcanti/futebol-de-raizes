"""API pública `/public/v1` (fase 12 do plano).

"Pronto quando": um teste de contrato prova que gol anulado e tipos internos não
aparecem. Também: chave (`X-API-Key` → 401 `invalid_api_key`), limite de uso por
chave (429 `rate_limited` + `Retry-After`, headers `X-RateLimit-*`), cache HTTP
(ETag forte, 304), documentação OpenAPI sem chave, classificação oficial e ao vivo,
admin das chaves e o comando `create_api_key`.
"""

from __future__ import annotations

import hashlib
import io
import re
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import Client

from core import timeutils
from matches import services
from matches.domain import NewEvent
from matches.models import Match, MatchEvent
from observability.metrics import metrics
from observability.models import AuditLog
from public_api import throttle
from public_api.models import ApiKey, hash_key
from tests.factories import make_knockout, make_league, make_match, make_tie_matches

pytestmark = pytest.mark.django_db

BASE = "/public/v1"
# Chaves internas que nunca podem aparecer em nenhum nível da resposta pública.
FORBIDDEN_KEYS = {
    "source", "created_by", "idempotency_key", "voided", "voided_at", "voided_by", "sequence",
    "version", "payload", "cursor", "server_time", "annuls_event_id", "derived_from_sequence",
    "annulled", "derived", "reason", "player_id", "player_in_id", "player_out_id", "available",
}
STATUS_EVENT_TYPES = {"postponed", "suspended", "resumed", "rescheduled", "cancelled"}


# --- Apoio ---------------------------------------------------------------------------------


class Op:
    """Lança pelos serviços (o mesmo núcleo da tela do operador), com relógio controlado."""

    def __init__(self, match, user):
        self.match_id = match.id
        self.user = user
        self.clock = timeutils.now() - timedelta(minutes=150)
        self.n = 0

    def _at(self, after: float):
        self.clock += timedelta(minutes=after)
        self.n += 1
        return self.clock, f"pub-{self.match_id}-{self.n}"

    def post(self, type_, *, after=1.0, confirm=False, **fields):
        at, key = self._at(after)
        new = NewEvent(type=type_, **fields)
        return services.post_event(self.match_id, self.user, new, idempotency_key=key, confirm=confirm, at=at)

    def status(self, action, *, after=1.0, **kwargs):
        at, key = self._at(after)
        return services.change_status(self.match_id, self.user, action, idempotency_key=key, at=at, **kwargs)

    def void(self, event_id, *, reason=""):
        at, _ = self._at(0.5)
        return services.void_event(self.match_id, event_id, self.user, reason=reason, at=at)

    def goal(self, team, minute, player, **payload):
        return self.post("goal", team_id=team.id, minute=minute, payload={"player": player, **payload})


def walk(node, keys: set, values: list) -> None:
    """Percorre o JSON inteiro: todas as chaves e todos os valores folha."""
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            walk(value, keys, values)
    elif isinstance(node, list):
        for item in node:
            walk(item, keys, values)
    else:
        values.append(node)


def get(client: Client, path: str, key: str | None, data=None, **headers):
    if key is not None:
        headers["HTTP_X_API_KEY"] = key
    return client.get(f"{BASE}{path}", data or {}, **headers)


def ok(response) -> dict:
    assert response.status_code == 200, response.content.decode()
    return response.json()


def assert_error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.content.decode()
    body = response.json()
    assert body["code"] == code and isinstance(body["message"], str) and "details" in body
    assert response["Cache-Control"] == "no-store"
    assert "ETag" not in response
    return body


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def frozen_clock(monkeypatch):
    """Relógio do limite de uso parado (a janela de 60 s não vira no meio do teste)."""
    state = {"now": 1_800_000_010.0}
    monkeypatch.setattr(throttle, "clock", lambda: state["now"])
    return state


@pytest.fixture
def api_key(db):
    obj, raw = ApiKey.generate("Parceiro de teste", 1000)
    return obj, raw


@pytest.fixture
def key(api_key):
    return api_key[1]


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def league(db):
    return make_league(
        n_teams=4,
        slug="pernambucano",
        team_names=["Sport", "Náutico", "Santa Cruz", "Retrô"],
        zones=[{"name": "Classificados", "color": "#1B7F3B", "position_from": 1, "position_to": 2}],
    )


@pytest.fixture
def live_match(league):
    sport, nautico = league["teams"][0], league["teams"][1]
    kickoff = timeutils.now() - timedelta(minutes=150)
    return make_match(league["stage"], sport, nautico, kickoff_at=kickoff, round=league["rounds"][0])


# --- Contrato: gol anulado e tipos internos não aparecem -------------------------------------


def test_contract_annulled_goal_and_internal_types_never_appear(client, key, league, live_match, operator_user):
    sport, nautico = league["teams"][0], league["teams"][1]
    op = Op(live_match, operator_user)
    op.post("match_start")
    op.goal(sport, 10, "Valido Gomes")
    annulled = op.goal(nautico, 20, "Anulado Pereira")
    annul = op.post("goal_annulled", minute=21, annuls_event_id=annulled.event.id, payload={"reason": "Impedimento claro"})
    voided = op.post("yellow_card", team_id=nautico.id, minute=25, payload={"player": "Cancelado Souza"})
    op.void(voided.event.id, reason="Lançado no time errado")
    suspend = op.status("suspend", reason="Chuva torrencial")
    resume = op.status("resume", after=10)
    op.post("yellow_card", team_id=sport.id, minute=30, payload={"player": "Duplo Amarelo"})
    second_yellow = op.post("yellow_card", team_id=sport.id, minute=40, payload={"player": "Duplo Amarelo"})
    (auto_red,) = second_yellow.derived
    op.post("stoppage_time", minute=45, payload={"minutes": 3})
    op.post("half_time")
    op.post("second_half_start", after=15)
    op.post("substitution", team_id=nautico.id, minute=55, payload={"player_out": "Sai Silva", "player_in": "Entra Costa"})
    op.post("penalty_missed", team_id=nautico.id, minute=58, payload={"player": "Perdeu Lima", "outcome": "saved"})
    op.goal(sport, 70, "Valido Gomes", origin="penalty", assist="Garçom Alves")
    op.post("match_end", after=30)

    # O banco tem tudo: gol anulado, anulação, cancelado, status e o vermelho automático.
    rows = MatchEvent.objects.filter(match=live_match)
    assert rows.filter(type="goal_annulled").count() == 1
    assert rows.filter(type__in=["suspended", "resumed"]).count() == 2
    assert rows.filter(voided_at__isnull=False).count() == 1
    assert rows.get(pk=auto_red.id).source == "system"

    response = get(client, f"/matches/{live_match.id}", key)
    body = ok(response)
    keys: set = set()
    values: list = []
    walk(body, keys, values)
    text = response.content.decode()

    # Nada interno em nenhum nível do documento.
    assert not keys & FORBIDDEN_KEYS, keys & FORBIDDEN_KEYS
    strings = {value for value in values if isinstance(value, str)}
    for hidden in ("goal_annulled", *STATUS_EVENT_TYPES, "Anulado Pereira", "Impedimento claro", "Cancelado Souza",
                   "Lançado no time errado", "Chuva torrencial", "second_yellow", "system", "operator"):
        assert hidden not in strings, hidden
    for hidden in ("Anulado Pereira", "Impedimento claro", "Cancelado Souza", "Chuva torrencial", "goal_annulled", "idempotency"):
        assert hidden not in text, hidden

    match = body["match"]
    events = match["events"]
    hidden_ids = {annulled.event.id, annul.event.id, voided.event.id, suspend.event.id, resume.event.id}
    assert not hidden_ids & {event["id"] for event in events}
    assert [event["type"] for event in events] == [
        "match_start", "goal", "yellow_card", "yellow_card", "red_card", "stoppage_time", "half_time",
        "second_half_start", "substitution", "penalty_missed", "goal", "match_end",
    ]
    assert {event["kind"] for event in events} == {"structural", "game"}

    # Gols válidos e cartões (o vermelho do 2º amarelo inclusive) estão lá, tipados.
    assert (match["home_score"], match["away_score"]) == (2, 0)
    assert [goal["player"] for goal in match["goals"]] == ["Valido Gomes", "Valido Gomes"]
    goals = [event for event in events if event["type"] == "goal"]
    assert [(goal["player"], goal["origin"], goal["score_after"]) for goal in goals] == [
        ("Valido Gomes", "open_play", {"home": 1, "away": 0}),
        ("Valido Gomes", "penalty", {"home": 2, "away": 0}),
    ]
    assert goals[1]["origin_label"] == "Pênalti" and goals[1]["assist"] == "Garçom Alves"
    assert goals[0]["team_side"] == "home" and goals[0]["team_id"] == sport.id and goals[0]["minute_label"] == "10'"
    red = next(event for event in events if event["type"] == "red_card")
    assert red["id"] == auto_red.id and red["player"] == "Duplo Amarelo" and red["second_yellow"] is True
    assert [event["player"] for event in events if event["type"] == "yellow_card"] == ["Duplo Amarelo", "Duplo Amarelo"]
    assert match["cards"] == {"home": {"yellow": 2, "red": 1}, "away": {"yellow": 0, "red": 0}}
    assert match["red_cards"] == [{"team_side": "home", "player": "Duplo Amarelo", "minute_label": "40'"}]
    sub = next(event for event in events if event["type"] == "substitution")
    assert (sub["player_out"], sub["player_in"], sub["player"]) == ("Sai Silva", "Entra Costa", None)
    miss = next(event for event in events if event["type"] == "penalty_missed")
    assert (miss["outcome"], miss["outcome_label"], miss["player"]) == ("saved", "Defendido", "Perdeu Lima")
    stoppage = next(event for event in events if event["type"] == "stoppage_time")
    assert stoppage["stoppage_minutes"] == 3 and stoppage["player"] is None
    assert match["status"] == "finished" and match["winner"] == "home"

    # A lista de partidas (resumo) segue a mesma regra.
    listing = ok(get(client, "/matches", key, {"competition": "pernambucano"}))
    keys, values = set(), []
    walk(listing, keys, values)
    assert not keys & FORBIDDEN_KEYS
    (summary,) = listing["matches"]
    assert [goal["event_id"] for goal in summary["goals"]] == [goal["id"] for goal in goals]
    assert "events" not in summary


def test_shootout_kicks_expose_scored(client, key, league, operator_user):
    ko = make_knockout(league["season"], legs=1, extra_time=False, team_a=league["teams"][2], team_b=league["teams"][3])
    tie, team_a, team_b = ko["tie"], ko["team_a"], ko["team_b"]
    (match,) = make_tie_matches(tie, kickoff=timeutils.now() - timedelta(hours=2))
    op = Op(match, operator_user)
    op.post("match_start")
    op.post("half_time")
    op.post("second_half_start", after=15)
    op.post("penalties_start", after=50)
    for team, scored in [(team_a, True), (team_b, False), (team_a, True), (team_b, True), (team_a, True), (team_b, False)]:
        op.post("shootout_kick", team_id=team.id, payload={"player": f"Cobrador {team.short_name}", "scored": scored})
    op.post("match_end")

    body = ok(get(client, f"/matches/{match.id}", key))["match"]
    kicks = [event for event in body["events"] if event["type"] == "shootout_kick"]
    assert [kick["scored"] for kick in kicks] == [True, False, True, True, True, False]
    assert kicks[0]["player"] == f"Cobrador {team_a.short_name}" and kicks[0]["score_after"] is None
    assert (body["home_penalties"], body["away_penalties"]) == (3, 1) and body["winner"] == "home"
    assert body["tie"]["decided_by"] == "penalties" and body["tie"]["winner_team_id"] == team_a.id
    assert body["group"] is None and body["goals"] == []


# --- Chave ----------------------------------------------------------------------------------


def test_missing_invalid_or_inactive_key_is_401(client, api_key, league):
    obj, raw = api_key
    for value in (None, "", "fdr_nada_disso", raw + "x"):
        response = get(client, "/competitions", value)
        body = assert_error(response, 401, "invalid_api_key")
        assert "X-API-Key" in body["message"]
        assert "X-RateLimit-Limit" not in response
    assert ok(get(client, "/competitions", raw))["competitions"][0]["slug"] == "pernambucano"
    obj.active = False
    obj.save(update_fields=["active"])
    assert_error(get(client, "/competitions", raw), 401, "invalid_api_key")


def test_last_used_at_is_written_at_most_once_per_minute(client, api_key, league):
    obj, raw = api_key
    assert obj.last_used_at is None
    ok(get(client, "/competitions", raw))
    obj.refresh_from_db()
    first = obj.last_used_at
    assert first is not None
    ok(get(client, "/competitions", raw))
    obj.refresh_from_db()
    assert obj.last_used_at == first
    # Um minuto depois do último registro, grava de novo.
    ApiKey.objects.filter(pk=obj.pk).update(last_used_at=first - timedelta(seconds=61))
    ok(get(client, "/competitions", raw))
    obj.refresh_from_db()
    assert obj.last_used_at > first - timedelta(seconds=61)


# --- Limite de uso ---------------------------------------------------------------------------


def test_rate_limit_per_key_per_minute(client, league, frozen_clock):
    _, raw = ApiKey.generate("Limitada", 2)
    _, other = ApiKey.generate("Outra", 2)
    throttled_before = metrics.value("fdr_public_api_throttled_total")

    first = get(client, "/competitions", raw)
    assert first.status_code == 200
    assert (first["X-RateLimit-Limit"], first["X-RateLimit-Remaining"], first["X-RateLimit-Reset"]) == ("2", "1", "1800000060")
    assert get(client, "/competitions", raw)["X-RateLimit-Remaining"] == "0"

    blocked = get(client, "/competitions", raw)
    body = assert_error(blocked, 429, "rate_limited")
    assert blocked["Retry-After"] == "50"
    assert body["details"] == {"limit": 2, "retry_after": 50}
    assert (blocked["X-RateLimit-Limit"], blocked["X-RateLimit-Remaining"]) == ("2", "0")
    assert metrics.value("fdr_public_api_throttled_total") == throttled_before + 1

    # Cada chave tem o seu contador.
    assert get(client, "/competitions", other).status_code == 200

    # A janela vira: libera de novo.
    frozen_clock["now"] = 1_800_000_061.0
    again = get(client, "/competitions", raw)
    assert again.status_code == 200 and again["X-RateLimit-Remaining"] == "1"
    assert again["X-RateLimit-Reset"] == "1800000120"


def test_request_metrics_count_every_public_response(client, key, league):
    before_ok = metrics.value("fdr_public_api_requests_total", endpoint="competitions", status=200)
    before_401 = metrics.value("fdr_public_api_requests_total", endpoint="competitions", status=401)
    ok(get(client, "/competitions", key))
    get(client, "/competitions", None)
    assert metrics.value("fdr_public_api_requests_total", endpoint="competitions", status=200) == before_ok + 1
    assert metrics.value("fdr_public_api_requests_total", endpoint="competitions", status=401) == before_401 + 1


# --- Cache HTTP --------------------------------------------------------------------------------


def test_etag_and_304(client, league, live_match, operator_user, settings, frozen_clock):
    settings.PUBLIC_API = {**settings.PUBLIC_API, "CACHE_MAX_AGE": 30}
    _, raw = ApiKey.generate("Cache", 10)
    path = f"/matches/{live_match.id}"

    first = get(client, path, raw)
    assert first.status_code == 200
    etag = first["ETag"]
    assert etag == f'"{hashlib.sha256(first.content).hexdigest()[:32]}"'
    assert first["Cache-Control"] == "public, max-age=30"
    assert "X-API-Key" in first["Vary"]
    # Sem server_time no corpo: a mesma resposta repete o ETag.
    second = get(client, path, raw)
    assert second["ETag"] == etag and second["X-RateLimit-Remaining"] == "8"

    not_modified = get(client, path, raw, HTTP_IF_NONE_MATCH=etag)
    assert not_modified.status_code == 304 and not_modified.content == b""
    assert not_modified["ETag"] == etag and not_modified["Cache-Control"] == "public, max-age=30"
    assert not_modified["X-RateLimit-Remaining"] == "7"  # 304 também conta no limite
    assert get(client, path, raw, HTTP_IF_NONE_MATCH=f'"outro", W/{etag}').status_code == 304
    assert get(client, path, raw, HTTP_IF_NONE_MATCH='"outro"').status_code == 200

    # O dado mudou: ETag novo, e o antigo recebe o corpo inteiro.
    Op(live_match, operator_user).post("match_start")
    changed = get(client, path, raw, HTTP_IF_NONE_MATCH=etag)
    assert changed.status_code == 200 and changed["ETag"] != etag
    assert changed.json()["match"]["status"] == "live"

    # Erro não entra no cache.
    assert_error(get(client, "/matches/999999", raw, HTTP_IF_NONE_MATCH=etag), 404, "not_found")


# --- Documentação ---------------------------------------------------------------------------------


def test_docs_and_openapi_without_key(client):
    docs = client.get(f"{BASE}/docs")
    assert docs.status_code == 200 and b"openapi.json" in docs.content
    schema = client.get(f"{BASE}/openapi.json")
    assert schema.status_code == 200
    spec = schema.json()
    assert spec["components"]["securitySchemes"]["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": spec["components"]["securitySchemes"]["ApiKeyAuth"]["description"],
    }
    paths = spec["paths"]
    assert set(paths) == {
        f"{BASE}/competitions", f"{BASE}/competitions/{{slug}}", f"{BASE}/matches",
        f"{BASE}/matches/{{match_id}}", f"{BASE}/stages/{{stage_id}}/standings",
    }
    for item in paths.values():
        assert item["get"]["security"] == [{"ApiKeyAuth": []}]
        assert {"200", "304", "401", "429"} <= set(item["get"]["responses"])
    schemas = spec["components"]["schemas"]
    for name in ("EventOut", "MatchOut", "MatchDetailOut", "GoalOut"):
        assert not set(schemas[name]["properties"]) & FORBIDDEN_KEYS, name
    assert {"player_in", "player_out", "scored", "origin", "minute_label"} <= set(schemas["EventOut"]["properties"])


# --- Leitura ----------------------------------------------------------------------------------------


def test_competitions_and_competition_detail(client, key, league, live_match):
    other = make_league(n_teams=2, slug="copa-do-nordeste", position=5)
    menu = ok(get(client, "/competitions", key))["competitions"]
    assert [item["slug"] for item in menu] == ["pernambucano", "copa-do-nordeste"]
    assert set(menu[0]) == {"id", "name", "slug", "short_name", "position"}

    ko = make_knockout(league["season"], legs=1, extra_time=True)
    detail = ok(get(client, "/competitions/pernambucano", key))
    assert detail["competition"]["slug"] == "pernambucano" and detail["season"]["year"] == 2026
    assert detail["timezone"] == "America/Sao_Paulo"
    league_stage, knockout_stage = detail["stages"]
    assert league_stage["id"] == league["stage"].id and league_stage["has_standings"] is True
    assert league_stage["groups"] == [{"id": league["group"].id, "name": "Tabela"}]
    assert [rnd["number"] for rnd in league_stage["rounds"]] == [1, 2, 3]
    assert league_stage["current_round_id"] == league["rounds"][0].id  # o jogo ainda não terminou
    assert knockout_stage["id"] == ko["stage"].id and knockout_stage["has_standings"] is False and knockout_stage["groups"] == []
    assert detail["current_stage_id"] == league["stage"].id
    assert detail["current_round_id"] == league["rounds"][0].id
    assert ok(get(client, "/competitions/copa-do-nordeste", key))["competition"]["id"] == other["competition"].id

    assert_error(get(client, "/competitions/nao-existe", key), 404, "not_found")


def test_matches_filters_and_errors(client, key, league, live_match):
    sport, nautico, santa, retro = league["teams"]
    kickoff = timeutils.now() + timedelta(days=1)
    tomorrow = make_match(league["stage"], santa, retro, kickoff_at=kickoff, round=league["rounds"][1])
    other = make_league(n_teams=2, slug="copa")
    make_match(other["stage"], *other["teams"], kickoff_at=live_match.kickoff_at, round=other["rounds"][0])

    def ids(**params):
        return [item["id"] for item in ok(get(client, "/matches", key, params))["matches"]]

    assert len(ids()) == 3
    assert ids(competition="pernambucano") == [live_match.id, tomorrow.id]
    assert ids(competition="pernambucano", round_id=league["rounds"][1].id) == [tomorrow.id]
    day = tomorrow.kickoff_at.astimezone(timeutils.app_tz()).date().isoformat()
    assert ids(date=day, competition="pernambucano") == [tomorrow.id]
    assert ids(status="scheduled", competition="pernambucano") == [live_match.id, tomorrow.id]
    assert ids(status="live,finished") == []

    body = ok(get(client, "/matches", key, {"competition": "pernambucano"}))
    assert body["timezone"] == "America/Sao_Paulo"
    first = body["matches"][0]
    assert first["home"]["name"] == "Sport" and first["competition"]["slug"] == "pernambucano"
    assert first["clock"] is None and first["tie"] is None

    invalid = [({"date": "03/10/2026"}, "date"), ({"status": "live,ao_vivo"}, "status"), ({"round_id": "abc"}, "round_id")]
    for params, field in invalid:
        assert assert_error(get(client, "/matches", key, params), 400, "invalid_input")["details"]["field"] == field
    assert_error(get(client, "/matches", key, {"competition": "nao-existe"}), 404, "not_found")
    assert_error(get(client, "/matches/999999", key), 404, "not_found")


def test_matches_list_has_a_fixed_number_of_queries(client, key, league, operator_user, django_assert_max_num_queries):
    sport, nautico, santa, retro = league["teams"]
    for home, away in [(sport, nautico), (santa, retro)]:
        kickoff = timeutils.now() - timedelta(minutes=150)
        match = make_match(league["stage"], home, away, kickoff_at=kickoff, round=league["rounds"][0])
        op = Op(match, operator_user)
        op.post("match_start")
        op.goal(home, 10, "Artilheiro")
        op.post("yellow_card", team_id=away.id, minute=12, payload={"player": "Zagueiro"})
    ok(get(client, "/matches", key))  # aquece (sessão, cache de permissões...)
    with django_assert_max_num_queries(6) as small:
        ok(get(client, "/matches", key))
    for _ in range(4):
        make_match(league["stage"], sport, santa, kickoff_at=timeutils.now() + timedelta(days=2), round=league["rounds"][1])
    with django_assert_max_num_queries(len(small.captured_queries)):
        assert len(ok(get(client, "/matches", key))["matches"]) == 6


def test_standings_official_vs_live(client, key, league, live_match, operator_user):
    sport, nautico = league["teams"][0], league["teams"][1]
    op = Op(live_match, operator_user)
    op.post("match_start")
    op.goal(sport, 10, "Valido Gomes")
    path = f"/stages/{league['stage'].id}/standings"

    official = ok(get(client, path, key))
    assert official["kind"] == "official" and official["stage_id"] == league["stage"].id
    assert official["legend"] == [{"name": "Classificados", "color": "#1B7F3B", "from": 1, "to": 2}]
    assert [item["key"] for item in official["criteria"]][:2] == ["points", "wins"]
    assert official["points"] == {"win": 3, "draw": 1, "loss": 0}
    (group,) = official["groups"]
    assert all(row["played"] == 0 for row in group["rows"])

    live = ok(get(client, path, key, {"live": "1"}))
    assert live["kind"] == "live"
    rows = {row["team"]["id"]: row for row in live["groups"][0]["rows"]}
    assert rows[sport.id]["position"] == 1 and rows[sport.id]["points"] == 3 and rows[sport.id]["played"] == 1
    assert rows[sport.id]["playing"] is True and rows[sport.id]["zone"] == {"name": "Classificados", "color": "#1B7F3B"}
    assert rows[nautico.id]["lost"] == 1 and rows[nautico.id]["goal_difference"] == -1
    assert ok(get(client, path, key, {"live": "false"}))["kind"] == "official"
    keys, values = set(), []
    walk(live, keys, values)
    assert not keys & FORBIDDEN_KEYS

    assert assert_error(get(client, path, key, {"live": "talvez"}), 400, "invalid_input")["details"]["field"] == "live"
    ko = make_knockout(league["season"], legs=1, extra_time=False)
    assert_error(get(client, f"/stages/{ko['stage'].id}/standings", key), 404, "not_found")
    assert_error(get(client, "/stages/999999/standings", key), 404, "not_found")


# --- Admin e comando ----------------------------------------------------------------------------------


RAW_KEY = re.compile(r"fdr_[0-9a-f]{8}_[A-Za-z0-9_\-]+")


def test_admin_creates_key_and_shows_it_once(admin_client_fdr):
    add_page = admin_client_fdr.get("/admin/public_api/apikey/add/")
    assert add_page.status_code == 200
    form_fields = set(add_page.context["adminform"].form.fields)
    assert form_fields == {"name", "rate_limit_per_minute"}

    assert admin_client_fdr.post("/admin/public_api/apikey/add/", {"name": "Zero", "rate_limit_per_minute": 0}).status_code == 200
    assert not ApiKey.objects.filter(name="Zero").exists()

    response = admin_client_fdr.post(
        "/admin/public_api/apikey/add/", {"name": "Rádio Parceira", "rate_limit_per_minute": 30}, follow=True
    )
    assert response.status_code == 200
    texts = [str(message) for message in response.context["messages"]]
    raws = [match.group(0) for text in texts for match in [RAW_KEY.search(text)] if match]
    assert len(raws) == 1
    raw = raws[0]
    obj = ApiKey.objects.get(name="Rádio Parceira")
    assert obj.key_hash == hash_key(raw) and raw.startswith(f"fdr_{obj.prefix}_")
    assert obj.rate_limit_per_minute == 30 and obj.active

    change = admin_client_fdr.get(f"/admin/public_api/apikey/{obj.pk}/change/")
    html = change.content.decode()
    assert change.status_code == 200 and obj.prefix in html and raw not in html
    assert 'name="prefix"' not in html and 'name="key_hash"' not in html
    assert set(change.context["adminform"].form.fields) == {"name", "active", "rate_limit_per_minute"}
    # A chave criada no admin funciona na API; a auditoria registra a inclusão sem a chave.
    assert Client().get(f"{BASE}/competitions", HTTP_X_API_KEY=raw).status_code == 200
    entry = AuditLog.objects.get(action="admin.add", object_type="public_api.apikey", object_id=str(obj.pk))
    assert raw not in str(entry.data)


def test_admin_deactivate_action(admin_client_fdr, api_key):
    obj, raw = api_key
    response = admin_client_fdr.post(
        "/admin/public_api/apikey/", {"action": "deactivate", "_selected_action": [obj.pk]}, follow=True
    )
    assert response.status_code == 200
    obj.refresh_from_db()
    assert obj.active is False
    assert AuditLog.objects.filter(action="admin.change", object_type="public_api.apikey", object_id=str(obj.pk)).exists()
    assert_error(Client().get(f"{BASE}/competitions", HTTP_X_API_KEY=raw), 401, "invalid_api_key")


def test_only_administrator_manages_keys(operator_client, admin_client_fdr):
    assert operator_client.get("/admin/public_api/apikey/").status_code == 403
    assert operator_client.get("/admin/public_api/apikey/add/").status_code == 403
    assert admin_client_fdr.get("/admin/public_api/apikey/").status_code == 200


def test_create_api_key_command(settings):
    out = io.StringIO()
    call_command("create_api_key", "App Parceira", "--limit", "120", stdout=out)
    raw = out.getvalue().strip().splitlines()[-1]
    obj = ApiKey.objects.get(name="App Parceira")
    assert obj.key_hash == hash_key(raw) and obj.rate_limit_per_minute == 120
    assert obj.prefix in out.getvalue()
    entry = AuditLog.objects.get(action="api_key.create", object_id=str(obj.pk))
    assert entry.data["prefix"] == obj.prefix and raw not in str(entry.data)

    call_command("create_api_key", "Padrão", stdout=io.StringIO())
    assert ApiKey.objects.get(name="Padrão").rate_limit_per_minute == settings.PUBLIC_API["DEFAULT_RATE_LIMIT_PER_MINUTE"]
    with pytest.raises(CommandError):
        call_command("create_api_key", "Ruim", "--limit", "0", stdout=io.StringIO())
    with pytest.raises(CommandError):
        call_command("create_api_key", "   ", stdout=io.StringIO())


def test_match_model_unchanged_by_reads(client, key, live_match):
    """Leitura pública não grava nada na partida (nem versão)."""
    version = live_match.version
    ok(get(client, f"/matches/{live_match.id}", key))
    assert Match.objects.get(pk=live_match.pk).version == version
