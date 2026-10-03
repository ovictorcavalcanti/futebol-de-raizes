"""Páginas do front (fases 7 e 8): ganchos dos templates, testes puros em node (alertas,
reconexão do stream, minuto sugerido, cliente da API) e as três páginas no Chromium com a
API simulada (page.route): gol pelo stream na home, rodadas na competição e o fluxo do
operador. Sem banco."""

import copy
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.conf import settings

from matches.selectors import catalog_payload

ROOT = Path(settings.BASE_DIR)
STATIC = ROOT / "static"
NODE = shutil.which("node")
FRONT_TESTS = ("alerts.test.mjs", "stream.test.mjs", "operator.test.mjs")
OWN_JS = (
    "api.js",
    "stream.js",
    "alerts.js",
    "home.js",
    "competition.js",
    "operator.js",
)


def _html(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


# --- Testes puros (node --test) -----------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node não instalado")
@pytest.mark.parametrize("tz", ["America/Sao_Paulo", "Asia/Tokyo"])
def test_logica_pura_do_front_em_node(tz):
    result = subprocess.run(
        [NODE, "--test", *(str(ROOT / "tests" / "js" / name) for name in FRONT_TESTS)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TZ": tz},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^# fail 0$", result.stdout, re.M), result.stdout


# --- Ganchos das páginas ---------------------------------------------------------------------


def test_home_ganchos_e_modulos(client):
    html = _html(client, "/")
    for hook in (
        'id="notifications-hint"',
        'id="toggle-sound"',
        'id="toggle-notifications"',
        'id="goal-alert"',
        'id="latest-goals-list"',
        'id="goal-sound"',
        'id="live-status"',
        "js/home.js",
    ):
        assert hook in html, hook
    for module in ("api.js", "stream.js", "alerts.js"):
        assert f'rel="modulepreload" href="/static/js/{module}"' in html, module
    assert "GoalNow" not in html


def test_competicao_ganchos_e_modulos(client):
    html = _html(client, "/competition.html?slug=pernambucano")
    for hook in (
        'id="stage-select"',
        'id="round-prev"',
        'id="round-next"',
        'id="competition-missing"',
        "js/competition.js",
    ):
        assert hook in html, hook
    for module in ("api.js", "stream.js"):
        assert f'rel="modulepreload" href="/static/js/{module}"' in html, module


def test_operador_ganchos(client):
    html = _html(client, "/operator.html")
    for hook in (
        'id="event-type"',
        'data-hook="confirm-list"',
        'data-hook="actions-card"',
        'data-hook="actions-block"',
        'data-hook="status-block"',
        'id="status-kickoff"',
        'id="void-reason"',
        'rel="modulepreload" href="/static/js/api.js"',
        "js/operator.js",
    ):
        assert hook in html, hook
    # a validação é nativa (reportValidity no envio); o login valida sozinho
    assert (
        re.search(r'<form id="login-form"[^>]*>', html).group(0).count("novalidate")
        == 0
    )


def test_modulos_sem_innerhtml_e_sem_dependencias():
    for name in OWN_JS:
        source = (STATIC / "js" / name).read_text()
        assert not re.search(
            r"\.(?:inner|outer)HTML|insertAdjacentHTML|document\.write", source
        ), name
        for target in re.findall(r"^import [^;]*? from '([^']+)';", source, re.M):
            assert target.startswith("./") and (STATIC / "js" / target[2:]).is_file(), (
                name,
                target,
            )


# --- As páginas no Chromium com a API simulada ----------------------------------------------------

CHROMIUM_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--no-first-run",
    "--disable-sync",
]
CONTENT_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".wav": "audio/wav",
}


def _now_iso(delta_seconds=0):
    return (
        (datetime.now(UTC) + timedelta(seconds=delta_seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


@pytest.fixture(scope="module")
def fixtures():
    """Dados de exemplo de static/js/fixtures.js (formato do contrato) + catálogo real do domínio."""
    if NODE is None:
        pytest.skip("node não instalado")
    script = (
        f"const F = await import({json.dumps((STATIC / 'js' / 'fixtures.js').as_uri())});"
        "console.log(JSON.stringify({HOME: F.HOME, COMPETITION: F.COMPETITION, COMPETITIONS: F.COMPETITIONS,"
        "MATCHES: F.MATCHES, ME: F.ME, AVAILABLE: F.AVAILABLE, STANDINGS: F.STANDINGS}))"
    )
    data = json.loads(
        subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    )
    data["CATALOG"] = catalog_payload()
    return data


def _launch(playwright):
    try:
        return playwright.chromium.launch(args=CHROMIUM_ARGS)
    except Exception:
        for root in filter(
            None,
            [
                os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
                "/opt/pw-browsers",
                str(Path.home() / ".cache" / "ms-playwright"),
            ],
        ):
            for exe in sorted(
                Path(root).glob("chromium-*/chrome-linux/chrome"), reverse=True
            ):
                try:
                    return playwright.chromium.launch(
                        executable_path=str(exe), args=CHROMIUM_ARGS
                    )
                except Exception:
                    continue
    pytest.skip("Chromium do Playwright indisponível")


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        chromium = _launch(playwright)
        yield chromium
        chromium.close()


def _summary(match):
    return {
        k: v
        for k, v in match.items()
        if k
        not in (
            "events",
            "lineups",
            "officials",
            "broadcasts",
            "stats",
            "attendance",
            "revenue_cents",
        )
    }


def _sse(frames, retry=200):
    body = f"retry: {retry}\n\n"
    for topic, data, event_id in frames:
        body += (
            f"id: {event_id}\n" if event_id else ""
        ) + f"event: {topic}\ndata: {json.dumps(data)}\n\n"
    return body


class FakeApi:
    """Responde às rotas da API (CONTRACT §4) com os dados de exemplo e registra os pedidos."""

    def __init__(self, pages, fx):
        self.pages = pages
        self.fx = fx
        self.requests = []
        self.streams = []  # corpos SSE, um por conexão (o último se repete)
        self.me = {"authenticated": False, "user": None, "csrf_token": "tok-csrf"}
        self.posts = []

    def json(self, route, body, status=200):
        route.fulfill(
            status=status, body=json.dumps(body), content_type="application/json"
        )

    def match_detail(self, match_id):
        match = next(
            (m for m in self.fx["MATCHES"].values() if m["id"] == match_id), None
        )
        if match is None:
            return None
        match = copy.deepcopy(match)
        match.setdefault("events", [])
        match.setdefault("lineups", {"home": None, "away": None})
        if match["status"] == "live":
            available = self.fx["AVAILABLE"]
        elif match["status"] == "scheduled":
            available = {
                "events": ["match_start"],
                "status": ["postpone", "reschedule", "cancel"],
            }
        else:
            available = {"events": [], "status": []}
        return match, available

    def __call__(self, route):
        request = route.request
        path, _, query = request.url.split("fdr.test", 1)[1].partition("?")
        self.requests.append((request.method, path, query, dict(request.headers)))
        stamp = {"server_time": _now_iso(), "timezone": "America/Sao_Paulo"}
        if path in self.pages:
            return route.fulfill(body=self.pages[path], content_type="text/html")
        if path.startswith("/static/"):
            file = STATIC / path.removeprefix("/static/")
            if file.is_file():
                return route.fulfill(
                    body=file.read_bytes(),
                    content_type=CONTENT_TYPES.get(
                        file.suffix, "application/octet-stream"
                    ),
                )
            return route.fulfill(status=404, body="")
        if path == "/api/stream":
            body = (
                self.streams.pop(0)
                if len(self.streams) > 1
                else (
                    self.streams[0]
                    if self.streams
                    else _sse([("ping", {"server_time": _now_iso()}, None)])
                )
            )
            return route.fulfill(body=body, content_type="text/event-stream")
        if path == "/api/competitions":
            return self.json(route, {"competitions": self.fx["COMPETITIONS"]})
        if path == "/api/home":
            return self.json(route, {**copy.deepcopy(self.fx["HOME"]), **stamp})
        if path.startswith("/api/competitions/"):
            if path.endswith("/nao-existe"):
                return self.json(
                    route,
                    {
                        "code": "not_found",
                        "message": "Competição não encontrada.",
                        "details": {},
                    },
                    404,
                )
            data = {**copy.deepcopy(self.fx["COMPETITION"]), **stamp}
            if "stage=6" in query:
                data["stage"] = {
                    "id": 6,
                    "name": "Fase de grupos",
                    "format": "groups",
                    "standings": self.fx["STANDINGS"],
                    "ties": [],
                    "matches": [
                        _summary(self.fx["MATCHES"]["live"]),
                        _summary(self.fx["MATCHES"]["finished"]),
                    ],
                }
                data["current_stage_id"], data["current_round_id"] = 6, 26
            elif "round=31" in query:
                data["stage"] = {**data["stage"], "matches": [], "ties": []}
                data["current_round_id"] = 31
            return self.json(route, data)
        if path == "/api/matches":
            if "roundId=" in query:
                matches = [_summary(self.fx["MATCHES"]["scheduledToday"])]
            else:
                keys = (
                    "live",
                    "halfTime",
                    "scheduledToday",
                    "finished",
                    "knockoutPenalties",
                )
                matches = [_summary(self.fx["MATCHES"][k]) for k in keys]
            return self.json(route, {**stamp, "matches": matches})
        if path.startswith("/api/matches/"):
            found = self.match_detail(int(path.rsplit("/", 1)[1]))
            if not found:
                return self.json(
                    route,
                    {
                        "code": "not_found",
                        "message": "Partida não encontrada.",
                        "details": {},
                    },
                    404,
                )
            return self.json(
                route,
                {**stamp, "cursor": 4182, "match": found[0], "available": found[1]},
            )
        if path == "/api/auth/me":
            return self.json(route, self.me)
        if path == "/api/auth/login":
            if json.loads(request.post_data or "{}").get("password") != "certa":
                return self.json(
                    route,
                    {
                        "code": "invalid_credentials",
                        "message": "Usuário ou senha incorretos.",
                        "details": {},
                    },
                    401,
                )
            self.me = self.fx["ME"]
            return self.json(route, {"user": self.fx["ME"]["user"]})
        if path == "/api/ops/catalog":
            return self.json(route, self.fx["CATALOG"])
        if path.endswith("/events") and request.method == "POST":
            body = json.loads(request.post_data)
            self.posts.append({"body": body, "headers": dict(request.headers)})
            if not body.get("confirm"):
                return self.json(
                    route,
                    {
                        "code": "confirmation_required",
                        "message": "Há avisos neste lançamento: confirme para lançar mesmo assim.",
                        "details": {"codes": ["player_not_in_lineup"]},
                        "warnings": [
                            {
                                "code": "player_not_in_lineup",
                                "message": "Jogador fora da escalação.",
                            }
                        ],
                    },
                    422,
                )
            match = copy.deepcopy(self.fx["MATCHES"]["live"])
            match["version"] += 1
            event = {
                **match["events"][1],
                "id": 9001,
                "sequence": len(match["events"]) + 1,
            }
            match["events"].append(event)
            return self.json(
                route,
                {
                    "event": event,
                    "derived": [],
                    "match": match,
                    "available": self.fx["AVAILABLE"],
                    "warnings": [],
                    "replayed": False,
                },
                201,
            )
        return self.json(
            route, {"code": "not_found", "message": "?", "details": {}}, 404
        )


@pytest.fixture
def open_page(browser, client, fixtures):
    pages = {
        "/": _html(client, "/"),
        "/competition.html": _html(client, "/competition.html?slug=x"),
        "/operator.html": _html(client, "/operator.html"),
    }
    contexts = []

    def _open(url, *, width=1280, prepare=None):
        api = FakeApi(pages, fixtures)
        if prepare:
            prepare(api)
        context = browser.new_context(viewport={"width": width, "height": 900})
        contexts.append(context)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://fdr.test/**", api)
        page.goto(f"http://fdr.test{url}")
        return page, api, errors

    yield _open
    for context in contexts:
        context.close()


def _latest(goal_id, match, minutes_ago=0.1, player="Romarinho"):
    score = {"home": match["home_score"] + 1, "away": match["away_score"]}
    goal = {
        "event_id": goal_id,
        "match_id": match["id"],
        "team_id": match["home"]["id"],
        "team_side": "home",
        "player": player,
        "origin": "open_play",
        "period": "second_half",
        "minute": 74,
        "stoppage": None,
        "minute_label": "74'",
        "score_after": score,
        "created_at": _now_iso(-minutes_ago * 60),
    }
    return {
        **goal,
        "match": {
            "id": match["id"],
            "competition": {
                "name": match["competition"]["name"],
                "slug": match["competition"]["slug"],
            },
            "home": match["home"],
            "away": match["away"],
        },
        "team": match["home"],
    }


def test_home_gol_pelo_stream_alerta_uma_vez_e_corrige(open_page, fixtures):
    live = copy.deepcopy(fixtures["MATCHES"]["live"])
    goal = _latest(9001, live)
    old = _latest(9002, live, minutes_ago=5, player="Barletta")
    other_day = {
        **_latest(9003, live),
        "match_id": 999,
        "match": {**_latest(9003, live)["match"], "id": 999},
    }
    updated = {
        **live,
        "home_score": live["home_score"] + 1,
        "version": live["version"] + 1,
    }
    date = fixtures["HOME"]["date"]
    base = fixtures["HOME"]["latest_goals"]

    def prepare(api):
        api.streams = [
            _sse(
                [
                    ("ping", {"server_time": _now_iso()}, None),
                    (
                        "match",
                        {"stage_id": 3, "competition_id": 1, "match": updated},
                        4183,
                    ),
                    (
                        "goals",
                        {
                            "date": date,
                            "changes": [
                                {"kind": "added", "reason": None, "goal": goal}
                            ],
                            "latest_goals": [goal, *base],
                        },
                        4184,
                    ),
                    (
                        "goals",
                        {
                            "date": date,
                            "changes": [
                                {"kind": "added", "reason": None, "goal": goal}
                            ],
                            "latest_goals": [goal, *base],
                        },
                        4185,
                    ),
                    (
                        "goals",
                        {
                            "date": date,
                            "changes": [{"kind": "added", "reason": None, "goal": old}],
                            "latest_goals": [goal, old, *base],
                        },
                        4186,
                    ),
                    (
                        "goals",
                        {
                            "date": date,
                            "changes": [
                                {
                                    "kind": "removed",
                                    "reason": "voided",
                                    "goal": other_day,
                                }
                            ],
                            "latest_goals": [goal, old, *base],
                        },
                        4187,
                    ),
                    (
                        "goals",
                        {
                            "date": date,
                            "changes": [
                                {"kind": "removed", "reason": "annulled", "goal": goal}
                            ],
                            "latest_goals": [old, *base],
                        },
                        4188,
                    ),
                ]
            ),
            _sse([("ping", {"server_time": _now_iso()}, None)]),
        ]

    page, api, errors = open_page("/", prepare=prepare)
    page.wait_for_selector("#goal-alert .goal-alert--correction", timeout=10_000)
    page.wait_for_function(
        "() => document.querySelectorAll('#competitions-nav a').length === 4"
    )
    out = page.evaluate("""() => ({
      alerts: [...document.querySelectorAll('#goal-alert .goal-alert__title')].map((e) => e.textContent),
      who: document.querySelector('#goal-alert .goal-alert--correction .goal-alert__who')?.textContent,
      first: document.querySelector('#latest-goals-list li')?.dataset.eventId,
      count: document.querySelectorAll('#latest-goals-list li').length,
      score: document.querySelector('.match[data-match-id="12"] [data-hook="score-home"]').textContent,
      sections: document.querySelectorAll('#competitions > .comp-section').length,
      live: !!document.querySelector('#competitions-nav .comp-nav__live'),
      clock: document.getElementById('brasilia-clock').textContent,
      notifyHidden: document.getElementById('toggle-notifications').hidden,
      hint: !document.getElementById('notifications-hint').hidden,
      noScroll: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    })""")
    # 1 alerta por gol; antigo e de outro dia não alertam; a correção substitui o "É gol!"
    assert out["alerts"] == ["Oxe! Gol anulado."], out
    assert "Romarinho" in out["who"] and "74'" in out["who"]
    assert (
        out["first"] == "9002" and out["count"] == len(base) + 1
    )  # a lista é a da mensagem
    assert out["score"] == str(live["home_score"] + 1)
    assert out["sections"] == 2 and out["live"]
    assert re.fullmatch(r"\d\d:\d\d:\d\d", out["clock"])
    # http://fdr.test não é contexto seguro: sem botão de notificação e com o aviso de HTTPS
    assert out["notifyHidden"] and out["hint"]
    assert out["noScroll"]
    # o navegador reconecta sozinho com Last-Event-ID = último id recebido
    page.wait_for_timeout(600)
    streams = [r for r in api.requests if r[1] == "/api/stream"]
    assert streams[0][2] == f"after={fixtures['HOME']['cursor']}"
    assert len(streams) >= 2 and streams[1][3].get("last-event-id") == "4188"
    # acordeão: busca o detalhe só ao abrir
    page.locator('.match[data-match-id="13"] summary').click()
    page.wait_for_selector('.match[data-match-id="13"] .tl-item')
    assert any(r[1] == "/api/matches/13" for r in api.requests)
    assert not errors, errors


def test_home_sem_jogo(open_page, fixtures):
    def prepare(api):
        api.fx = {
            **api.fx,
            "HOME": {**api.fx["HOME"], "competitions": [], "latest_goals": []},
        }

    page, _, errors = open_page("/", prepare=prepare)
    page.wait_for_selector("#home-empty:not([hidden])")
    assert page.evaluate("document.getElementById('latest-goals').hidden")
    assert page.evaluate("document.querySelectorAll('#competitions-nav a').length") == 4
    assert not errors, errors


def test_competicao_rodadas_fase_e_slug_inexistente(open_page):
    page, api, errors = open_page("/competition.html?slug=copa-pernambuco", width=390)
    page.wait_for_selector("#stage-ties .tie")
    state = "() => ({label: document.getElementById('round-label').textContent, prev: document.getElementById('round-prev').disabled, next: document.getElementById('round-next').disabled, url: location.search, ties: document.querySelectorAll('#stage-ties .tie').length, standings: !document.getElementById('stage-standings').hidden, cards: document.querySelectorAll('#round-matches .match').length})"
    first = page.evaluate(state)
    assert (
        first["label"] == "Semifinal"
        and first["prev"]
        and not first["next"]
        and first["ties"] == 2
        and not first["standings"]
    )
    assert first["url"] == "?slug=copa-pernambuco&stage=7&round=30"
    page.click("#round-next")
    page.wait_for_function(
        "() => document.getElementById('round-label').textContent === 'Final'"
    )
    page.wait_for_selector("#round-empty:not([hidden])")
    final = page.evaluate(state)
    assert (
        not final["prev"]
        and final["next"]
        and final["url"].endswith("round=31")
        and final["ties"] == 0
    )
    page.select_option("#stage-select", "6")
    page.wait_for_selector("#stage-standings .standings")
    groups = page.evaluate(state)
    assert (
        groups["label"] == "Rodada 2"
        and not groups["prev"]
        and not groups["next"]
        and groups["standings"]
        and groups["cards"] == 2
    )
    page.click("#round-prev")
    page.wait_for_function(
        "() => document.getElementById('round-label').textContent === 'Rodada 1'"
    )
    assert any(r[1] == "/api/matches" and "roundId=25" in r[2] for r in api.requests)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert not errors, errors

    page, _, errors = open_page("/competition.html?slug=nao-existe")
    page.wait_for_selector("#competition-missing:not([hidden])")
    assert page.evaluate("document.getElementById('competition-grid').hidden")
    assert not errors, errors


def test_operador_lanca_gol_com_confirmacao(open_page):
    page, api, errors = open_page("/operator.html")
    page.wait_for_selector("#op-login:not([hidden])")
    page.fill("#login-username", "operador")
    page.fill("#login-password", "errada")
    page.click("#login-submit")
    page.wait_for_selector("#login-error:not([hidden])")
    page.fill("#login-password", "certa")
    page.click("#login-submit")
    page.wait_for_selector('#picker-list li[data-match-id="12"] .pick-item')
    assert page.evaluate(
        "[...document.querySelectorAll('.match-picker__group-title')].map((e) => e.textContent)"
    ) == ["Pernambucano Raiz", "Copa Pernambuco"]
    page.click('#picker-list li[data-match-id="12"] .pick-item')
    page.wait_for_selector('#action-grid [data-type="goal"]')
    assert page.evaluate(
        "[...document.querySelectorAll('#status-actions button:not(:disabled)')].map((b) => b.dataset.action)"
    ) == ["suspend"]
    page.click('#action-grid [data-type="goal"]')
    visible = "() => [...document.querySelectorAll('#event-form [data-hook=event-fields] > *')].filter((e) => !e.hidden).map((e) => e.dataset.field)"
    assert page.evaluate(visible) == ["minute", "team", "player", "choice", "player"]
    minute = int(
        page.evaluate("document.querySelector('#event-form input[name=minute]').value")
    )
    assert 70 <= minute <= 75  # sugerido pelo relógio (2T, ~72')
    # gol anulado: escolhido o gol, o time some (herda o do gol)
    page.select_option("#event-type", "goal_annulled")
    assert page.evaluate(visible) == ["minute", "event_ref", "team", "text"]
    page.select_option('#event-form select[name="annuls_event_id"]', index=1)
    assert page.evaluate(visible) == ["minute", "event_ref", "text"]
    page.select_option("#event-type", "goal")
    # sem time: a validação nativa barra o envio
    page.click('[data-hook="event-submit"]')
    assert api.posts == []
    page.check("#event-form [data-field=team] input[type=radio] >> nth=0", force=True)
    assert (
        page.evaluate(
            "document.querySelectorAll('#event-form [data-field=player] datalist option').length"
        )
        > 0
    )
    page.fill('#event-form input[name="payload.player"]', "Romarinho")
    page.click('[data-hook="event-submit"]')
    page.wait_for_selector("#confirm-dialog[open]")
    assert (
        page.evaluate(
            "document.querySelector('#confirm-dialog [data-hook=confirm-list]').textContent"
        )
        == "Jogador fora da escalação."
    )
    page.click('#confirm-dialog [data-hook="confirm-ok"]')
    page.wait_for_selector("#event-form-card[hidden]", state="attached")
    first, second = api.posts
    assert first["body"]["confirm"] is False and second["body"]["confirm"] is True
    key = first["headers"]["idempotency-key"]
    assert (
        re.fullmatch(r"[0-9a-f-]{36}", key)
        and second["headers"]["idempotency-key"] == key
    )  # mesma chave na confirmação
    assert first["headers"]["x-csrftoken"] == "tok-csrf"
    assert first["body"] == {
        "type": "goal",
        "minute": minute,
        "team_id": 1,
        "payload": {"player": "Romarinho"},
        "confirm": False,
        "source": "operator",
    }
    page.wait_for_function(
        "() => document.querySelectorAll('#op-timeline li').length === 18"
    )
    assert (
        sum(1 for r in api.requests if r[1] == "/api/matches/12") >= 2
    )  # busca a partida de novo depois do envio
    assert not errors, errors
