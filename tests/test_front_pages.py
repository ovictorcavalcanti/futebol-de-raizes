"""Páginas do front (fases 7 e 8): ganchos dos templates, testes puros em node (alertas,
reconexão do stream, minuto sugerido, cliente da API) e as três páginas no Chromium com a
API simulada (page.route): gol pelo stream na home, rodadas na competição e o fluxo do
operador; mais as páginas de erro, a marca no admin e o acabamento visual (nomes dos
times, logo largo, ordem dos botões do operador). Sem banco, exceto o login do admin."""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.views import defaults as error_views

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
    # busca: título e descrição com o Campeonato Pernambucano
    assert "<title>Futebol de Raízes - Campeonato Pernambucano ao vivo e tabela</title>" in html
    assert '<meta name="description" content="Campeonato Pernambucano ao vivo:' in html


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


@pytest.mark.parametrize("url", ["/", "/competition.html?slug=pernambucano"])
def test_fonte_dos_titulos_pre_carregada(client, url):
    """Alfa Slab One é a face do h1 (LCP da home): pré-carregada, não descoberta só depois do CSS."""
    html = _html(client, url)
    assert re.search(
        r'<link rel="preload" href="/static/fonts/alfa-slab-one-latin-400-normal\.woff2" as="font" type="font/woff2" crossorigin>',
        html,
    )


def test_ultimos_gols_visiveis_com_esqueleto_e_focaveis(client):
    """Sem layout shift: o bloco já aparece com um chip esqueleto e a data do dia; a lista que
    rola para o lado recebe foco pelo teclado (axe: scrollable-region-focusable)."""
    html = _html(client, "/")
    section = re.search(r'<section id="latest-goals"[^>]*>', html).group(0)
    assert " hidden" not in section
    ol = re.search(r'<ol id="latest-goals-list"[^>]*>(.*?)</ol>', html, re.S)
    assert 'tabindex="0"' in ol.group(0) and "aria-label=" in ol.group(0)
    assert '<li class="skeleton" aria-hidden="true">' in ol.group(1)
    date = re.search(r'<time id="home-date"[^>]*datetime="(\d{4}-\d\d-\d\d)"[^>]*>([^<]+)</time>', html)
    assert date and " de " in date.group(2)
    # menu de competições (esqueleto) nas páginas públicas
    assert "comp-nav__placeholder" in html


@override_settings(DEBUG=False)
@pytest.mark.parametrize("url", ["/nao-existe", "/styleguide.html"])
def test_pagina_404_com_a_marca_em_pt_br(client, url):
    response = client.get(url)
    assert response.status_code == 404
    html = response.content.decode()
    assert '<html lang="pt-BR"' in html
    assert "Página não encontrada" in html and "Essa página não existe, visse?" in html
    assert 'href="/"' in html and "Ver os jogos de hoje" in html
    assert "#ill-sertao" in html and "/static/img/logo.svg" in html and "Futebol de Raízes" in html
    assert "Not Found" not in html and "Guia de estilo" not in html
    # sem `page`: nada do menu de competições que ficaria carregando para sempre
    assert "comp-nav__placeholder" not in html and 'aria-busy="true"' not in html


def test_pagina_500_sem_contexto_e_403_com_a_marca(settings):
    """O server_error do Django renderiza sem request e sem context processors."""
    html = render_to_string("500.html")
    assert '<html lang="pt-BR"' in html and "Não deu certo agora." in html and 'href="/"' in html
    assert "{{" not in html and "{%" not in html and "Server Error" not in html
    request = RequestFactory().get("/qualquer")
    response = error_views.server_error(request)
    assert response.status_code == 500 and "Tente de novo em instantes." in response.content.decode()
    response = error_views.permission_denied(request, PermissionDenied())
    html = response.content.decode()
    assert response.status_code == 403
    assert '<html lang="pt-BR"' in html and "Acesso negado." in html and "Futebol de Raízes" in html
    assert "comp-nav__placeholder" not in html


@pytest.mark.django_db
def test_admin_usa_o_nome_da_marca(client, settings):
    settings.BRAND = {**settings.BRAND, "name": "Raízes do Agreste", "logo_alt": "Raízes do Agreste"}
    html = client.get("/admin/login/").content.decode()
    assert "<title>" in html and "Raízes do Agreste · Administração" in html
    assert "Futebol de Raízes" not in html
    # senha errada: o aviso aparece na página, mas o título da aba não vira "Erro: ..."
    html = client.post("/admin/login/", {"username": "ninguem", "password": "errada"}).content.decode()
    assert 'class="errornote"' in html
    title = html[html.index("<title>") : html.index("</title>")]
    assert "Erro" not in title and "Raízes do Agreste · Administração" in title
    html = client.get("/admin/login/").content.decode()
    # tema do admin: sincroniza com o do site antes do theme.js do Django (claro por padrão)
    assert html.index('localStorage.getItem(k)==="dark"') < html.index("admin/js/theme.js")


def test_brand_name_vale_para_o_alt_do_logo_e_o_cabecalho_do_admin():
    """BRAND_NAME sozinho (sem BRAND_LOGO_ALT) chega ao alt do logo e ao admin.site."""
    script = (
        "import django; django.setup();"
        "from django.conf import settings; import config.urls; from django.contrib import admin;"
        "print(settings.BRAND['logo_alt']); print(admin.site.site_header); print(admin.site.site_title)"
    )
    env = {k: v for k, v in os.environ.items() if k != "BRAND_LOGO_ALT"}
    env.update(BRAND_NAME="Raízes do Agreste", DJANGO_SETTINGS_MODULE="config.settings", PYTHONIOENCODING="utf-8")
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["Raízes do Agreste", "Raízes do Agreste · Administração", "Raízes do Agreste"]


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
        "MATCHES: F.MATCHES, ME: F.ME, AVAILABLE: F.AVAILABLE, STANDINGS: F.STANDINGS, STANDINGS_GROUPS: F.STANDINGS_GROUPS}))"
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


def _team(fx, group, index):
    """Time da linha `index` do grupo `group` de STANDINGS_GROUPS (0 = Grupo A, 1 = Grupo B)."""
    return fx["STANDINGS_GROUPS"]["groups"][group]["rows"][index]["team"]


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
        self.status_posts = []  # POST /api/ops/matches/:id/status
        self.rankings = []  # RankingRef da competição (botões ao lado da tabela)
        self.ranking_stage_ids = []  # fases somadas por GET /api/rankings/{id}
        self.round_matches = None  # MatchOut de GET /api/matches?roundId= (padrão: scheduledToday)
        self.competition = None  # CompetitionOut de GET /api/competitions/:slug (padrão: COMPETITION)
        self.home = None  # HomeOut de GET /api/home (padrão: HOME)

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
                "status": ["postpone", "reschedule", "cancel", "walkover"],
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
            day = dict(parse_qsl(query)).get("date")  # o seletor de dias pede ?date=
            return self.json(route, {**copy.deepcopy(self.home or self.fx["HOME"]), **stamp, **({"date": day} if day else {})})
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
            data = {**copy.deepcopy(self.competition or self.fx["COMPETITION"]), **stamp}
            if "stage=6" in query:
                data["stage"] = {
                    "id": 6,
                    "name": "Fase de grupos",
                    "format": "groups",
                    "standings": {**self.fx["STANDINGS_GROUPS"], "stage_id": 6},
                    "ties": [],
                    "matches": [  # um jogo dentro de cada grupo e um entre grupos
                        {**_summary(self.fx["MATCHES"]["live"]), "group": {"id": 12, "name": "Grupo B"}, "home": _team(self.fx, 1, 0), "away": _team(self.fx, 1, 1)},
                        {**_summary(self.fx["MATCHES"]["finished"]), "group": {"id": 11, "name": "Grupo A"}, "home": _team(self.fx, 0, 2), "away": _team(self.fx, 0, 3)},
                        {**_summary(self.fx["MATCHES"]["scheduledToday"]), "group": {"id": 11, "name": "Grupo A"}},  # Central (A) × Salgueiro (B)
                    ],
                }
                data["current_stage_id"], data["current_round_id"] = 6, 26
            elif "round=31" in query:
                data["stage"] = {**data["stage"], "matches": [], "ties": []}
                data["current_round_id"] = 31
            if self.rankings:
                data["rankings"] = self.rankings
            return self.json(route, data)
        if path.startswith("/api/rankings/"):
            ranking_id = int(path.rsplit("/", 1)[1])
            ref = next(r for r in self.rankings if r["id"] == ranking_id)
            return self.json(
                route,
                {
                    **copy.deepcopy(self.fx["STANDINGS"]),
                    "stage_id": None,
                    "stage_name": ref["name"],
                    "ranking_id": ranking_id,
                    "scope": ref["scope"],
                    "stage_ids": self.ranking_stage_ids,
                    # a classificação é um "grupo" com o nome dela (standings/services.py)
                    "groups": [{**copy.deepcopy(self.fx["STANDINGS"]["groups"][0]), "id": None, "name": ref["name"]}],
                },
            )
        if path == "/api/matches":
            if "roundId=" in query:
                matches = self.round_matches if self.round_matches is not None else [_summary(self.fx["MATCHES"]["scheduledToday"])]
            else:
                keys = (
                    "live",
                    "halfTime",
                    "scheduledToday",
                    "finished",
                    "knockoutPenalties",
                )
                matches = [_summary(self.fx["MATCHES"][k]) for k in keys]
                status = dict(parse_qsl(query)).get("status")  # ?status=live: o menu da competição
                if status:
                    matches = [m for m in matches if m["status"] in status.split(",")]
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
        if path.endswith("/status") and request.method == "POST":
            body = json.loads(request.post_data)
            self.status_posts.append(body)
            match_id = int(path.split("/")[-2])
            match, _ = self.match_detail(match_id)
            match = {**match, "status": "finished", "status_label": "Encerrado", "version": match["version"] + 1}
            event = {"id": 9002, "sequence": 1, "type": body["action"], "kind": "status", "payload": {}}
            return self.json(route, {"event": event, "match": match, "available": {"events": [], "status": []}, "replayed": False}, 201)
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


def test_home_jogo_remarcado_para_outro_dia_sai_da_pagina(open_page, fixtures):
    scheduled = copy.deepcopy(fixtures["MATCHES"]["scheduledToday"])
    date = fixtures["HOME"]["date"]
    tomorrow = (datetime.fromisoformat(date) + timedelta(days=1)).date().isoformat()
    moved = {
        **scheduled,
        "kickoff_at": f"{tomorrow}T15:00:00Z",  # meio-dia de amanhã em Brasília
        "version": scheduled["version"] + 1,
    }

    def prepare(api):
        api.streams = [
            _sse(
                [
                    ("ping", {"server_time": _now_iso()}, None),
                    ("match", {"stage_id": 3, "competition_id": 1, "match": moved}, 4183),
                ]
            ),
            _sse([("ping", {"server_time": _now_iso()}, None)]),
        ]

    page, api, errors = open_page("/", prepare=prepare)
    page.wait_for_selector(f'.match[data-match-id="{scheduled["id"]}"]')
    # a home pedida de novo já não traz o jogo (o servidor filtra pelo dia do kickoff_at)
    home = copy.deepcopy(fixtures["HOME"])
    for comp in home["competitions"]:
        for stage in comp["stages"]:
            stage["matches"] = [m for m in stage["matches"] if m["id"] != scheduled["id"]]
    api.fx = {**api.fx, "HOME": home}
    page.wait_for_selector(
        f'.match[data-match-id="{scheduled["id"]}"]', state="detached", timeout=10_000
    )
    assert len([r for r in api.requests if r[1] == "/api/home"]) >= 2
    assert page.locator('.match[data-match-id="12"]').count() == 1  # os outros ficam
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


def test_home_fase_de_grupos_em_linhas_so_com_os_grupos_com_jogo(open_page, fixtures):
    """Fase de grupos na home: um grupo por linha (jogos | tabela do grupo, topo alinhado; a linha
    tem a altura da tabela), só os grupos com jogo no dia (também depois da mensagem
    `standings`, que traz todos os grupos); jogos entre grupos numa seção à parte, com as tabelas
    dos dois grupos uma abaixo da outra; critérios dentro do card da última tabela."""
    groups = fixtures["STANDINGS_GROUPS"]
    group_a, group_b = groups["groups"]
    sport, central = _team(fixtures, 0, 0), _team(fixtures, 0, 1)
    nautico, santa = _team(fixtures, 1, 0), _team(fixtures, 1, 1)
    base = {**_summary(fixtures["MATCHES"]["finished"]), "group": {"id": group_a["id"], "name": group_a["name"]}}
    in_b = {**_summary(fixtures["MATCHES"]["live"]), "group": {"id": group_b["id"], "name": group_b["name"]}, "home": nautico, "away": santa}

    def home_with(*matches):
        stage = {"id": groups["stage_id"], "name": "Fase de grupos", "format": "groups", "position": 1, "matches": list(matches), "standings": groups}
        home = copy.deepcopy(fixtures["HOME"])
        home["competitions"] = [{**home["competitions"][0], "stages": [stage]}]
        return home

    # [título, jogos, aviso, legendas das tabelas ao lado, nº de legendas, nº de blocos de critérios]
    rows = "() => [...document.querySelectorAll('[data-hook=\"matches\"] .group-row')].map((g) => [g.querySelector('.match-group__title').textContent, g.querySelectorAll('.match').length, g.querySelector('.group-row__empty')?.textContent || '', [...g.querySelectorAll('.group-row__table caption')].map((c) => c.firstChild.textContent).join('|'), g.querySelectorAll('.group-row__table .legend').length, g.querySelectorAll('.group-row__table .criteria').length])"
    loose = "() => document.querySelectorAll('[data-hook=\"matches\"] .standings > .card:not(.standings__group), [data-hook=\"standings\"]').length"
    update = copy.deepcopy(groups)
    update["groups"][0]["rows"][0]["points"] = 99

    def only_group_a(api):
        api.fx = {**api.fx, "HOME": home_with({**base, "home": sport, "away": central})}
        api.streams = [_sse([("standings", {"stage_id": groups["stage_id"], "standings": update}, 9001)])]

    page, _, errors = open_page("/", prepare=only_group_a)
    page.wait_for_function("() => document.querySelector('.group-row__table td.col-pts')?.textContent.startsWith('99')")
    assert page.evaluate(rows) == [["Grupo A", 1, "", "Grupo A", 1, 1]]  # Grupo B sem jogo no dia: fora
    assert page.evaluate(loose) == 0  # sem aside e sem caixa à parte
    assert not errors, errors

    # dois grupos com um jogo cada: a tabela de cada um fica na linha dele, ao lado do jogo dele
    def two_groups(api):
        api.fx = {**api.fx, "HOME": home_with({**base, "home": sport, "away": central}, in_b)}

    page, _, errors = open_page("/", prepare=two_groups, width=1280)
    page.wait_for_selector(".group-row__table .standings__group")
    assert page.evaluate(rows) == [["Grupo A", 1, "", "Grupo A", 1, 0], ["Grupo B", 1, "", "Grupo B", 1, 1]]
    sizes = page.evaluate("[...document.querySelectorAll('.group-row__table')].map((t) => { const row = t.closest('.group-row'); const games = row.querySelector('.match-group__games').getBoundingClientRect(); const box = t.getBoundingClientRect(); const next = row.nextElementSibling; return {top: Math.round(games.top - box.top), right: box.left > games.right, taller: box.bottom > games.bottom, row: row.getBoundingClientRect().bottom - box.bottom, next: next ? next.getBoundingClientRect().top - box.bottom : null}; })")
    assert all(s["top"] == 0 and s["right"] and s["taller"] and s["row"] >= -0.5 for s in sizes), sizes
    assert sizes[0]["next"] > 0  # o Grupo B começa depois da tabela do Grupo A
    assert not errors, errors

    # só jogo entre grupos: uma seção, com as tabelas dos dois grupos uma abaixo da outra
    def cross_groups(api):
        api.fx = {**api.fx, "HOME": home_with({**base, "home": sport, "away": nautico})}

    page, _, errors = open_page("/", prepare=cross_groups, width=1280)
    page.wait_for_selector(".group-row__table .standings__group")
    assert page.evaluate(rows) == [["Jogos entre grupos", 1, "", "Grupo A|Grupo B", 2, 1]]
    stack = page.evaluate("(() => { const t = [...document.querySelectorAll('.group-row--cross .group-row__table .standings')].map((x) => x.getBoundingClientRect()); const games = document.querySelector('.group-row--cross .match-group__games').getBoundingClientRect(); return {top: Math.round(games.top - t[0].top), below: t[1].top >= t[0].bottom, right: t[0].left > games.right}; })()")
    assert stack == {"top": 0, "below": True, "right": True}, stack
    assert page.evaluate(loose) == 0
    assert not errors, errors

    # nada casa com a tabela (dado incompleto) em outro dia: todos os grupos, e o aviso não diz "hoje"
    stranger = {"id": 9901, "name": "Time de Fora", "short_name": "FOR", "city": "Recife", "color_primary": "#123456", "color_secondary": "#ffffff", "crest_url": ""}

    def unknown_yesterday(api):
        home = home_with({**base, "home": stranger, "away": {**stranger, "id": 9902, "short_name": "FO2"}, "group": None})
        home["date"] = (datetime.fromisoformat(home["date"]) - timedelta(days=1)).date().isoformat()
        api.fx = {**api.fx, "HOME": home}

    page, _, errors = open_page("/", prepare=unknown_yesterday)
    page.wait_for_selector(".group-row__table .standings__group")
    assert [r[2] for r in page.evaluate(rows)][:2] == ["Não há jogos deste grupo ontem."] * 2
    assert not errors, errors

    # o admin troca os times e o grupo do jogo (status e início iguais): a home busca de novo
    moved = {**base, "home": nautico, "away": santa, "group": {"id": group_b["id"], "name": group_b["name"]}, "version": base["version"] + 1}

    def regrouped(api):
        api.fx = {**api.fx, "HOME": home_with({**base, "home": sport, "away": central})}
        api.streams = [_sse([("ping", {"server_time": _now_iso()}, None), ("match", {"stage_id": groups["stage_id"], "competition_id": 1, "match": moved}, 9002)]), _sse([("ping", {"server_time": _now_iso()}, None)])]

    page, api, errors = open_page("/", prepare=regrouped)
    page.wait_for_selector(".group-row__table .standings__group")
    api.fx = {**api.fx, "HOME": home_with(moved)}
    page.wait_for_function(f"() => ({rows})().map((r) => r[0]).join() === 'Grupo B'", timeout=10_000)
    assert len([r for r in api.requests if r[1] == "/api/home"]) >= 2
    assert not errors, errors


def test_home_classificacao_diz_a_fase_quando_a_competicao_tem_outras(open_page, fixtures):
    """Classificação resumida da home: com mais de uma fase na temporada (`season_stages`), o
    título diz de qual fase é ("Classificação · 1ª fase"); com uma só, "Classificação"."""
    home = copy.deepcopy(fixtures["HOME"])
    home["competitions"][0]["stages"][0]["standings"]["season_stages"] = 2

    def prepare(api):
        api.home = home

    caption = "() => document.querySelector('#competitions .standings caption')?.firstChild.textContent"
    page, api, errors = open_page("/", prepare=prepare)
    page.wait_for_selector("#competitions .standings caption")
    assert page.evaluate(caption) == "Classificação · 1ª fase"
    page, api, errors_single = open_page("/")
    page.wait_for_selector("#competitions .standings caption")
    assert page.evaluate(caption) == "Classificação"
    assert not errors and not errors_single, errors + errors_single


def test_home_seletor_de_dias(open_page):
    today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    tomorrow = (today + timedelta(days=1)).isoformat()

    def prepare(api):
        api.fx = {**api.fx, "HOME": {**api.fx["HOME"], "date": today.isoformat()}}

    page, api, errors = open_page("/", prepare=prepare)
    page.wait_for_selector("#competitions .match")
    title = "() => document.getElementById('home-title').textContent"
    assert page.evaluate(title) == "Jogos de hoje"
    assert page.evaluate("document.getElementById('day-today').hidden")
    page.click("#day-next")
    page.wait_for_function(f"() => document.getElementById('home-date').dateTime === '{tomorrow}'")
    assert page.evaluate(title) == "Jogos de amanhã"
    assert page.evaluate("location.search") == f"?date={tomorrow}"
    assert [q for _, p, q, _ in api.requests if p == "/api/home"][-1] == f"date={tomorrow}"
    assert not page.evaluate("document.getElementById('day-today').hidden")
    order = "() => [...document.querySelector('.date-nav').children].map((el) => el.id || el.className)"
    # dia futuro: "Hoje" junto do ‹ (o caminho de volta)
    assert page.evaluate(order) == ["day-today", "day-prev", "page-intro__date", "day-next"]
    page.click("#day-today")
    page.wait_for_function(f"() => document.getElementById('home-date').dateTime === '{today.isoformat()}'")
    assert page.evaluate("location.search") == ""
    assert page.evaluate("document.getElementById('day-today').hidden")
    page.click("#day-prev")
    yesterday = (today - timedelta(days=1)).isoformat()
    page.wait_for_function(f"() => document.getElementById('home-date').dateTime === '{yesterday}'")
    # dia passado: "Hoje" junto do ›
    assert page.evaluate(order) == ["day-prev", "page-intro__date", "day-next", "day-today"]
    page.go_back()
    page.go_back()  # o histórico volta para amanhã
    page.wait_for_function(f"() => document.getElementById('home-date').dateTime === '{tomorrow}'")
    assert page.evaluate(order)[0] == "day-today"
    assert not errors, errors


def test_competicao_rodadas_fase_e_slug_inexistente(open_page, fixtures):
    page, api, errors = open_page("/competition.html?slug=copa-pernambuco", width=390)
    page.wait_for_selector("#round-matches .tie-group .match")
    state = "() => ({label: document.getElementById('round-label').textContent, prev: document.getElementById('round-prev').disabled, next: document.getElementById('round-next').disabled, url: location.search, ties: document.querySelectorAll('#round-matches .tie-group').length, standings: !document.getElementById('stage-standings').hidden, cards: document.querySelectorAll('#round-matches .match').length})"
    first = page.evaluate(state)
    assert (
        first["label"] == "Semifinal"
        and first["prev"]
        and not first["next"]
        and first["ties"] == 2
        and not first["standings"]
    )
    # sem card de agregado à parte: a ida só avisa; a volta traz o agregado e quem avança
    legs = page.evaluate(
        "[...document.querySelectorAll('#round-matches .tie-group')].map((g) => [...g.querySelectorAll('.match')].map((c) => { const t = c.querySelector('.match__tie'); return t.hidden ? null : t.textContent; }))"
    )
    assert page.evaluate("document.querySelectorAll('#round-matches .tie, #stage-ties:not([hidden])').length") == 0
    assert legs[0][0] == "Jogo de ida"
    assert "Agregado" in legs[0][1] and "3 × 3" in legs[0][1] and "Náutico" in legs[0][1] and "avança nos pênaltis" in legs[0][1]
    assert legs[1] == [None]  # jogo único ainda sem vencedor
    # a faixa do card não repete a fase da navegação ("Semifinal"): só o jogo
    assert page.evaluate("[...document.querySelectorAll('#round-matches .match__lead > .match__round')].map((e) => e.textContent)") == ["Ida", "Volta", "Jogo único"]
    assert first["url"] == "?slug=copa-pernambuco&stage=7&round=30"
    # compartilhar os resultados: o mesmo botão da home ("Compartilhar" no celular, 44 px de altura)
    share = page.locator("#round-share")
    assert share.get_attribute("class") == "btn btn--ghost btn--sm controls__share"
    assert share.inner_text().strip() == "Compartilhar" and share.bounding_box()["height"] >= 44
    assert share.get_attribute("aria-label") == "Compartilhar os resultados no WhatsApp"
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
    page.wait_for_selector("#round-matches .group-row .standings")
    groups = page.evaluate(state)
    assert (
        groups["label"] == "Rodada 2"
        and not groups["prev"]
        and not groups["next"]
        and not groups["standings"]  # a tabela de cada grupo vai para a linha do grupo
        and groups["cards"] == 3
    )
    # fase de grupos: uma linha por grupo (na ordem da tabela), com os jogos e a tabela dele; o
    # jogo entre grupos (pelos times da tabela) numa seção à parte, sem tabela
    rows = "() => [...document.querySelectorAll('#round-matches .group-row:not(.group-row--head)')].map((g) => [g.querySelector('.match-group__title').textContent, g.querySelectorAll('.match').length, g.querySelector('.group-row__empty')?.textContent || '', g.querySelector('.group-row__table caption')?.textContent || ''])"
    # rodada mista (caso raro): a seção entre grupos traz também as tabelas dos dois grupos
    assert page.evaluate(rows) == [["Grupo A", 1, "", "Grupo A"], ["Grupo B", 1, "", "Grupo B"], ["Jogos entre grupos", 1, "", "Grupo A"]]
    assert page.evaluate("[...document.querySelectorAll('#round-matches .group-row--cross caption')].map((c) => c.firstChild.textContent)") == ["Grupo A", "Grupo B"]
    # legenda das cores embaixo de cada grupo; os critérios de desempate uma vez, dentro do card
    # do último grupo (sem caixa à parte depois das tabelas)
    assert page.evaluate("[...document.querySelectorAll('#round-matches .group-row__table')].map((t) => [!!t.querySelector('.standings__group .legend'), !!t.querySelector('.standings__group .criteria')])") == [[True, False], [True, False], [True, True]]
    assert page.evaluate("document.querySelectorAll('#round-matches .criteria').length") == 1
    assert page.evaluate("document.querySelectorAll('#round-matches .standings > .card:not(.standings__group)').length") == 0
    assert page.evaluate(  # cards do grupo com respiro entre si
        "parseFloat(getComputedStyle(document.querySelector('#round-matches .match-group__games')).rowGap) > 0"
    )
    page.set_viewport_size({"width": 1280, "height": 900})
    aligned = "() => [...document.querySelectorAll('#round-matches .group-row__table')].map((t) => { const first = t.parentElement.querySelector('.match-group__games, .group-row__empty'); return [Math.round(first.getBoundingClientRect().top - t.getBoundingClientRect().top), t.getBoundingClientRect().left > first.getBoundingClientRect().right]; })"
    assert page.evaluate(aligned) == [[0, True]] * 3  # tabela à direita, topo com o primeiro jogo
    # a tabela (com a legenda) é mais alta que o jogo do grupo: ela define a altura da linha
    sizes = page.evaluate("[...document.querySelectorAll('#round-matches .group-row__table')].map((t) => { const row = t.closest('.group-row'); const games = row.querySelector('.match-group__games'); const next = row.nextElementSibling; return {table: t.getBoundingClientRect().bottom, games: games.getBoundingClientRect().bottom, row: row.getBoundingClientRect().bottom, next: next ? next.getBoundingClientRect().top : null}; })")
    assert all(s["table"] > s["games"] and s["row"] >= s["table"] - 0.5 and (s["next"] is None or s["next"] > s["table"]) for s in sizes), sizes
    assert sizes[0]["next"] is not None  # o grupo seguinte começa depois da tabela do primeiro
    assert page.evaluate("document.getElementById('competition-grid').classList.contains('split--no-aside')")
    page.set_viewport_size({"width": 390, "height": 844})
    api.round_matches = [{**_summary(fixtures["MATCHES"]["finished"]), "group": {"id": 11, "name": "Grupo A"}, "home": _team(fixtures, 0, 2), "away": _team(fixtures, 0, 3)}]
    page.click("#round-prev")
    page.wait_for_function(
        "() => document.getElementById('round-label').textContent === 'Rodada 1'"
    )
    assert any(r[1] == "/api/matches" and "roundId=25" in r[2] for r in api.requests)
    # grupo sem jogo na rodada: o aviso no lugar dos jogos, a tabela continua ao lado
    page.wait_for_function("() => document.querySelectorAll('#round-matches .group-row__empty').length > 0")
    assert page.evaluate(rows) == [["Grupo A", 1, "", "Grupo A"], ["Grupo B", 0, "Não há jogos deste grupo nesta rodada.", "Grupo B"]]
    # rodada só com jogo entre grupos: uma seção, com as tabelas dos dois grupos empilhadas
    api.round_matches = [_summary(fixtures["MATCHES"]["scheduledToday"])]  # Central (A) × Salgueiro (B)
    page.click("#round-next")
    page.wait_for_function("() => document.querySelector('#round-matches .group-row--cross .group-row__table')")
    assert page.evaluate("[...document.querySelectorAll('#round-matches .group-row')].map((g) => [g.querySelector('.match-group__title').textContent, g.querySelectorAll('.match').length, [...g.querySelectorAll('.group-row__table caption')].map((c) => c.firstChild.textContent), g.querySelectorAll('.criteria').length])") == [["Jogos entre grupos", 1, ["Grupo A", "Grupo B"], 1]]
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert not errors, errors

    page, _, errors = open_page("/competition.html?slug=nao-existe")
    page.wait_for_selector("#competition-missing:not([hidden])")
    assert page.evaluate("document.getElementById('competition-grid').hidden")
    assert not errors, errors


def test_competicao_gol_durante_a_troca_de_rodada_nao_se_perde(open_page, fixtures):
    """O gol chega pelo stream enquanto a rodada nova carrega (o card ainda não existe); a
    resposta, de antes do gol, chega depois com 0 × 0: a tela termina com o placar do gol."""
    # jogo só da rodada nova (sem card na tela durante a carga)
    before = {**_summary(fixtures["MATCHES"]["scheduledToday"]), "id": 9101, "status": "live", "period": "first_half", "home_score": 0, "away_score": 0, "version": 5}
    goal = {**before, "home_score": 1, "version": 6}
    page, api, errors = open_page("/competition.html?slug=copa-pernambuco&stage=6")
    page.wait_for_selector("#round-matches .group-row")
    api.round_matches = [before]
    held = []  # segura a resposta da rodada nova
    page.route(lambda url: "/api/matches" in url and "roundId=" in url, lambda route: route.fallback() if held else held.append(route))
    page.click("#round-prev")
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    assert held  # a carga está a caminho
    card = f'#round-matches .match[data-match-id="{before["id"]}"]'
    assert not page.evaluate(f"!!document.querySelector('{card}')")
    ping = _sse([("ping", {"server_time": _now_iso()}, None)])
    api.streams = [_sse([("match", {"stage_id": 6, "competition_id": 2, "match": goal}, 8101)]), ping]
    for _ in range(50):  # o stream reconecta (retry 200 ms) e entrega
        if api.streams == [ping]:
            break
        page.wait_for_timeout(100)
    assert api.streams == [ping]
    page.wait_for_timeout(300)
    held[0].fallback()  # a resposta velha (0 × 0) chega depois do gol
    page.wait_for_selector(card)
    page.wait_for_function(f"() => document.querySelector('{card} [data-hook=\"score-home\"]')?.textContent === '1'", timeout=5_000)
    assert not errors, errors


def test_competicao_grupos_tabelas_atualizam_no_lugar(open_page, fixtures):
    """Mensagem `standings` na fase de grupos: as tabelas redesenham no lugar, sem refazer as
    linhas (os cards e as tabelas continuam os mesmos elementos), mesmo com os jogos da API
    numa ordem diferente da dos cards na tela."""
    page, api, errors = open_page("/competition.html?slug=copa-pernambuco&stage=6")
    page.wait_for_selector("#round-matches .group-row__table .standings__group")
    page.evaluate("window.__table = document.querySelector('.group-row__table .standings'); window.__card = document.querySelector('#round-matches .match')")
    update = copy.deepcopy({**fixtures["STANDINGS_GROUPS"], "stage_id": 6})
    update["groups"][0]["rows"][0]["points"] = 99
    ping = _sse([("ping", {"server_time": _now_iso()}, None)])
    api.streams = [_sse([("standings", {"stage_id": 6, "standings": update}, 7001)]), ping]
    page.wait_for_function("() => document.querySelector('.group-row__table td.col-pts')?.textContent.startsWith('99')", timeout=10_000)
    assert page.evaluate("document.contains(window.__table) && document.contains(window.__card)")
    assert not errors, errors


def test_competicao_classificacao_geral_atualiza_com_partida_do_mata_mata(open_page, fixtures):
    """O mata-mata não publica `standings`: a classificação geral que soma a fase busca de
    novo quando chega `match` de uma partida dela (placar, status, cartões ou fase mudaram)."""
    final = fixtures["MATCHES"]["knockoutPenalties"]  # fase 7 (mata-mata)
    goal = {**final, "home_score": final["home_score"] + 1, "version": final["version"] + 1}
    same = {**goal, "version": goal["version"] + 1}  # nada que conte na tabela mudou
    other = fixtures["MATCHES"]["live"]  # fase 3, fora da classificação

    def prepare(api):
        api.rankings = [{"id": 5, "name": "Geral", "scope": "overall"}]
        api.ranking_stage_ids = [6, 7]

    page, api, errors = open_page("/competition.html?slug=copa-pernambuco", prepare=prepare)
    page.wait_for_selector("#ranking-standings:not([hidden]) .standings")

    def ranking_gets():
        return sum(1 for r in api.requests if r[0] == "GET" and r[1] == "/api/rankings/5")

    assert ranking_gets() == 1
    ping = _sse([("ping", {"server_time": _now_iso()}, None)])
    with page.expect_response(lambda r: "/api/rankings/5" in r.url, timeout=5000):
        api.streams = [
            _sse([("match", {"stage_id": 7, "competition_id": 2, "match": goal}, 4183)]),
            ping,
        ]
    assert ranking_gets() == 2
    # o card redesenhado pelo stream continua sem a fase da navegação na faixa
    assert page.evaluate("document.querySelector('.match[data-match-id=\"21\"] .match__lead > .match__round').textContent") == "Volta"
    api.streams = [
        _sse(
            [
                ("match", {"stage_id": 7, "competition_id": 2, "match": same}, 4184),
                ("match", {"stage_id": 3, "competition_id": 1, "match": {**other, "home_score": 9}}, 4185),
            ]
        ),
        ping,
    ]
    page.wait_for_timeout(2500)  # o stream reconecta e entrega; espera mais que o agrupamento (1,5 s)
    assert api.streams == [ping]  # as mensagens foram entregues
    assert ranking_gets() == 2

    # o admin trocou o jogo de fase sem mexer em placar, status, times ou cartões: sair de
    # uma fase somada (7 → 3) ou entrar nela (3 → 7) também muda a classificação
    for stage_id, version, event_id, gets in ((3, 1, 4186, 3), (7, 2, 4187, 4)):
        moved = {"stage_id": stage_id, "competition_id": 2, "match": {**same, "version": same["version"] + version}}
        with page.expect_response(lambda r: "/api/rankings/5" in r.url, timeout=5000):
            api.streams = [_sse([("match", moved, event_id)]), ping]
        assert ranking_gets() == gets
    assert not errors, errors


def test_competicao_classificacao_geral_nao_perde_gol_anulado(open_page, fixtures):
    """O último visto de cada partida não pode ficar velho: segue sendo gravado com a tabela
    da fase na tela e é esquecido no reload (as mensagens perdidas não voltam). Senão um gol
    anulado (a partida volta ao placar já visto) não buscava a classificação de novo. Nem a
    mudança que chega no meio da carga da classificação pode se perder."""
    final = fixtures["MATCHES"]["knockoutPenalties"]  # fase 7 (mata-mata)

    def score(goals):
        return {
            "stage_id": 7,
            "competition_id": 2,
            "match": {**final, "home_score": final["home_score"] + goals, "version": final["version"] + goals},
        }

    def prepare(api):
        api.rankings = [{"id": 5, "name": "Geral", "scope": "overall"}]
        api.ranking_stage_ids = [6, 7]

    page, api, errors = open_page("/competition.html?slug=copa-pernambuco&stage=6", prepare=prepare)
    page.wait_for_selector("#standings-switch button")
    ping = _sse([("ping", {"server_time": _now_iso()}, None)])
    api.streams = [ping]
    switch = page.locator("#standings-switch")

    def ranking_gets():
        return sum(1 for r in api.requests if r[0] == "GET" and r[1] == "/api/rankings/5")

    def refetches_ranking():
        return page.expect_response(lambda r: "/api/rankings/5" in r.url, timeout=5000)

    def deliver(message, event_id):
        api.streams = [_sse([("match", message, event_id)]), ping]
        for _ in range(50):  # o stream reconecta (retry 200 ms) e entrega
            if api.streams == [ping]:
                break
            page.wait_for_timeout(100)
        assert api.streams == [ping]
        page.wait_for_timeout(300)

    with refetches_ranking():
        switch.get_by_role("button", name="Geral").click()
    # classificação geral na lateral: os jogos por grupo, e o jogo entre grupos à parte
    assert page.evaluate("[...document.querySelectorAll('#round-matches .match-group')].map((g) => [g.querySelector('.match-group__title').textContent, g.querySelectorAll('.match').length])") == [["Grupo A", 1], ["Grupo B", 1], ["Jogos entre grupos", 1]]
    with refetches_ranking():
        deliver(score(1), 4183)
    assert ranking_gets() == 2

    # com a tabela da fase na tela chega o 2x0; de volta à classificação, ela mostra o 2x0
    switch.get_by_role("button", name="Por grupo").click()
    deliver(score(2), 4184)
    with refetches_ranking():
        switch.get_by_role("button", name="Geral").click()
    assert ranking_gets() == 3
    with refetches_ranking():  # gol anulado: volta ao 1x0 (já visto antes da troca)
        deliver(score(1), 4185)
    assert ranking_gets() == 4

    # 5 min sem stream: o 2x0 se perdeu, o reload busca tudo de novo (com a classificação)
    with refetches_ranking():
        page.evaluate("document.querySelector('[data-hook=\"competition-retry\"]').click()")
    assert ranking_gets() == 5
    with refetches_ranking():  # gol anulado: volta ao 1x0 (o último visto antes do reload)
        deliver(score(1), 4187)
    assert ranking_gets() == 6

    # o 2x0 chega no meio da carga (da tabela da fase para "Geral"): as fases somadas ainda
    # não chegaram e a resposta pode vir velha; ao fim da carga, busca de novo
    switch.get_by_role("button", name="Por grupo").click()
    held = []  # segura a primeira resposta da classificação
    page.route(lambda url: "/api/rankings/5" in url, lambda route: route.fallback() if held else held.append(route))
    switch.get_by_role("button", name="Geral").click()
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    assert held  # a carga está a caminho
    deliver(score(2), 4188)
    with refetches_ranking():
        held[0].fallback()
    assert ranking_gets() == 7
    with refetches_ranking():  # agrupado: 1,5 s depois da resposta
        pass
    assert ranking_gets() == 8
    assert not errors, errors


def test_competicao_melhores_segundos_troca_no_lugar_dos_botoes(open_page, fixtures):
    """Fase de grupos com outra classificação: os botões dizem o tipo de tabela ("Por grupo", não
    o nome da fase, que é o do seletor "Fase"). No celular, tocar não tira os botões do lugar: a
    tabela escolhida aparece logo abaixo deles, antes dos jogos, e o foco fica no botão tocado. No
    desktop, a troca é no alto da coluna da direita, como antes."""

    def prepare(api):
        api.rankings = [{"id": 9, "name": "Melhores segundos", "scope": "position"}]
        api.ranking_stage_ids = [6]

    page, api, errors = open_page("/competition.html?slug=copa-pernambuco&stage=6", width=390, prepare=prepare)
    page.wait_for_selector("#round-matches .group-row--head #standings-switch button")
    switch = page.locator("#standings-switch")
    assert switch.locator("button").all_inner_texts() == ["Por grupo", "Melhores segundos"]
    where = """() => {
      const sw = document.getElementById('standings-switch').getBoundingClientRect();
      const ranking = document.getElementById('ranking-standings');
      const table = ranking.hidden ? null : ranking.querySelector('.standings')?.getBoundingClientRect();
      const game = document.querySelector('#round-matches .match').getBoundingClientRect();
      return { top: Math.round(sw.top + scrollY), left: Math.round(sw.left), right: sw.left > game.right,
        below: table ? [Math.round(table.top - sw.bottom), table.bottom <= game.top] : null,
        focus: [document.activeElement?.textContent, document.activeElement?.getAttribute('aria-pressed')],
        caption: table ? ranking.querySelector('caption').firstChild.textContent : null };
    }"""
    before = page.evaluate(where)
    switch.get_by_role("button", name="Melhores segundos").click()
    page.wait_for_selector("#ranking-standings:not([hidden]) .standings")
    after = page.evaluate(where)
    assert (after["top"], after["left"]) == (before["top"], before["left"])  # os botões não saem do lugar
    assert 0 <= after["below"][0] <= 24 and after["below"][1]  # a tabela logo abaixo, antes dos jogos
    assert after["focus"] == ["Melhores segundos", "true"] and after["caption"] == "Melhores segundos"
    switch.get_by_role("button", name="Por grupo").click()
    page.wait_for_selector("#round-matches .group-row--head #standings-switch")
    back = page.evaluate(where)
    assert (back["top"], back["left"]) == (before["top"], before["left"]) and back["focus"] == ["Por grupo", "true"]
    # desktop: no alto da coluna da direita, nas duas tabelas
    page.set_viewport_size({"width": 1280, "height": 900})
    wide = page.evaluate(where)
    switch.get_by_role("button", name="Melhores segundos").click()
    page.wait_for_selector("#ranking-standings:not([hidden]) .standings")
    ranking = page.evaluate(where)
    assert wide["right"] and ranking["right"] and abs(ranking["top"] - wide["top"]) <= 1 and ranking["below"][0] <= 24
    assert ranking["focus"] == ["Melhores segundos", "true"]
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert not errors, errors


def test_competicao_turno_com_classificacao_geral_e_menu_ao_vivo(open_page, fixtures):
    """Fase de tabela única com classificação geral: "Só esta fase | Geral" (sem repetir o nome da
    fase do seletor) e o título diz o recorte ("Classificação · 2º turno", "Classificação geral"),
    com "Jogando agora" na legenda e a nota do "=" embaixo da tabela. O menu marca, também aqui,
    a competição com jogo ao vivo agora (GET /api/matches?status=live, atualizado pelo stream)."""
    comp = copy.deepcopy(fixtures["COMPETITION"])
    comp["competition"] = fixtures["COMPETITIONS"][2]  # Série A2
    comp["stages"] = [
        {"id": 2, "name": "1º turno", "format": "league", "position": 1, "rounds": [{"id": 39, "number": 1, "name": "Rodada 1"}]},
        {"id": 3, "name": "2º turno", "format": "league", "position": 2, "rounds": [{"id": 40, "number": 1, "name": "Rodada 1"}]},
    ]
    comp["current_stage_id"], comp["current_round_id"] = 3, 40
    comp["stage"] = {"id": 3, "name": "2º turno", "format": "league", "standings": {**fixtures["STANDINGS"], "stage_name": "2º turno"}, "matches": [], "ties": []}

    def prepare(api):
        api.competition = comp
        api.rankings = [{"id": 5, "name": "Classificação geral", "scope": "overall"}]
        api.ranking_stage_ids = [2, 3]

    page, api, errors = open_page("/competition.html?slug=serie-a2", width=360, prepare=prepare)
    page.wait_for_selector("#stage-standings .standings caption")
    switch = page.locator("#standings-switch")
    assert switch.locator("button").all_inner_texts() == ["Só esta fase", "Geral"]
    table = page.evaluate("""() => {
      const group = document.querySelector('#stage-standings .standings__group');
      return { caption: group.querySelector('caption').firstChild.textContent,
        legend: [...group.querySelectorAll('.legend__item')].map((li) => li.textContent),
        note: group.querySelector('.standings__note')?.textContent,
        noScroll: group.querySelector('.table-scroll').scrollWidth <= group.querySelector('.table-scroll').clientWidth };
    }""")
    assert table["caption"] == "Classificação · 2º turno"
    assert table["legend"] == ["Semifinal 1º–4º", "Rebaixamento 7º–8º", "Jogando agora"]
    assert table["note"] == "= Empatados em todos os critérios de desempate" and table["noScroll"]
    switch.get_by_role("button", name="Geral").click()
    page.wait_for_selector("#ranking-standings:not([hidden]) .standings caption")
    assert page.evaluate("document.querySelector('#ranking-standings caption').firstChild.textContent") == "Classificação geral"
    # menu: o Pernambucano tem jogo ao vivo agora (o ponto e o texto para leitor de tela)
    live = "() => [...document.querySelectorAll('#competitions-nav a')].filter((a) => a.querySelector('.comp-nav__live')).map((a) => [a.getAttribute('href'), a.querySelector('.visually-hidden').textContent])"
    assert page.evaluate(live) == [["/competition.html?slug=pernambucano", " (jogando agora)"]]
    assert any(r[1] == "/api/matches" and "status=live" in r[2] for r in api.requests)
    # os dois jogos ao vivo terminam: o ponto some
    done = [{**_summary(fixtures["MATCHES"][key]), "status": "finished", "version": 999} for key in ("live", "halfTime")]
    ping = _sse([("ping", {"server_time": _now_iso()}, None)])
    api.streams = [_sse([("match", {"stage_id": 3, "competition_id": 1, "match": m}, 5001 + i) for i, m in enumerate(done)]), ping]
    page.wait_for_function(f"() => ({live})().length === 0", timeout=10_000)
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
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


def test_operador_encerra_por_wo_com_time_placar_e_motivo(open_page):
    page, api, errors = open_page("/operator.html")
    page.wait_for_selector("#op-login:not([hidden])")
    page.fill("#login-username", "operador")
    page.fill("#login-password", "certa")
    page.click("#login-submit")
    page.wait_for_selector('#picker-list li[data-match-id="14"] .pick-item')
    page.click('#picker-list li[data-match-id="14"] .pick-item')  # Central × Salgueiro, agendado
    page.wait_for_selector('#status-actions button[data-action="walkover"]:not(:disabled)')
    page.click('#status-actions button[data-action="walkover"]')
    page.wait_for_selector("#status-dialog[open]")
    dialog = "#status-dialog"
    assert page.text_content(f"{dialog} [data-hook=status-title]") == "Encerrar a partida por W.O.?"
    assert page.is_visible(f"{dialog} [data-hook=status-wo-team-field]") and page.is_visible(f"{dialog} [data-hook=status-wo-score]")
    assert not page.is_visible(f"{dialog} [data-hook=status-kickoff-field]")
    # sem time, sem placar e sem motivo: a validação nativa barra o envio
    page.click(f"{dialog} [data-hook=status-ok]")
    assert api.status_posts == []
    # o time escolhido recebe o 3 × 0 padrão (aqui o visitante: 0 × 3), editável
    salgueiro = api.fx["MATCHES"]["scheduledToday"]["away"]["id"]
    page.select_option("#status-wo-team", value=str(salgueiro))
    assert [page.input_value(f"{dialog} [data-hook=status-wo-{side}]") for side in ("home", "away")] == ["0", "3"]
    page.fill(f"{dialog} [data-hook=status-wo-away]", "2")
    page.fill("#status-reason", "Central não compareceu")
    page.click(f"{dialog} [data-hook=status-ok]")
    page.wait_for_function("() => !document.querySelector('#status-dialog').open")
    page.wait_for_function("() => document.querySelector('.toast')?.textContent.includes('Encerrado')")
    assert api.status_posts == [
        {"action": "walkover", "reason": "Central não compareceu", "team_id": salgueiro, "home_score": 0, "away_score": 2}
    ]
    assert not errors, errors


def test_operador_lista_de_partidas_e_lista_de_listas(open_page):
    """Cada competição é um <li> com título e <ul> aninhada (axe: list); as setas atravessam os grupos."""
    page, _, errors = open_page("/operator.html", prepare=lambda api: setattr(api, "me", api.fx["ME"]))
    page.wait_for_selector('#picker-list li[data-match-id="12"] .pick-item')
    out = page.evaluate("""() => {
      const list = document.getElementById('picker-list');
      const items = [...list.querySelectorAll('.pick-item')];
      return {
        children: [...list.children].map((li) => [li.tagName, li.getAttribute('role'), li.className]),
        nested: items.every((b) => { const ul = b.closest('ul'); return ul !== list && ul.classList.contains('match-picker__sublist') && ul.parentElement.closest('ul') === list; }),
        labelled: [...list.querySelectorAll('.match-picker__sublist')].every((ul) => document.getElementById(ul.getAttribute('aria-labelledby'))?.classList.contains('match-picker__group-title')),
        region: (() => { const r = document.querySelector('[data-hook="picker"]'); return [r.tagName, r.getAttribute('role')]; })(),
        asides: document.querySelectorAll('#op-app aside').length,
        groupSizes: [...list.children].map((li) => li.querySelectorAll('.pick-item').length),
      };
    }""")
    assert out["children"] and all(tag == "LI" and role is None and cls == "match-picker__group" for tag, role, cls in out["children"])
    assert out["nested"] and out["labelled"]
    assert out["region"] == ["DIV", "region"] and out["asides"] == 0
    # ↓ no último jogo do 1º grupo vai para o 1º jogo do 2º grupo
    first_group = out["groupSizes"][0]
    page.locator("#picker-list .pick-item").nth(first_group - 1).focus()
    page.keyboard.press("ArrowDown")
    assert page.evaluate(
        "[...document.querySelectorAll('#picker-list .pick-item')].indexOf(document.activeElement)"
    ) == first_group
    assert not errors, errors


def test_operador_lances_primeiro_e_andamento_do_jogo_a_parte_com_confirmacao(open_page):
    """A API manda os estruturais antes do gol (ordem do catálogo): a tela mostra os lances
    primeiro e o andamento do jogo num grupo à parte, que sempre pede confirmação."""

    def prepare(api):
        api.me = api.fx["ME"]
        api.fx = {**api.fx, "AVAILABLE": {"events": ["half_time", "goal", "goal_annulled", "yellow_card", "substitution"], "status": ["suspend"]}}

    page, api, errors = open_page("/operator.html", prepare=prepare)
    page.click('#picker-list li[data-match-id="12"] .pick-item')
    page.wait_for_selector('#action-grid [data-type="goal"]')
    types = "(sel) => [...document.querySelectorAll(sel + ' .action-btn')].map((b) => b.dataset.type)"
    assert page.evaluate(types, "#action-grid") == ["goal", "goal_annulled", "yellow_card", "substitution"]
    assert page.evaluate(types, "#flow-actions") == ["half_time"]
    assert page.evaluate("""() => {
      const block = document.getElementById('flow-block'), grid = document.getElementById('action-grid');
      return !block.hidden && !!(grid.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING)
        && document.querySelector('#flow-actions .action-btn').classList.contains('action-btn--structural')
        && document.getElementById('flow-actions').getAttribute('role') === 'group';
    }""")
    # o <select> de tipo segue a mesma ordem: lances, depois o grupo "Andamento do jogo"
    page.click('#flow-actions [data-type="half_time"]')
    assert page.evaluate("document.querySelector('#flow-actions [data-type=half_time]').getAttribute('aria-pressed')") == "true"
    assert page.evaluate("[...document.querySelectorAll('#event-type option')].map((o) => [o.value, o.parentElement.tagName])") == [
        ["goal", "SELECT"], ["goal_annulled", "SELECT"], ["yellow_card", "SELECT"], ["substitution", "SELECT"], ["half_time", "OPTGROUP"],
    ]
    # enviar pede confirmação; "Voltar" não lança nada
    page.click('[data-hook="event-submit"]')
    page.wait_for_selector("#confirm-dialog[open]")
    assert page.evaluate("document.querySelector('#confirm-dialog [data-hook=confirm-title]').textContent") == "Encerrar o 1º tempo?"
    assert page.evaluate("document.querySelector('#confirm-dialog [data-hook=confirm-ok]').textContent") == "Encerrar 1º tempo"
    page.click('#confirm-dialog button[value="cancel"]')
    page.wait_for_selector("#confirm-dialog:not([open])", state="attached")
    assert api.posts == []
    # confirmado, lança (o aviso do servidor ainda passa pela confirmação de sempre)
    page.click('[data-hook="event-submit"]')
    page.wait_for_selector("#confirm-dialog[open]")
    page.click('#confirm-dialog [data-hook="confirm-ok"]')
    page.wait_for_function("() => document.querySelector('#confirm-dialog [data-hook=confirm-title]').textContent === 'Confirme o lançamento'")
    page.click('#confirm-dialog [data-hook="confirm-ok"]')
    page.wait_for_selector("#event-form-card[hidden]", state="attached")
    assert [p["body"]["type"] for p in api.posts] == ["half_time", "half_time"]
    # gol (lance de jogo) não pede a confirmação do andamento
    page.click('#action-grid [data-type="goal"]')
    assert page.evaluate("document.querySelector('#flow-actions [data-type=half_time]').getAttribute('aria-pressed')") == "false"
    assert not errors, errors


PICKER_STATE_JS = """() => {
  const sw = document.getElementById('picker-switch'), panel = document.getElementById('op-panel');
  const r = sw.getBoundingClientRect(), p = panel.getBoundingClientRect();
  return { expanded: sw.getAttribute('aria-expanded'), switchShown: r.height > 0, switchHeight: r.height,
    listShown: document.getElementById('picker-list').getBoundingClientRect().height > 0,
    text: sw.textContent.replace(/\\s+/g, ' ').trim(), above: r.bottom <= p.top, panelTop: p.top,
    viewport: innerHeight, focusOnSwitch: document.activeElement === sw };
}"""
PANEL_ON_SCREEN_JS = "() => { const t = document.getElementById('op-panel').getBoundingClientRect().top; return t >= 0 && t < innerHeight * 0.6; }"


def test_operador_celular_lista_recolhe_em_trocar_partida(open_page):
    """Celular (< 960 px): com a partida escolhida (?match= ou toque na lista), a lista recolhe em
    "Trocar partida", que mostra a partida atual logo acima do painel, já na tela; aberto, mostra a
    lista. No desktop, nada muda: a lista continua na coluna lateral."""
    page, _, errors = open_page("/operator.html?match=12", width=390, prepare=lambda api: setattr(api, "me", api.fx["ME"]))
    page.wait_for_selector('#action-grid [data-type="goal"]')
    out = page.evaluate(PICKER_STATE_JS)
    assert out["switchShown"] and not out["listShown"] and out["expanded"] == "false", out
    assert out["text"].startswith("Trocar partida") and "SPT" in out["text"] and "NAU" in out["text"], out
    assert out["above"] and out["switchHeight"] >= 44, out
    assert page.evaluate(PANEL_ON_SCREEN_JS), out  # aberto/recarregado com ?match=: o painel já está na tela
    # aberto, mostra a lista; tocar noutra partida recolhe de novo, com o foco no botão e o placar na tela
    page.click("#picker-switch")
    out = page.evaluate(PICKER_STATE_JS)
    assert out["listShown"] and out["expanded"] == "true", out
    page.click('#picker-list li[data-match-id="14"] .pick-item')
    page.wait_for_function("() => document.getElementById('op-scoreboard').textContent.includes('Central')")
    page.wait_for_function(PANEL_ON_SCREEN_JS)
    out = page.evaluate(PICKER_STATE_JS)
    assert not out["listShown"] and out["expanded"] == "false" and out["focusOnSwitch"], out
    assert "CEN" in out["text"] and "SAL" in out["text"] and "match=14" in page.url, out
    # desktop: sem o botão, lista na coluna lateral
    page.set_viewport_size({"width": 1280, "height": 900})
    out = page.evaluate(PICKER_STATE_JS)
    assert not out["switchShown"] and out["listShown"], out
    assert len(page.evaluate("getComputedStyle(document.querySelector('.op-layout')).gridTemplateColumns").split()) == 2
    assert not errors, errors


def test_operador_rotulos_distintos_e_um_icone_por_acao(open_page):
    """Fechar (formulário) × Desfazer (lance) × Cancelar partida (status), também nos diálogos;
    "Tipo do gol", dica do gol contra e do minuto; o efeito das informações parciais; e nenhum
    ícone repetido entre ações (Acréscimos × Relógio do jogo, Fim de jogo × W.O. etc.)."""

    def prepare(api):
        api.me = api.fx["ME"]
        events = [*api.fx["AVAILABLE"]["events"], "clock_adjust"]
        api.fx = {**api.fx, "AVAILABLE": {"events": events, "status": ["suspend", "cancel"]}}

    page, _, errors = open_page("/operator.html?match=12", prepare=prepare)
    page.wait_for_selector('#action-grid [data-type="clock_adjust"]')
    icons = page.evaluate("""() => {
      const href = (el) => el.querySelector('use').getAttribute('href');
      return {
        actions: [...document.querySelectorAll('#action-grid .action-btn, #flow-actions .action-btn')].map((b) => [b.dataset.type, href(b)]),
        status: [...document.querySelectorAll('#status-actions button')].map((b) => [b.dataset.action, href(b), b.textContent.trim()]),
        title: href(document.getElementById('status-actions-title')),
      };
    }""")
    actions = dict(icons["actions"])
    assert actions["stoppage_time"] != actions["clock_adjust"]
    assert len(set(actions.values())) == len(actions), icons["actions"]
    status = [href for _, href, _ in icons["status"]]
    assert len(set(status)) == len(status), icons["status"]
    assert not set(status) & set(actions.values()), icons  # W.O. × Fim de jogo, Cancelar × Pênalti perdido
    assert icons["title"] not in status
    assert {action: text for action, _, text in icons["status"]}["cancel"] == "Cancelar partida"
    partial = "() => { const i = document.getElementById('op-partial-info'); return document.getElementById(i.getAttribute('aria-describedby')).textContent; }"
    assert page.evaluate(partial) == "No site, o jogo aparece sem minuto."

    # formulário do gol: "Tipo do gol", minuto sugerido com dica coerente, "Fechar"
    page.click('#action-grid [data-type="goal"]')
    described = "(sel) => { const i = document.querySelector(sel); const h = document.getElementById(i.getAttribute('aria-describedby') || ''); return h && !h.hidden ? h.textContent : null; }"
    minute = '#event-form input[name="minute"]'
    player = '#event-form input[name="payload.player"]'
    assert page.evaluate("() => { const s = document.querySelector('#event-form select[name=\"payload.origin\"]'); return document.querySelector(`label[for='${s.id}']`).textContent; }") == "Tipo do gol"
    assert page.text_content('[data-hook="event-cancel"]').strip() == "Fechar"
    assert page.input_value(minute) != ""
    assert page.evaluate(described, minute) == "Sugerido pelo relógio; apague para usar o minuto padrão do lance."
    page.fill(minute, "")
    assert page.evaluate(described, minute) == "Em branco, vale o minuto padrão do lance."
    # gol contra: a dica diz de que time é o jogador
    assert page.evaluate(described, player) is None
    page.select_option('#event-form select[name="payload.origin"]', "own_goal")
    assert page.evaluate(described, player) == "Gol contra: jogador do adversário do time beneficiado."
    page.check("#event-form [data-field=team] input[type=radio] >> nth=0", force=True)
    assert page.evaluate(described, player) == "Gol contra: jogador do adversário (Náutico)."
    page.click('[data-hook="event-cancel"]')
    page.wait_for_selector("#event-form-card[hidden]", state="attached")

    # lance: "Desfazer" (o nome acessível contém o texto visível) e o diálogo com "Manter"
    void = page.locator("#op-timeline [data-hook=void]").first
    assert void.text_content().strip() == "Desfazer"
    assert void.get_attribute("aria-label").startswith("Desfazer lançamento: ")
    void.click()
    page.wait_for_selector("#void-dialog[open]")
    assert page.text_content("#void-title") == "Desfazer este lançamento?"
    assert [t.strip() for t in page.locator("#void-dialog .dialog__foot button").all_text_contents()] == ["Manter", "Desfazer lançamento"]
    page.click('#void-dialog button[value="cancel"]')
    page.wait_for_selector("#void-dialog:not([open])", state="attached")

    # partida: "Cancelar partida", com título próprio
    page.click('#status-actions button[data-action="cancel"]')
    page.wait_for_selector("#status-dialog[open]")
    assert page.text_content("#status-dialog [data-hook=status-title]") == "Cancelar a partida?"
    assert page.text_content("#status-dialog [data-hook=status-ok]") == "Cancelar partida"
    assert page.text_content('#status-dialog button[value="cancel"]') == "Voltar"
    page.click('#status-dialog button[value="cancel"]')
    assert not errors, errors


HIT_AREA_JS = """(selectors) => selectors.map((sel) => {
  const el = document.querySelector(sel);
  if (!el) return [sel, null, null];
  el.scrollIntoView({ block: 'center' });
  const r = el.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const hit = (x, y) => el.contains(document.elementFromPoint(x, y));
  let h = 0, w = 0;
  for (let y = Math.floor(r.top) - 12; y <= r.bottom + 12; y++) if (hit(cx, y)) h++;
  for (let x = Math.floor(r.left) - 12; x <= r.right + 12; x++) if (hit(x, cy)) w++;
  return [sel, w, h];
})"""


def test_operador_celular_alvos_de_toque_de_44px(open_page):
    """Celular: Editar/Desfazer de cada lance, Sair, Administração, Atualizar lista, atalhos e
    "Informações parciais" com área de toque de 44 px; Editar e Desfazer afastados."""

    def prepare(api):
        me = copy.deepcopy(api.fx["ME"])
        me["user"]["permissions"]["admin_site"] = True
        api.me = me

    page, _, errors = open_page("/operator.html?match=12", width=390, prepare=prepare)
    page.wait_for_selector("#op-timeline [data-hook=void]")
    page.click("#picker-switch")  # abre a lista para medir o "Atualizar lista"
    sizes = page.evaluate(HIT_AREA_JS, [
        "#logout-button", '[data-hook="admin-link"]', "#picker-refresh", "#op-match-tools a.btn",
        ".op-tools__switch", "#op-timeline [data-hook=edit]", "#op-timeline [data-hook=void]",
    ])
    small = [s for s in sizes if s[1] is None or s[1] < 44 or s[2] < 44]
    assert not small, sizes
    gap = page.evaluate("""() => {
      const li = [...document.querySelectorAll('#op-timeline li')].find((l) => l.querySelector('[data-hook=edit]') && l.querySelector('[data-hook=void]'));
      return li.querySelector('[data-hook=void]').getBoundingClientRect().left - li.querySelector('[data-hook=edit]').getBoundingClientRect().right;
    }""")
    assert gap >= 8, gap
    assert not errors, errors


NAMES_JS = """() => [...document.querySelectorAll('.match .team__name')].map((name) => {
  const shown = [...name.children].find((c) => getComputedStyle(c).display !== 'none');
  const lh = parseFloat(getComputedStyle(name).lineHeight);
  return { kind: shown.classList.contains('team__short') ? 'short' : 'full', text: shown.textContent,
    // corte do nome dentro dele (no placar estreito o .team__full corta o que não cabe) ou para fora
    lines: shown.getBoundingClientRect().height / lh,
    overflow: Math.max(name.scrollWidth - name.clientWidth, shown.scrollWidth - shown.clientWidth) };
})"""


@pytest.mark.parametrize("width", [320, 360, 375, 1024])
@pytest.mark.parametrize("url", ["/", "/competition.html?slug=pernambucano-raiz"])
def test_nome_do_time_nao_quebra_letra_a_letra(open_page, url, width):
    """Nome completo em no máximo 2 linhas (quebra só entre palavras), também no celular, onde
    fica embaixo do escudo (a sigla não aparece mais no placar)."""
    page, _, errors = open_page(url, width=width)
    page.wait_for_selector("article.match .team__name")
    page.wait_for_timeout(200)
    names = page.evaluate(NAMES_JS)
    assert names
    bad = [n for n in names if n["lines"] > (1.5 if n["kind"] == "short" else 2.5) or n["overflow"] > 1]
    assert not bad, bad
    if width <= 375:
        assert {n["kind"] for n in names} == {"full"}
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert not errors, errors


META_LINES_JS = """() => [...document.querySelectorAll('article.match')].map((card) => {
  // linhas de um bloco flex: centros verticais distintos dos filhos (alinhados ao centro)
  const lines = (el) => (el ? new Set([...el.children].map((c) => c.getBoundingClientRect()).filter((r) => r.height > 1)
    .map((r) => Math.round((r.top + r.bottom) / 2))).size : 0);
  return { id: card.dataset.matchId, status: card.dataset.status,
    when: lines(card.querySelector('.match__lead')), where: lines(card.querySelector('.match__place')) };
}).filter((c) => c.when)"""


@pytest.mark.parametrize("width", [360, 390])
@pytest.mark.parametrize("url", ["/", "/competition.html?slug=copa-pernambuco"])
def test_faixa_do_card_no_celular_em_ate_tres_linhas(open_page, url, width):
    """Celular: "onde" numa linha só (o estádio corta com reticências, a cidade fica inteira) e a
    faixa toda em até 3 linhas (normalmente 2: "quando" e "onde"; rodada/fase cabe numa a mais)."""
    page, _, errors = open_page(url, width=width)
    page.wait_for_selector("article.match .match__meta")
    page.wait_for_timeout(200)
    cards = page.evaluate(META_LINES_JS)
    assert cards
    bad = [c for c in cards if c["where"] > 1 or c["when"] + c["where"] > 3]
    assert not bad, bad
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert not errors, errors


@pytest.mark.parametrize("width", [320, 390])
def test_logo_horizontal_largo_nao_empurra_o_cabecalho(open_page, width):
    """BRAND_LOGO_URL de qualquer proporção (aqui 10:1): relógio e tema continuam na tela."""
    wide = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="60" viewBox="0 0 600 60"><rect width="600" height="60" fill="#12306B"/></svg>'
    page, _, errors = open_page("/", width=width)
    page.route("**/static/img/logo*.svg", lambda route: route.fulfill(body=wide, content_type="image/svg+xml"))
    page.reload()
    page.wait_for_selector("article.match")
    out = page.evaluate("""() => {
      const logo = document.querySelector('.brand__logo--light');
      return { scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth,
        toggle: document.querySelector('[data-theme-toggle]').getBoundingClientRect().right,
        clock: document.getElementById('brasilia-clock').getBoundingClientRect().right,
        logo: [logo.naturalWidth, logo.getBoundingClientRect().width, logo.getBoundingClientRect().height] };
    }""")
    assert out["logo"][0] == 600 and out["logo"][1] <= 240  # carregou e foi limitado
    assert out["scroll"] <= out["client"], out
    assert out["toggle"] <= width - 16 + 0.5 and out["clock"] < out["toggle"], out
    assert not errors, errors


# --- Compartilhar e link do jogo -----------------------------------------------------------

WHATSAPP_SPY = "() => { window.open = (href) => { window.__opened = decodeURIComponent(href.split('?text=')[1]); }; }"


def test_previa_do_link_do_jogo():
    from core.share import match_preview

    base = {
        "competition": {"name": "Pernambucano"}, "round": {"name": "Rodada 5"}, "status_label": "Encerrado",
        "home": {"id": 1, "name": "Sport"}, "away": {"id": 2, "name": "Náutico"}, "home_penalties": None, "away_penalties": None,
    }
    finished = {
        **base, "status": "finished", "home_score": 2, "away_score": 1, "walkover": None,
        "goals": [
            {"team_side": "home", "player": "Fulano", "minute_label": "12'", "origin": "open_play"},
            {"team_side": "away", "player": "Beltrano", "minute_label": "45'", "origin": "penalty"},
            {"team_side": "home", "player": None, "minute_label": "", "origin": "own_goal"},
        ],
    }
    assert match_preview(finished) == (
        "Sport 2 × 1 Náutico",
        "Pernambucano · Rodada 5 · Encerrado. Gols: Sport: Fulano 12', a confirmar (contra); Náutico: Beltrano 45' (pên.)",
    )
    scheduled = {**base, "status": "scheduled", "home_score": None, "away_score": None, "goals": [], "kickoff_at": "2026-10-18T19:00:00Z"}
    assert match_preview(scheduled) == ("Sport × Náutico", "Pernambucano · Rodada 5 · 18/10 às 16:00")
    walkover = {**finished, "home_score": 0, "away_score": 3, "goals": [], "walkover": {"team_id": 2, "reason": "x"}}
    assert match_preview(walkover) == ("Sport 0 × 3 Náutico", "Pernambucano · Rodada 5 · Encerrado · W.O. em favor do Náutico")


def test_home_com_jogo_compartilhado_traz_a_previa_e_o_dia(client, db):
    from tests.factories import make_league, make_match, utc

    league = make_league()
    sport, nautico = league["teams"][0], league["teams"][1]
    match = make_match(league["stage"], sport, nautico, kickoff_at=utc(2026, 10, 18, 2, 30), round=league["rounds"][0])
    response = client.get(f"/?jogo={match.id}")
    assert response.status_code == 200 and response["Cache-Control"] == "public, max-age=60"
    html = response.content.decode()
    assert f'<meta property="og:title" content="{sport.name} × {nautico.name}">' in html
    assert f"<title>{sport.name} × {nautico.name} · " in html
    assert 'data-focus-day="2026-10-17"' in html  # 23h30 de Brasília: o dia é o de Brasília
    for value in ("999999", "abc", "9" * 12):  # jogo que não existe ou id inválido: a home de sempre
        html = _html(client, f"/?jogo={value}")
        assert "og:title" not in html and "data-focus-day" not in html


def test_home_abre_no_dia_do_jogo_compartilhado_com_o_card_aberto(open_page):
    # o HTML que o servidor manda para /?jogo=12 com o jogo noutro dia (sem banco: o navegador
    # roda num loop assíncrono); a API de exemplo devolve a home do dia pedido
    request = RequestFactory().get("/?jogo=12")
    context = {"page": "home", "share_title": "Sport 2 × 1 Náutico", "share_description": "Pernambucano", "share_url": "http://fdr.test/?jogo=12", "focus_day": "2030-01-05"}
    html = render_to_string("index.html", context, request=request)
    page, api, errors = open_page("/?jogo=12", prepare=lambda api: api.pages.__setitem__("/", html))
    page.wait_for_selector('.match[data-match-id="12"] > .match__details[open] .tl-item')
    assert any(path == "/api/home" and "date=2030-01-05" in query for _, path, query, _ in api.requests)
    assert page.get_attribute("#home-date", "datetime") == "2030-01-05"
    assert page.evaluate("""() => { const r = document.querySelector('.match[data-match-id="12"]').getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight; }""")
    assert page.locator(".match > .match__details[open]").count() == 1  # só o do link
    page.click("#day-next")  # trocar de dia tira o ?jogo da URL
    page.wait_for_function("() => !location.search.includes('jogo')")
    assert not errors, errors


def test_resultados_no_whatsapp_da_rodada_e_do_dia(open_page, fixtures):
    page, api, errors = open_page("/competition.html?slug=x")
    page.wait_for_selector("#round-share:not([hidden])")
    page.evaluate(WHATSAPP_SPY)
    page.click("#round-share")
    page.wait_for_function("() => window.__opened")
    lines = page.evaluate("window.__opened").split("\n")
    title = page.text_content("[data-hook=competition-name]").strip()
    cards = page.locator("#round-matches .match").count()
    assert lines[0].startswith(f"*{title}") and lines[0].endswith("*") and lines[1] == ""
    assert len(lines) == cards + 4 and lines[-2:] == ["", "🔗 " + page.evaluate("location.href")]

    # classificação (fase de grupos: uma tabela por grupo): o botão no canto manda a tabela do
    # grupo, uma linha por time, com o link da página
    page, api, errors_groups = open_page("/competition.html?slug=x&stage=6")
    page.wait_for_selector("#round-matches .group-row__table .standings__share")
    table = page.locator("#round-matches .group-row__table .standings__group").first
    table.locator(".standings__share").click()  # a janela com a imagem da tabela
    page.wait_for_selector("dialog.share-sheet .share-sheet__img:not([hidden])")
    assert page.text_content("dialog.share-sheet .dialog__title") == "Compartilhar classificação"
    href = page.get_attribute("dialog.share-sheet [data-hook=share-whatsapp]", "href")  # sem Web Share: o link do WhatsApp
    assert href.startswith("https://api.whatsapp.com/send?text=")
    lines = unquote(href.split("?text=", 1)[1]).split("\n")
    group = table.locator("caption").text_content().replace("Ao vivo", "").strip()
    assert lines[0].lower() == f"*classificação do {group.lower()}*"  # é de grupo: diz qual
    round_name = page.text_content("#round-label").strip()
    assert lines[1] == f"{title} · Fase de grupos · {round_name}"  # competição · fase · rodada
    assert len(lines) == table.locator("tbody tr").count() + 5 and lines[2] == ""
    lines = lines[2:]  # a partir da linha em branco antes dos times
    assert re.fullmatch(r"1\. .+ — \d+ pts? · \d+J · GP \d+ · SG [+-]?\d+", lines[1]) and lines[-1] == "🔗 " + page.evaluate("location.href")
    errors += errors_groups

    page, api, errors_home = open_page("/")
    page.wait_for_selector("[data-hook=competition] [data-hook=competition-share]")
    page.evaluate(WHATSAPP_SPY)
    section = page.locator("[data-hook=competition]").first
    section.locator("[data-hook=competition-share]").click()
    page.wait_for_function("() => window.__opened")
    lines = page.evaluate("window.__opened").split("\n")
    name = section.locator("[data-hook=competition-name]").text_content().strip()
    assert lines[0].startswith(f"*{name} · ") and len(lines) == section.locator(".match").count() + 4
    assert re.fullmatch(r"🔗 http://fdr\.test/\?date=\d{4}-\d\d-\d\d", lines[-1])
    assert not errors and not errors_home, errors + errors_home


def test_home_no_celular_barra_do_dia_ao_vivo_nomes_e_gaveta(open_page, fixtures):
    page, api, errors = open_page("/", width=390)
    page.wait_for_selector("[data-hook=competition] .match")
    # no topo, o seletor de dias está na tela: a barra fixa não aparece (nunca os dois juntos); o
    # atalho "Ao vivo" só existe nela
    assert page.is_hidden("[data-hook=day-bar]") and page.locator("[data-hook=live-jump]").count() == 1
    # ao rolar além do seletor, a barra aparece com o dia e "Ao vivo N"
    page.evaluate("window.scrollTo(0, document.getElementById('competitions').getBoundingClientRect().top + scrollY + 400)")
    page.wait_for_function("() => !document.querySelector('[data-hook=day-bar]').hidden")
    live = page.evaluate("[...document.querySelectorAll('#competitions .match')].filter((c) => c.dataset.status === 'live').length")
    chip = page.locator("[data-hook=day-bar] [data-hook=live-jump]")
    assert live > 0 and chip.is_visible() and chip.locator("[data-hook=live-count]").text_content() == str(live)
    # cada toque leva ao próximo jogo ao vivo, no topo da tela, sem ficar atrás da barra
    chip.click()
    page.wait_for_timeout(700)  # rolagem suave
    first = page.evaluate("""() => {
      const card = [...document.querySelectorAll('#competitions .match')].find((c) => c.dataset.status === 'live');
      return [card.getBoundingClientRect().top, document.querySelector('[data-hook=day-bar]').getBoundingClientRect().bottom];
    }""")
    assert first[1] <= first[0] < first[1] + 40
    # a barra repete o dia; o › dela troca o dia (como o do seletor) e volta ao topo
    assert page.text_content("[data-hook=day-bar-date]").strip() == "Hoje"
    page.focus("[data-hook=day-bar-next]")
    page.keyboard.press("Enter")
    page.wait_for_function("() => new URLSearchParams(location.search).has('date') && window.scrollY === 0")
    page.wait_for_function("() => document.activeElement.id === 'day-next'")  # terminada a troca, o foco não se perde
    page.wait_for_function("() => document.querySelector('[data-hook=day-bar]').hidden")
    # placar estreito: o nome do time (não a sigla), ao lado do escudo
    card = page.locator("#competitions .match").first
    assert card.locator(".team__full").first.is_visible() and not card.locator(".team__short").first.is_visible()
    assert page.evaluate("""() => {
      const team = document.querySelector('#competitions .match .team--away');
      const [crest, name] = [team.querySelector('.crest'), team.querySelector('.team__name')].map((el) => el.getBoundingClientRect());
      return name.left >= crest.right && Math.abs((name.top + name.bottom) / 2 - (crest.top + crest.bottom) / 2) < 12;
    }""")
    # cabeçalho da competição: texto curto e visível ("Compartilhar", "Ver competição"), numa linha
    # embaixo do título, em alvos de 44 px de altura
    share = page.locator("[data-hook=competition] [data-hook=competition-share]").first
    more = page.locator("[data-hook=competition] [data-hook=competition-more]").first
    assert share.inner_text().strip() == "Compartilhar" and more.inner_text().strip() == "Ver competição"
    title = page.locator("[data-hook=competition] .section-head__titles").first.bounding_box()
    assert share.bounding_box()["y"] >= title["y"] + title["height"] and share.bounding_box()["height"] >= 44
    assert share.get_attribute("aria-label") == "Compartilhar os resultados no WhatsApp"
    # botão de compartilhar do card: área de toque além do desenho de 32 px
    box = card.locator(".match__share").bounding_box()
    hit = page.evaluate(f"document.elementFromPoint({box['x'] - 4}, {box['y'] + box['height'] / 2})?.closest('.match__share') !== null")
    assert box["width"] == 32 and hit
    # a janela de compartilhar abre como gaveta, presa embaixo e na largura da tela
    card.locator(".match__share").click()
    page.wait_for_selector("dialog.share-sheet[open]")
    page.wait_for_timeout(300)  # animação
    sheet = page.locator("dialog.share-sheet").bounding_box()
    assert abs(sheet["y"] + sheet["height"] - 900) <= 1 and sheet["width"] == 390
    assert not errors, errors


# contraste WCAG entre duas cores calculadas ("rgb(r, g, b)")
CONTRAST_JS = """(pair) => {
  const lum = (css) => {
    const [r, g, b] = css.match(/[\\d.]+/g).slice(0, 3).map(Number).map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [a, b] = pair.map(lum).sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}"""


def test_home_tablet_barra_do_dia_e_ao_vivo_ate_960(open_page):
    """Coluna única (< 960 px, tablet): barra fixa do dia e "Ao vivo N" como no celular; com a
    classificação ao lado (>= 960 px), nenhum dos dois (barra escondida e inerte). O atalho usa
    --on-vermelho, como a pílula AO VIVO: contraste AA também no tema escuro."""
    page, _, errors = open_page("/", width=768)
    page.wait_for_selector("[data-hook=competition] .match")
    bar = "document.querySelector('[data-hook=day-bar]')"
    assert page.is_hidden("[data-hook=day-bar]") and page.evaluate(f"{bar}.inert")
    page.evaluate("window.scrollTo(0, document.getElementById('competitions').getBoundingClientRect().top + scrollY + 400)")
    page.wait_for_function(f"() => !{bar}.hidden")
    assert not page.evaluate(f"{bar}.inert") and page.evaluate("document.body.classList.contains('has-day-bar')")
    chip = page.locator("[data-hook=day-bar] [data-hook=live-jump]")
    assert chip.is_visible()
    chip.click()
    page.wait_for_timeout(700)  # rolagem suave
    first = page.evaluate(f"""() => {{
      const card = [...document.querySelectorAll('#competitions .match')].find((c) => c.dataset.status === 'live');
      return [card.getBoundingClientRect().top, {bar}.getBoundingClientRect().bottom];
    }}""")
    assert first[1] <= first[0] < first[1] + 40
    # tema escuro: texto e bolinha do atalho na cor do texto da pílula AO VIVO, com 4,5:1
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    colors = page.evaluate("""() => {
      const chip = document.querySelector('[data-hook=day-bar] [data-hook=live-jump]');
      const style = getComputedStyle(chip);
      return { fg: style.color, bg: style.backgroundColor,
        dot: getComputedStyle(chip.querySelector('.live-dot')).backgroundColor,
        pill: getComputedStyle(document.querySelector('#competitions .pill--live')).color };
    }""")
    assert colors["fg"] == colors["dot"] == colors["pill"], colors
    assert page.evaluate(CONTRAST_JS, [colors["fg"], colors["bg"]]) >= 4.5, colors
    # duas colunas: a barra sai (e fica fora do Tab), sem o espaço guardado nos cards
    page.set_viewport_size({"width": 1024, "height": 900})
    page.wait_for_function(f"() => {bar}.hidden && {bar}.inert && !document.body.classList.contains('has-day-bar')")
    assert not errors, errors


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_cabecalho_botao_do_minuto_com_borda_legenda_e_nome_com_o_texto(open_page, width):
    """Formato do minuto no topo: botão com borda e a legenda "Minuto" no desenho da legenda do
    relógio; o nome acessível contém o texto visível (WCAG 2.5.3) e o trecho escondido diz o que
    o toque faz; no celular, alvo de 44 px; a 320 px, logo + relógio + minuto + tema numa linha."""
    page, _, errors = open_page("/", width=width)
    page.wait_for_selector("article.match")
    button = page.locator("[data-minute-format-toggle]")
    assert button.get_attribute("aria-label") is None
    # texto na tela (sem o trecho escondido): o valor e a legenda, que abrem o nome acessível
    visible = """() => {
      const b = document.querySelector('[data-minute-format-toggle]');
      return `${b.querySelector('.minute-toggle__value').innerText.trim()} ${b.querySelector('.minute-toggle__label').firstChild.textContent}`;
    }"""
    assert page.evaluate(visible) == "45+2' Minuto"
    off = page.get_by_role("button", name="45+2' Minuto — ver por tempo (1T/2T)", exact=True, pressed=False)
    assert off.count() == 1
    layout = """() => {
      const b = document.querySelector('[data-minute-format-toggle]');
      const s = getComputedStyle(b);
      const label = getComputedStyle(b.querySelector('.minute-toggle__label'));
      const clock = getComputedStyle(document.querySelector('.clock__label'));
      const rects = ['.brand__logo--light', '.clock', '[data-minute-format-toggle]', '[data-theme-toggle]']
        .map((sel) => document.querySelector(sel).getBoundingClientRect());
      return {
        border: s.borderTopStyle === 'solid' && parseFloat(s.borderTopWidth) >= 1 && s.borderTopColor !== s.backgroundColor,
        sameLabel: ['fontFamily', 'fontSize', 'fontWeight', 'letterSpacing', 'textTransform', 'color'].every((k) => label[k] === clock[k]),
        rects: rects.map((r) => ({ left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height })),
        scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth,
      };
    }"""
    out = page.evaluate(layout)
    logo, clock, minute, theme = out["rects"]
    assert out["border"] and out["sameLabel"], out
    assert out["scroll"] <= out["client"], out
    # numa linha: logo, relógio, minuto e tema, nessa ordem, dentro da margem lateral
    assert logo["right"] <= clock["left"] and clock["right"] <= minute["left"] and minute["right"] <= theme["left"], out
    assert theme["right"] <= width - 16 + 0.5 and max(r["top"] for r in out["rects"]) < min(r["bottom"] for r in out["rects"]), out
    assert logo["width"] >= 85 and minute["height"] >= 40, out
    if width < 640:
        # toque de 44 px: o ::after estende a área além do desenho de 40 px
        hit = "([x, y]) => document.elementFromPoint(x, y)?.closest('[data-minute-format-toggle]') !== null"
        x = (minute["left"] + minute["right"]) / 2
        assert page.evaluate(hit, [x, minute["top"] - 2]) and page.evaluate(hit, [x, minute["bottom"] + 2])
    # ao tocar, o valor troca no lugar (largura fixa: o relógio não pula) e o nome acompanha
    button.click()
    assert page.evaluate(visible) == "47' 1T Minuto"
    assert page.get_by_role("button", name="47' 1T Minuto — ver por tempo (1T/2T)", exact=True, pressed=True).count() == 1
    assert page.evaluate(layout)["rects"][1]["left"] == clock["left"]
    assert not errors, errors


@pytest.mark.parametrize("width", [320, 1280])
def test_ultimos_gols_som_e_notificacoes_curtos_no_celular(open_page, width):
    """Celular (< 640 px): "Som" e "Notificações" (com ícone) na linha do título, em todos os
    estados; o nome acessível é o texto visível; toque de 44 px. Desktop: rótulos longos."""
    page, _, errors = open_page("/", width=width)
    page.wait_for_selector("article.match")
    mobile = width < 640
    # como num celular (sem notificações do sistema): só o som, na linha do título
    same_line = """(id) => {
      const [t, b] = [document.querySelector('.goals__title'), document.getElementById(id)].map((el) => el.getBoundingClientRect());
      return b.top < t.bottom && t.top < b.bottom && b.left > t.right;
    }"""
    assert page.evaluate(same_line, "toggle-sound")
    # botão de notificações (HTTPS no computador) e os dois estados de cada botão
    page.evaluate("document.getElementById('toggle-notifications').hidden = false")
    states = {
        "false": ("Som", "Notificações") if mobile else ("Ativar som", "Ativar notificações"),
        "true": ("Som", "Notificações") if mobile else ("Som ligado", "Notificações ligadas"),
    }
    for pressed, names in states.items():
        page.evaluate(f"for (const id of ['toggle-sound', 'toggle-notifications']) document.getElementById(id).setAttribute('aria-pressed', '{pressed}')")
        for button_id, name in zip(("toggle-sound", "toggle-notifications"), names):
            assert page.inner_text(f"#{button_id}").strip() == name
            role = page.get_by_role("button", name=name, exact=True, pressed=pressed == "true")
            assert role.count() == 1 and role.get_attribute("id") == button_id
    if mobile:
        box = page.locator("#toggle-sound").bounding_box()
        hit = "([x, y]) => document.elementFromPoint(x, y)?.closest('#toggle-sound') !== null"
        x = box["x"] + box["width"] / 2
        assert page.evaluate(hit, [x, box["y"] - 5]) and page.evaluate(hit, [x, box["y"] + box["height"] + 5])
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert not errors, errors
