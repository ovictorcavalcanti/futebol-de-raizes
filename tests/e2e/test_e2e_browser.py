"""Ponta a ponta no navegador: servidor ASGI de verdade (uvicorn, um processo só), banco
próprio carregado com o seed e o Chromium do Playwright. Prova o "pronto quando" do front
(docs/PLANO.md, fases 7 e 8) sem nada simulado:

* fase 7 — um operador lança um jogo inteiro pela tela (reagendar, início, gols dos dois
  lados, cartão, aviso com confirmação, gol anulado pelo formulário, "Cancelar lançamento",
  suspender e retomar, intervalo, 2º tempo e fim), conferindo o placar da tela a cada passo;
* fase 8 — home e página da competição abertas ANTES do lance acompanham o gol sem
  recarregar (placar, "É gol!", últimos gols, classificação), com som e notificação para
  quem ativou; a anulação gera o aviso de correção e tira o gol da lista;
* mata-mata (confrontos com agregado e vencedor; a final indo aos pênaltis com a página
  aberta), navegação de rodadas e fases, tema que persiste, logo e nome vindos da
  configuração da marca (também no admin), nenhum erro no console, nenhuma rolagem
  horizontal a 390 px e o stream voltando sozinho (com o que perdeu) depois de o servidor
  reiniciar;
* acabamento: os lances antes do "Andamento do jogo" (estruturais à parte, sempre com
  confirmação), nomes dos times sem quebrar no meio da palavra de 320 a 1024 px, CLS < 0,1
  no carregamento da home e da competição (1280 e 390 px) e o admin claro por padrão e no
  mesmo tema do site.

Só roda com E2E=1 (preparar o banco leva ~40 s):

    E2E=1 python -m pytest tests/e2e -o addopts="" -q

Variáveis opcionais: E2E_DB_NAME (banco recriado do zero; padrão fdr_e2e_browser),
E2E_BASE_URL (usa um servidor já de pé, com seed, em vez de subir um; o teste do logo
pula), E2E_CHROMIUM (executável do Chromium).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ("operador", os.environ.get("SEED_OPERATOR_PASSWORD") or "raizes-operador-2026")
DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
CHROMIUM_ARGS = ["--disable-gpu"]  # política de autoplay padrão: o som depende do clique em "Ativar som"
EXPECT_TIMEOUT = 10_000
BRASILIA = ZoneInfo("America/Sao_Paulo")

pytestmark = pytest.mark.skipif(os.environ.get("E2E") != "1", reason="ponta a ponta: rode com E2E=1")

# Instrumenta o som e a notificação do sistema antes de qualquer script da página: conta as
# chamadas de play() e registra cada Notification construída (a real continua sendo criada).
INSTRUMENT = r"""
(() => {
  window.__played = 0;    // chamadas de play()
  window.__playedOk = 0;  // as que o navegador deixou tocar (a recusa segue para a página)
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    window.__played += 1;
    return play.apply(this, args).then((value) => { window.__playedOk += 1; return value; });
  };
  window.__notifications = [];
  const Real = window.Notification;
  if (typeof Real !== 'function') return;
  function Recorded(title, options) {
    const record = { title: String(title), body: (options && options.body) || '', tag: (options && options.tag) || '', closed: false };
    if (record.tag !== 'fdr-probe') window.__notifications.push(record);
    let real = null;
    try { real = new Real(title, options); } catch (e) { /* sem suporte: só o registro */ }
    return {
      set onclick(fn) { if (real) real.onclick = fn; },
      set onerror(fn) { if (real) real.onerror = fn; },
      close() { record.closed = true; if (real) real.close(); },
    };
  }
  Object.defineProperty(Recorded, 'permission', { get: () => Real.permission });
  Recorded.requestPermission = (...args) => Real.requestPermission(...args);
  window.Notification = Recorded;
})();
"""


# --- Infraestrutura: banco, servidor e navegador -------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - localhost
        return json.loads(response.read().decode())


def _wait_health(base: str, proc: subprocess.Popen | None, log: Path | None, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"o servidor caiu ao subir:\n{log.read_text() if log else ''}")
        try:
            if _get_json(f"{base}/health").get("status") == "ok":
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"o servidor não respondeu em {timeout:.0f} s")


def _recreate_database(name: str) -> None:
    import psycopg
    from django.conf import settings

    if not DB_NAME_RE.match(name):
        raise ValueError(f"nome de banco inválido: {name!r}")
    cfg = settings.DATABASES["default"]
    params = {"host": cfg["HOST"], "port": cfg["PORT"], "user": cfg["USER"], "dbname": "postgres", "autocommit": True}
    if cfg.get("PASSWORD"):
        params["password"] = cfg["PASSWORD"]
    with psycopg.connect(**params) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')


class Server:
    """Processo uvicorn (ASGI, um processo só, como no plano) contra o banco do teste."""

    def __init__(self, env: dict, log: Path):
        self.env = env
        self.port = _free_port()
        self.base = f"http://localhost:{self.port}"
        self.log = log
        self._file = log.open("a")
        self.proc = None
        self.start()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "config.asgi:application", "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT,
            env=self.env,
            stdout=self._file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_health(self.base, self.proc, self.log)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def close(self) -> None:
        self.stop()
        self._file.close()


@pytest.fixture(scope="session")
def e2e_env(tmp_path_factory):
    """Ambiente do servidor: banco recriado, migrado e com o seed do dia."""
    db_name = os.environ.get("E2E_DB_NAME", "fdr_e2e_browser")
    env = {
        **os.environ,
        "DB_NAME": db_name,
        "DJANGO_DEBUG": "1",
        "LOG_LEVEL": "WARNING",
        "PYTHONUNBUFFERED": "1",
    }
    if os.environ.get("E2E_BASE_URL"):
        return {"env": env, "logs": tmp_path_factory.mktemp("e2e"), "external": True}
    _recreate_database(db_name)
    for command in (["migrate", "--noinput", "-v", "0"], ["seed", "--reset"]):
        result = subprocess.run(
            [sys.executable, "manage.py", *command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300
        )
        assert result.returncode == 0, result.stdout + result.stderr
    return {"env": env, "logs": tmp_path_factory.mktemp("e2e"), "external": False}


@pytest.fixture(scope="session")
def server(e2e_env):
    """O servidor do teste (None com E2E_BASE_URL: servidor externo)."""
    if e2e_env["external"]:
        yield None
        return
    process = Server(e2e_env["env"], e2e_env["logs"] / "uvicorn.log")
    yield process
    process.close()


@pytest.fixture(scope="session")
def base_url(server):
    if server is not None:
        return server.base
    base = os.environ["E2E_BASE_URL"].rstrip("/")
    _wait_health(base, None, None, timeout=5)
    return base


def _launch(playwright):
    candidates = [os.environ.get("E2E_CHROMIUM")]
    candidates.append(None)  # o Chromium que o Playwright instalou
    for root in filter(None, [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "/opt/pw-browsers"]):
        candidates += [str(p) for p in sorted(Path(root).glob("chromium-*/chrome-linux/chrome"), reverse=True)]
    for exe in candidates:
        if exe is not None and not Path(exe).exists():
            continue
        try:
            return playwright.chromium.launch(executable_path=exe, args=CHROMIUM_ARGS)
        except Exception:
            continue
    pytest.skip("Chromium do Playwright indisponível")


@pytest.fixture(scope="module")
def browser():
    # escopo de módulo (não de sessão): o Playwright síncrono mantém um laço asyncio na
    # thread principal, e os testes assíncronos dos outros arquivos não podem herdá-lo
    sync_api = pytest.importorskip("playwright.sync_api")
    sync_api.expect.set_options(timeout=EXPECT_TIMEOUT)
    try:
        with sync_api.sync_playwright() as playwright:
            chromium = _launch(playwright)
            yield chromium
            chromium.close()
    finally:
        sync_api.expect.set_options(timeout=5_000)  # padrão do Playwright para os outros testes


class Console:
    """Junta erros de console e exceções de todas as páginas abertas pelo teste."""

    def __init__(self):
        self.errors: list[str] = []
        self.allowed: list[re.Pattern] = []

    def allow(self, pattern: str) -> None:
        self.allowed.append(re.compile(pattern))

    def watch(self, page, name: str):
        def on_console(msg):
            if msg.type == "error":
                location = msg.location or {}
                self.errors.append(f"[{name}] {msg.text} ({location.get('url', '')})")

        page.on("console", on_console)
        page.on("pageerror", lambda exc: self.errors.append(f"[{name}] exceção: {exc}"))
        return page

    def unexpected(self) -> list[str]:
        return [e for e in self.errors if not any(p.search(e) for p in self.allowed)]


@pytest.fixture
def console():
    watcher = Console()
    yield watcher
    assert watcher.unexpected() == [], "erros no console:\n" + "\n".join(watcher.unexpected())


@pytest.fixture
def new_context(browser, base_url):
    contexts = []

    def make(width: int = 1280, height: int = 900, *, instrument: bool = False, notifications: bool = False, theme: str | None = None):
        ctx = browser.new_context(viewport={"width": width, "height": height}, locale="pt-BR", base_url=base_url)
        if notifications:
            ctx.grant_permissions(["notifications"], origin=base_url)
        if instrument:
            ctx.add_init_script(INSTRUMENT)
        if theme:
            ctx.add_init_script(f"try {{ localStorage.setItem('fdr-theme', '{theme}'); }} catch (e) {{}}")
        contexts.append(ctx)
        return ctx

    yield make
    for ctx in contexts:
        ctx.close()


# --- Dados do seed pela API pública de leitura -----------------------------------------------


def _today_matches(base: str) -> list[dict]:
    return _get_json(f"{base}/api/matches?date={_get_json(f'{base}/api/home')['date']}")["matches"]


def _scheduled_league_match(base: str) -> dict:
    for match in _today_matches(base):
        if match["status"] == "scheduled" and match["group"] is not None:
            return match
    pytest.fail("o seed não deixou jogo agendado de pontos corridos hoje")


def _live_league_match(base: str) -> dict:
    for match in _today_matches(base):
        if match["status"] == "live" and match["period"] in {"first_half", "second_half"} and match["group"] is not None:
            return match
    pytest.fail("o seed não deixou jogo ao vivo de pontos corridos hoje")


def _player_on_field(base: str, match_id: int, side: str) -> str:
    """Um titular do lado que segue em campo (sem aviso de escalação no lançamento)."""
    match = _get_json(f"{base}/api/matches/{match_id}")["match"]
    lineup = match["lineups"][side]
    if not lineup:
        return "Artilheiro E2E"
    out = set()
    for event in match["events"]:
        if event["team_side"] != side:
            continue
        if event["type"] == "substitution":
            out.add(event["payload"].get("player_out"))
        if event["type"] == "red_card":
            out.add(event["player"]["name"])
    on_field = [p["name"] for p in lineup["starters"] if p["name"] not in out]
    return on_field[-1]


class ApiOperator:
    """Operador pela API (sessão própria): para os lances que o teste só precisa observar."""

    def __init__(self, request):
        self.request = request
        me = request.get("/api/auth/me").json()
        response = request.post(
            "/api/auth/login",
            data={"username": OPERATOR[0], "password": OPERATOR[1]},
            headers={"X-CSRFToken": me["csrf_token"]},
        )
        assert response.status == 200, response.text()
        self.csrf = response.json()["csrf_token"]

    def _post(self, path: str, body: dict, status: int):
        response = self.request.post(
            path, data=body, headers={"X-CSRFToken": self.csrf, "Idempotency-Key": str(uuid.uuid4())}
        )
        assert response.status == status, f"{path} {body}: {response.status} {response.text()}"
        return response.json()

    def event(self, match_id: int, body: dict, status: int = 201):
        return self._post(f"/api/ops/matches/{match_id}/events", {"confirm": True, **body}, status)


# --- Tela do operador ---------------------------------------------------------------------------


# Estruturais (andamento do jogo): grupo à parte na tela e confirmação antes do envio
STRUCTURAL = {
    "match_start": "Iniciar a partida?",
    "half_time": "Encerrar o 1º tempo?",
    "second_half_start": "Iniciar o 2º tempo?",
    "extra_time_start": "Iniciar a prorrogação?",
    "penalties_start": "Ir para os pênaltis?",
    "match_end": "Encerrar a partida?",
}


class OperatorPage:
    def __init__(self, page, expect):
        self.page = page
        self.expect = expect
        self.board = page.locator("#op-scoreboard")
        self.form = page.locator("#event-form")

    def login(self):
        page = self.page
        page.goto("/operator.html")
        self.expect(page.locator("#op-login")).to_be_visible()
        page.fill("#login-username", OPERATOR[0])
        page.fill("#login-password", OPERATOR[1])
        page.click("#login-submit")
        self.expect(page.locator("#op-app")).to_be_visible()
        self.expect(page.locator('#op-app [data-hook="user-name"]')).not_to_be_empty()

    def pick(self, match_id: int):
        self.page.click(f'#picker-list li[data-match-id="{match_id}"] .pick-item')
        self.expect(self.page.locator(f'#picker-list li[data-match-id="{match_id}"] .pick-item')).to_have_attribute("aria-current", "true")
        self.expect(self.page.locator("#op-work")).to_be_visible()
        self.expect(self.board.locator(".match")).to_be_visible()

    def open(self, event_type: str):
        # lances de jogo em #action-grid; andamento do jogo (estruturais) em #flow-actions
        grid = "#flow-actions" if event_type in STRUCTURAL else "#action-grid"
        self.page.click(f'{grid} .action-btn[data-type="{event_type}"]')
        self.expect(self.page.locator("#event-form-card")).to_be_visible()
        self.expect(self.page.locator("#event-type")).to_have_value(event_type)

    def minute(self, value: int):
        self.form.locator('input[name="minute"]').fill(str(value))

    def team(self, side: str):
        self.form.locator(f'[data-field="team"] label:has(input[data-hook="input-{side}"])').click()

    def fill(self, name: str, value: str):
        self.form.locator(f'[name="{name}"]').fill(value)

    def select(self, name: str, value: str):
        self.form.locator(f'select[name="{name}"]').select_option(value)

    def submit(self):
        self.form.locator('[data-hook="event-submit"]').click()

    def confirm_structural(self, event_type: str):
        dialog = self.page.locator("#confirm-dialog")
        self.expect(dialog).to_be_visible()
        self.expect(dialog.locator('[data-hook="confirm-title"]')).to_have_text(STRUCTURAL[event_type])
        dialog.locator('[data-hook="confirm-ok"]').click()

    def wait_posted(self):
        self.expect(self.page.locator("#event-form-card")).to_be_hidden()
        self.expect(self.form.locator('[data-hook="event-submit"]')).to_be_enabled()

    def post(self, event_type: str, *, side: str | None = None, minute: int | None = None, **fields):
        self.open(event_type)
        if minute is not None:
            self.minute(minute)
        if side:
            self.team(side)
        for name, value in fields.items():
            self.fill(name.replace("__", "."), value)
        self.submit()
        if event_type in STRUCTURAL:
            self.confirm_structural(event_type)
        self.wait_posted()

    def status(self, action: str, *, kickoff_at: str | None = None, reason: str | None = None):
        page = self.page
        page.click(f'#status-actions [data-action="{action}"]')
        dialog = page.locator("#status-dialog")
        self.expect(dialog).to_be_visible()
        if kickoff_at:
            dialog.locator("#status-kickoff").fill(kickoff_at)
        if reason:
            dialog.locator("#status-reason").fill(reason)
        dialog.locator('[data-hook="status-ok"]').click()
        self.expect(dialog).to_be_hidden()

    def expect_score(self, home: int, away: int):
        self.expect(self.board.locator('[data-hook="score-home"]')).to_have_text(str(home))
        self.expect(self.board.locator('[data-hook="score-away"]')).to_have_text(str(away))

    def expect_status(self, pill: str, period_short: str | None = None):
        self.expect(self.board.locator(".match__status .pill")).to_have_text(re.compile(pill, re.I))
        if period_short:
            self.expect(self.board.locator(".match__minute-period")).to_have_text(period_short)

    def latest_event_id(self) -> int:
        return int(self.page.locator("#op-timeline li[data-event-id]").first.get_attribute("data-event-id"))

    def expect_latest(self, text: str):
        self.expect(self.page.locator("#op-timeline li[data-event-id]").first).to_contain_text(text)


# --- Fase 7: o operador lança um jogo inteiro pela tela ---------------------------------------


def test_fase7_operador_lanca_um_jogo_inteiro_pela_tela(new_context, console, base_url):
    from playwright.sync_api import expect

    match = _scheduled_league_match(base_url)
    # o aviso de confirmação chega como 422 confirmation_required: o navegador loga a resposta
    console.allow(r"status of 422")
    ctx = new_context()
    op = OperatorPage(console.watch(ctx.new_page(), "operador"), expect)
    op.login()
    op.pick(match["id"])
    expect(op.board.locator(".score__vs")).to_be_visible()  # agendado: sem placar
    expect(op.page.locator('#status-actions [data-action="postpone"]')).to_be_enabled()
    expect(op.page.locator('#status-actions [data-action="resume"]')).to_be_disabled()

    # reagendar pelo <dialog> (datetime-local em Brasília, enviado sem fuso): +30 min no mesmo dia
    kickoff = datetime.fromisoformat(match["kickoff_at"].replace("Z", "+00:00")).astimezone(BRASILIA)
    new_kickoff = kickoff + timedelta(minutes=30)
    op.status("reschedule", kickoff_at=new_kickoff.strftime("%Y-%m-%dT%H:%M"), reason="Atraso do ônibus")
    expect(op.board.locator(".match__status .pill")).to_contain_text(new_kickoff.strftime("%H:%M"))
    expect(op.page.locator("#op-timeline li[data-event-id]").first).to_contain_text("Atraso do ônibus")
    rescheduled = _get_json(f"{base_url}/api/matches/{match['id']}")["match"]
    assert datetime.fromisoformat(rescheduled["kickoff_at"].replace("Z", "+00:00")) == new_kickoff

    # agendado: nenhum lance de jogo; o início fica no grupo "Andamento do jogo"
    expect(op.page.locator("#action-grid .action-btn")).to_have_count(0)
    expect(op.page.locator("#flow-actions .action-btn")).to_have_count(1)
    expect(op.page.locator('#flow-actions .action-btn[data-type="match_start"]')).to_be_visible()
    # "Voltar" na confirmação não lança nada
    op.open("match_start")
    op.submit()
    expect(op.page.locator("#confirm-dialog")).to_be_visible()
    op.page.locator('#confirm-dialog button[value="cancel"]').click()
    expect(op.page.locator("#confirm-dialog")).to_be_hidden()
    expect(op.board.locator(".score__vs")).to_be_visible()
    op.form.locator('[data-hook="event-cancel"]').click()

    # início de jogo
    op.post("match_start")
    op.expect_status("Ao vivo", "1T")
    # ao vivo: o gol é o primeiro botão; fim do 1º tempo só no grupo do andamento, depois dos lances
    expect(op.page.locator("#action-grid .action-btn").first).to_have_attribute("data-type", "goal")
    expect(op.page.locator('#action-grid .action-btn[data-type="half_time"]')).to_have_count(0)
    expect(op.page.locator('#flow-actions .action-btn[data-type="half_time"]')).to_be_visible()
    op.expect_score(0, 0)
    expect(op.page.locator("#op-timeline li[data-event-id]")).to_have_count(2)  # reagendado + início

    # gols dos dois lados (o minuto sugerido pelo relógio é trocado pelo do lance)
    op.post("goal", side="home", minute=10, payload__player="Mandante Um")
    op.expect_score(1, 0)
    op.expect_latest("Mandante Um")
    op.post("goal", side="away", minute=20, payload__player="Visitante Um")
    op.expect_score(1, 1)

    # "Cancelar" desiste do lance: nada do rascunho passa para o próximo formulário
    op.open("goal")
    op.team("away")
    op.fill("payload.player", "Rascunho")
    op.form.locator('[data-hook="event-cancel"]').click()
    expect(op.page.locator("#event-form-card")).to_be_hidden()
    op.open("yellow_card")
    expect(op.form.locator('[name="payload.player"]')).to_have_value("")
    expect(op.form.locator('[data-field="team"] input:checked')).to_have_count(0)
    op.form.locator('[data-hook="event-cancel"]').click()

    # cartão
    op.post("yellow_card", side="home", minute=25, payload__player="Zagueiro Duro")
    op.expect_latest("Zagueiro Duro")
    op.expect_score(1, 1)

    # aviso brando (minuto menor que o do lance anterior) → diálogo de confirmação → lança
    op.open("yellow_card")
    op.minute(15)
    op.team("away")
    op.fill("payload.player", "Volante Atrasado")
    op.submit()
    dialog = op.page.locator("#confirm-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator('[data-hook="confirm-list"] li')).not_to_have_count(0)
    expect(op.form.locator('[data-hook="event-warnings"]')).to_be_visible()
    dialog.locator('[data-hook="confirm-ok"]').click()
    op.wait_posted()
    op.expect_latest("Volante Atrasado")

    # gol anulado pelo formulário (escolhendo o gol já lançado): o placar volta
    op.post("goal", side="home", minute=30, payload__player="Mandante Dois")
    op.expect_score(2, 1)
    goal_id = op.latest_event_id()
    op.open("goal_annulled")
    op.minute(31)
    op.select("annuls_event_id", str(goal_id))
    expect(op.form.locator('[data-field="team"]')).to_be_hidden()  # o time vem do gol
    op.fill("payload.reason", "Impedimento na origem")
    op.submit()
    op.wait_posted()
    op.expect_score(1, 1)
    expect(op.page.locator(f'#op-timeline li[data-event-id="{goal_id}"]')).to_have_class(re.compile(r"op-event--annulled"))

    # lance errado → "Cancelar lançamento": some da linha do tempo e do placar
    op.post("goal", side="away", minute=35, payload__player="Lance Errado")
    op.expect_score(1, 2)
    wrong_id = op.latest_event_id()
    op.page.locator(f'#op-timeline li[data-event-id="{wrong_id}"] [data-hook="void"]').click()
    void = op.page.locator("#void-dialog")
    expect(void).to_be_visible()
    void.locator("#void-reason").fill("Lançado no jogo errado")
    void.locator('[data-hook="void-ok"]').click()
    expect(void).to_be_hidden()
    op.expect_score(1, 1)
    expect(op.page.locator(f'#op-timeline li[data-event-id="{wrong_id}"]')).to_have_count(0)

    # suspender e retomar (status pelo <dialog>, só os que `available` libera)
    op.status("suspend", reason="Chuva forte")
    op.expect_status("Suspenso")
    expect(op.page.locator('#action-grid .action-btn[data-type="goal"]')).to_have_count(0)
    expect(op.page.locator('#status-actions [data-action="suspend"]')).to_be_disabled()
    op.status("resume")
    op.expect_status("Ao vivo", "1T")
    op.expect_score(1, 1)

    # intervalo e 2º tempo
    op.post("half_time")
    op.expect_status("Intervalo")
    op.expect_score(1, 1)
    expect(op.page.locator('#action-grid .action-btn[data-type="goal"]')).to_have_count(0)
    op.post("second_half_start")
    op.expect_status("Ao vivo", "2T")
    op.post("goal", side="home", minute=60, payload__player="Mandante Um")
    op.expect_score(2, 1)

    # fim de jogo (pede confirmação, como todo o andamento do jogo)
    op.open("match_end")
    op.submit()
    op.confirm_structural("match_end")
    op.wait_posted()
    op.expect_status("Encerrado")
    op.expect_score(2, 1)
    expect(op.page.locator(f'#picker-list li[data-match-id="{match["id"]}"] .pill')).to_have_text(re.compile("Encerrado", re.I))

    # o banco fechou o mesmo placar (leitura pública, sem a tela)
    final = _get_json(f"{base_url}/api/matches/{match['id']}")["match"]
    assert (final["status"], final["home_score"], final["away_score"], final["winner"]) == ("finished", 2, 1, "home")
    goals = [g["player"] for g in final["goals"]]
    assert goals == ["Mandante Um", "Visitante Um", "Mandante Um"]
    assert final["cards"]["home"]["yellow"] == 1 and final["cards"]["away"]["yellow"] == 1


# --- Fase 8: duas abas acompanham o jogo ao vivo, com alertas ---------------------------------


def _card(page, match_id: int):
    return page.locator(f'article.match[data-match-id="{match_id}"]')


def _goals_for(page, team_id: int):
    return page.locator(f'.standings tr[data-team-id="{team_id}"] td.col-gp').first


def test_fase8_home_e_competicao_acompanham_gol_e_anulacao_sem_recarregar(new_context, console, base_url):
    from playwright.sync_api import expect

    match = _live_league_match(base_url)
    home_team, home_score, away_score = match["home"], match["home_score"], match["away_score"]
    scorer = _player_on_field(base_url, match["id"], "home")
    ctx = new_context(instrument=True, notifications=True)

    # 1) home e competição abertas ANTES do lance
    home = console.watch(ctx.new_page(), "home")
    home.goto("/")
    expect(_card(home, match["id"]).locator('[data-hook="score-home"]')).to_have_text(str(home_score))
    comp = console.watch(ctx.new_page(), "competição")
    comp.goto(f"/competition.html?slug={match['competition']['slug']}")
    expect(_card(comp, match["id"]).locator('[data-hook="score-home"]')).to_have_text(str(home_score))
    for page in (home, comp):
        page.evaluate("window.__sameDocument = true")  # some se a página recarregar
    gf_before = int(_goals_for(home, home_team["id"]).inner_text())
    assert int(_goals_for(comp, home_team["id"]).inner_text()) == gf_before
    goals_before = home.locator("#latest-goals-list > li").count()

    # 2) som e notificações ligados na home (o clique toca a amostra e libera o áudio)
    home.bring_to_front()
    home.click("#toggle-sound")
    expect(home.locator("#toggle-sound")).to_have_attribute("aria-pressed", "true")
    played_after_sample = home.evaluate("window.__played")
    assert played_after_sample >= 1 and home.evaluate("window.__playedOk") >= 1
    expect(home.locator("#toggle-notifications")).to_be_visible()
    home.click("#toggle-notifications")
    expect(home.locator("#toggle-notifications")).to_have_attribute("aria-pressed", "true")
    expect(home.locator("#notifications-hint")).to_be_hidden()  # localhost é contexto seguro

    # 3) o operador (terceira aba) lança um gol do mandante
    op = OperatorPage(console.watch(ctx.new_page(), "operador"), expect)
    op.login()
    op.pick(match["id"])
    op.post("goal", side="home", payload__player=scorer)
    op.expect_score(home_score + 1, away_score)
    goal_id = op.latest_event_id()

    # 4) sem recarregar: placar, aviso "É gol!", últimos gols e classificação nas duas abas
    for page in (home, comp):
        card = _card(page, match["id"])
        expect(card.locator('[data-hook="score-home"]')).to_have_text(str(home_score + 1))
        expect(card.locator('[data-hook="score-away"]')).to_have_text(str(away_score))
        expect(_goals_for(page, home_team["id"])).to_have_text(str(gf_before + 1))
    alert = home.locator("#goal-alert .goal-alert").first
    expect(home.locator("#goal-alert")).to_have_attribute("aria-live", "polite")
    expect(alert).to_contain_text("É gol!")
    expect(alert).to_contain_text(scorer)
    first_goal = home.locator("#latest-goals-list > li").first
    expect(first_goal).to_have_attribute("data-event-id", str(goal_id))
    expect(first_goal).to_contain_text(scorer)
    expect(home.locator("#latest-goals-list > li")).to_have_count(min(goals_before + 1, 10))
    home.wait_for_function("window.__playedOk > %d" % played_after_sample)  # tocou, sem gesto novo
    home.wait_for_function("window.__notifications.some((n) => n.title.startsWith('É gol!'))")
    goal_notification = home.evaluate("window.__notifications.find((n) => n.title.startsWith('É gol!'))")
    assert scorer in goal_notification["body"]
    # a página da competição não alerta
    assert comp.evaluate("window.__notifications.length") == 0
    assert comp.locator("#goal-alert").count() == 0

    # 5) o operador anula o gol: aviso de correção, gol fora da lista, placar e tabela de volta
    op.open("goal_annulled")
    op.select("annuls_event_id", str(goal_id))
    op.fill("payload.reason", "Falta no goleiro")
    op.submit()
    op.wait_posted()
    op.expect_score(home_score, away_score)
    correction = home.locator("#goal-alert .goal-alert--correction").first
    expect(correction).to_contain_text("Oxe! Gol anulado.")
    expect(correction).to_contain_text(scorer)
    # a correção toma o lugar do "É gol!" daquele gol (como a notificação do sistema)
    expect(home.locator(f'#goal-alert .goal-alert[data-event-id="{goal_id}"]')).to_have_count(1)
    expect(home.locator(f'#latest-goals-list > li[data-event-id="{goal_id}"]')).to_have_count(0)
    for page in (home, comp):
        expect(_card(page, match["id"]).locator('[data-hook="score-home"]')).to_have_text(str(home_score))
        expect(_goals_for(page, home_team["id"])).to_have_text(str(gf_before))
    home.wait_for_function("window.__notifications.some((n) => n.title === 'Oxe! Gol anulado.')")
    assert home.evaluate("window.__notifications.find((n) => n.title.startsWith('É gol!')).closed") is True
    assert all(page.evaluate("window.__sameDocument === true") for page in (home, comp)), "a página recarregou"
    # a correção não toca som
    played_after_goal = home.evaluate("window.__played")
    home.wait_for_timeout(500)
    assert home.evaluate("window.__played") == played_after_goal


# --- Mata-mata e navegação de rodadas -----------------------------------------------------------


def test_competicao_mata_mata_e_navegacao_de_rodadas(new_context, console):
    from playwright.sync_api import expect

    ctx = new_context()
    page = console.watch(ctx.new_page(), "copa")
    page.goto("/competition.html?slug=copa-pernambuco")
    select = page.locator("#stage-select")
    expect(select).to_be_enabled()
    options = select.locator("option")
    labels = [options.nth(i).inner_text() for i in range(options.count())]
    knockout_value = options.filter(has_text="Mata-mata").get_attribute("value")
    groups_value = options.filter(has_text="Grupos").first.get_attribute("value") if any("Grupos" in t for t in labels) else options.first.get_attribute("value")

    # fase de grupos: uma linha por grupo, com a tabela do grupo ao lado dos jogos dele
    select.select_option(groups_value)
    expect(page).to_have_url(re.compile(rf"stage={groups_value}"))
    expect(page.locator("#round-matches .group-row .group-row__table .standings__group")).to_have_count(2)
    expect(page.locator("#stage-standings")).to_be_hidden()
    expect(page.locator("#stage-ties")).to_be_hidden()

    # mata-mata: um bloco por confronto, sem classificação nem card de agregado à parte
    select.select_option(knockout_value)
    expect(page).to_have_url(re.compile(rf"stage={knockout_value}"))
    expect(page.locator("#round-matches .tie-group")).not_to_have_count(0)
    expect(page.locator("#stage-ties")).to_be_hidden()
    expect(page.locator("#stage-standings .standings")).to_have_count(0)
    expect(page.locator("#round-label")).to_have_text(re.compile("Final", re.I))
    expect(page.locator("#round-next")).to_be_disabled()

    # rodada anterior: semifinais de ida e volta; o card da volta traz o agregado, quem avança
    # e a forma da decisão
    page.click("#round-prev")
    expect(page.locator("#round-label")).to_have_text(re.compile("Semifinal", re.I))
    ties = page.locator("#round-matches .tie-group")
    expect(ties).to_have_count(2)
    for i in range(2):
        legs = ties.nth(i).locator("article.match")
        expect(legs).to_have_count(2)
        expect(legs.nth(0).locator(".match__tie")).to_have_text(re.compile("Jogo de ida"))
        expect(legs.nth(0).locator(".match__tie-agg")).to_have_count(0)
        expect(legs.nth(1).locator(".match__tie")).to_contain_text("Agregado")
        expect(legs.nth(1).locator(".match__tie-agg")).to_have_count(1)
        expect(legs.nth(1).locator(".tie__adv")).to_contain_text("avança")
    decided = " ".join(page.locator("#round-matches .tie-group .tie__adv").all_inner_texts()).lower()
    assert "prorrogação" in decided and "agregado" in decided
    expect(page.locator("#round-matches article.match")).not_to_have_count(0)

    # Pernambucano: ‹ › trocam a rodada sem recarregar
    page.goto("/competition.html?slug=pernambucano-raiz")
    label = page.locator("#round-label")
    expect(label).to_have_text(re.compile(r"Rodada 5"))
    page.evaluate("window.__sameDocument = true")
    page.click("#round-prev")
    expect(label).to_have_text(re.compile(r"Rodada 4"))
    expect(page).to_have_url(re.compile(r"round=\d+"))
    expect(page.locator("#round-matches article.match")).to_have_count(10)
    expect(page.locator("#round-matches .pill--live")).to_have_count(0)
    page.click("#round-next")
    page.click("#round-next")
    expect(label).to_have_text(re.compile(r"Rodada 6"))
    expect(page.locator("#round-matches .pill--scheduled")).to_have_count(10)
    assert page.evaluate("window.__sameDocument") is True  # não recarregou


def test_mata_mata_ao_vivo_agregado_e_penaltis_na_pagina_da_competicao(new_context, console, base_url):
    """A final (jogo único, sem prorrogação) vai aos pênaltis com a página aberta: o card do
    jogo muda pelo stream, sem recarregar, e no fim diz quem avança e como."""
    from playwright.sync_api import expect

    final = next(m for m in _today_matches(base_url) if m["tie"] is not None)
    if final["status"] != "scheduled":
        pytest.skip("a final do seed já foi jogada neste banco")
    home_id, away_id = final["home"]["id"], final["away"]["id"]
    ctx = new_context()
    page = console.watch(ctx.new_page(), "copa-ao-vivo")
    page.goto("/competition.html?slug=copa-pernambuco")
    expect(page.locator("#round-label")).to_have_text(re.compile("Final", re.I))
    card = _card(page, final["id"])
    expect(page.locator(f'#round-matches .tie-group[data-tie-id="{final["tie"]["id"]}"] article.match')).to_have_count(1)
    expect(card.locator(".match__tie")).to_be_hidden()  # jogo único: nada até alguém avançar
    page.evaluate("window.__sameDocument = true")

    api = ApiOperator(new_context().request)
    mid = final["id"]
    api.event(mid, {"type": "match_start"})
    api.event(mid, {"type": "goal", "minute": 10, "team_id": home_id, "payload": {"player": "Camisa Nove"}})
    api.event(mid, {"type": "goal", "minute": 40, "team_id": away_id, "payload": {"player": "Camisa Dez"}})
    expect(card.locator('[data-hook="score-home"]')).to_have_text("1")
    expect(card.locator('[data-hook="score-away"]')).to_have_text("1")
    api.event(mid, {"type": "half_time"})
    api.event(mid, {"type": "second_half_start"})
    # empate no jogo decisivo: o fim de jogo é recusado (vai aos pênaltis, sem prorrogação)
    refused = api.event(mid, {"type": "match_end"}, status=422)
    assert refused["code"]
    api.event(mid, {"type": "penalties_start"})
    expect(card.locator(".match__status .pill")).to_have_text(re.compile("Pênaltis", re.I))
    for team_id, scored in ((home_id, True), (away_id, False), (home_id, True), (away_id, False), (home_id, True)):
        api.event(mid, {"type": "shootout_kick", "team_id": team_id, "payload": {"player": "Cobrador", "scored": scored}})
    api.event(mid, {"type": "match_end"})
    expect(card.locator(".match__status .pill")).to_have_text(re.compile("Encerrado", re.I))
    expect(card.locator(".score__pen")).to_have_text("(3) × (0) pên.")
    expect(card.locator(".match__tie")).to_contain_text(final["home"]["name"])
    expect(card.locator(".match__tie .tie__adv")).to_contain_text("avança nos pênaltis")
    assert page.evaluate("window.__sameDocument") is True


# --- Tema, console, largura de celular e marca ----------------------------------------------------


PAGES = (
    ("home", "/"),
    ("liga", "/competition.html?slug=pernambucano-raiz"),
    ("copa", "/competition.html?slug=copa-pernambuco"),
    ("operador", "/operator.html"),
)


def test_tema_claro_por_padrao_e_escuro_persiste(new_context, console):
    from playwright.sync_api import expect

    ctx = new_context()
    page = console.watch(ctx.new_page(), "tema")
    page.goto("/")
    html = page.locator("html")
    expect(html).to_have_attribute("data-theme", "light")
    toggle = page.locator("[data-theme-toggle]")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()
    expect(html).to_have_attribute("data-theme", "dark")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(html).to_have_attribute("data-theme", "dark")
    expect(page.locator('meta[name="theme-color"]')).to_have_attribute("content", "#0C1322")
    # vale nas outras páginas também
    page.goto("/operator.html")
    expect(html).to_have_attribute("data-theme", "dark")
    page.locator("[data-theme-toggle]").click()
    page.reload()
    expect(html).to_have_attribute("data-theme", "light")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_paginas_sem_erro_no_console_nem_rolagem_horizontal_a_390px(new_context, console, theme):
    from playwright.sync_api import expect

    ctx = new_context(390, 844, theme=theme)
    for name, path in PAGES:
        page = console.watch(ctx.new_page(), f"{name}-{theme}")
        page.goto(path)
        if name == "operador":
            expect(page.locator("#op-login")).to_be_visible()
        else:
            expect(page.locator("article.match").first).to_be_visible()
            expect(page.locator("#competitions-nav")).not_to_have_attribute("aria-busy", "true")
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        page.wait_for_timeout(300)
        widths = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        assert widths[0] <= widths[1], f"{name}: rolagem horizontal ({widths[0]} > {widths[1]})"
        page.close()


def test_logo_vem_da_configuracao_da_marca(new_context, console, e2e_env):
    from playwright.sync_api import expect

    if e2e_env["external"]:
        pytest.skip("servidor externo: não dá para trocar a configuração da marca")
    env = {**e2e_env["env"], "BRAND_LOGO_URL": "img/logo-mark.svg", "BRAND_NAME": "Raízes de Teste"}
    env.pop("BRAND_LOGO_DARK_URL", None)
    env.pop("BRAND_LOGO_ALT", None)  # sem ele, o alt do logo é o BRAND_NAME
    brand_server = Server(env, e2e_env["logs"] / "uvicorn-brand.log")
    try:
        ctx = new_context()
        page = console.watch(ctx.new_page(), "marca")
        page.goto(f"{brand_server.base}/")
        expect(page.locator("img.brand__logo--light")).to_have_attribute("src", "/static/img/logo-mark.svg")
        expect(page.locator("img.brand__logo--dark")).to_have_attribute("src", "/static/img/logo-mark.svg")
        expect(page).to_have_title(re.compile("Raízes de Teste"))
        assert page.evaluate("document.querySelector('img.brand__logo--light').naturalWidth") > 0
        expect(page.locator("img.brand__logo--light")).to_have_attribute("alt", "Raízes de Teste")
        expect(page.locator("article.match").first).to_be_visible()
        # o admin também segue o BRAND_NAME (cabeçalho e título)
        page.goto(f"{brand_server.base}/admin/login/")
        expect(page.locator(".fdr-brand__title")).to_have_text("Raízes de Teste · Administração")
        expect(page).to_have_title(re.compile("Raízes de Teste"))
    finally:
        brand_server.close()


# --- Nome do time, layout shift e tema do admin ------------------------------------------------

NAMES_JS = """() => [...document.querySelectorAll('.match .team__name')].map((name) => {
  const shown = [...name.children].find((c) => getComputedStyle(c).display !== 'none');
  const lh = parseFloat(getComputedStyle(name).lineHeight);
  return { kind: shown.classList.contains('team__short') ? 'short' : 'full', text: shown.textContent,
    height: shown.getBoundingClientRect().height, lh, overflow: name.scrollWidth - name.clientWidth };
})"""


@pytest.mark.parametrize("width", [320, 360, 375, 1024])
@pytest.mark.parametrize("path", ["/", "/competition.html?slug=pernambucano-raiz"])
def test_nome_do_time_nao_quebra_no_meio_da_palavra(new_context, console, path, width):
    """Sigla em uma linha (nada de "SP/T"; o ✓ do vencedor fica fora do fluxo); nome completo
    em no máximo duas linhas equilibradas; nada vaza da célula do time."""
    from playwright.sync_api import expect

    ctx = new_context(width, 900)
    page = console.watch(ctx.new_page(), f"nomes-{width}")
    page.goto(path)
    expect(page.locator("article.match").first).to_be_visible()
    page.wait_for_timeout(200)
    names = page.evaluate(NAMES_JS)
    assert len(names) >= 20
    bad = [n for n in names if n["height"] > (1.5 if n["kind"] == "short" else 2.5) * n["lh"] or n["overflow"] > 1]
    assert not bad, bad
    if width <= 375:
        assert {n["kind"] for n in names} == {"short"}
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


CLS_OBSERVER = """
window.__cls = 0; window.__shifts = [];
new PerformanceObserver((list) => {
  for (const entry of list.getEntries()) {
    if (entry.hadRecentInput) continue;
    window.__cls += entry.value;
    window.__shifts.push([entry.value, entry.sources.map((s) => (s.node && (s.node.id || s.node.className)) || '?')]);
  }
}).observe({ type: 'layout-shift', buffered: true });
"""


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
@pytest.mark.parametrize("path", ["/", "/competition.html?slug=pernambucano-raiz"])
def test_carregamento_sem_layout_shift(new_context, console, path, viewport):
    """CLS < 0,1 ("bom") do esqueleto aos dados: rodapé fora da 1ª tela, menu com a mesma
    altura, últimos gols e data reservados, fontes da 1ª tela pré-carregadas."""
    from playwright.sync_api import expect

    ctx = new_context(*viewport)
    ctx.add_init_script(CLS_OBSERVER)
    page = console.watch(ctx.new_page(), f"cls-{viewport[0]}")
    page.goto(path)
    expect(page.locator("article.match").first).to_be_visible()
    page.wait_for_timeout(1500)
    cls = page.evaluate("window.__cls")
    assert cls < 0.1, page.evaluate("window.__shifts")


def test_admin_claro_por_padrao_e_com_o_mesmo_tema_do_site(browser, base_url, console):
    """Sistema em modo escuro e nada guardado: o admin abre claro (como o site). O tema
    escolhido no site vale no admin, e o botão do admin alterna só claro/escuro, de volta ao site."""
    from playwright.sync_api import expect

    ctx = browser.new_context(viewport={"width": 1024, "height": 700}, color_scheme="dark", base_url=base_url, locale="pt-BR")
    try:
        page = console.watch(ctx.new_page(), "admin-tema")
        html = page.locator("html")
        page.goto("/admin/login/")
        expect(html).to_have_attribute("data-theme", "light")
        page.goto("/")
        page.locator("[data-theme-toggle]").click()
        expect(html).to_have_attribute("data-theme", "dark")
        page.goto("/admin/login/")
        expect(html).to_have_attribute("data-theme", "dark")
        toggle = page.locator(".theme-toggle")
        toggle.click()
        expect(html).to_have_attribute("data-theme", "light")  # sem o passo "auto" do Django
        toggle.click()
        expect(html).to_have_attribute("data-theme", "dark")
        toggle.click()
        expect(html).to_have_attribute("data-theme", "light")
        page.goto("/")
        expect(html).to_have_attribute("data-theme", "light")  # a escolha volta para o site
    finally:
        ctx.close()


# --- Django Admin: navegação por competição e punição em pontos ---------------------------------------


def test_admin_navega_pela_competicao_e_punicao_aparece_ao_vivo(new_context, console):
    """O admin desce Competição › Temporada › Fase pela trilha; uma punição de 3 pontos
    salva na fase chega à página da competição aberta (pelo stream), com a marca nos pontos
    e o motivo embaixo da legenda; apagar a punição tira as duas coisas."""
    from playwright.sync_api import expect

    site = console.watch(new_context().new_page(), "competicao-punicao")
    site.goto("/competition.html?slug=pernambucano-raiz")
    expect(site.locator("#stage-standings .standings__group")).to_have_count(1)
    expect(site.locator("#stage-standings .adjustments")).to_have_count(0)

    admin = console.watch(new_context(1280, 900).new_page(), "admin-punicao")
    admin.goto("/admin/login/")
    admin.fill("#id_username", "admin")
    admin.fill("#id_password", os.environ.get("SEED_ADMIN_PASSWORD") or "raizes-admin-2026")
    admin.locator('input[type="submit"]').click()
    expect(admin.locator("#content")).to_contain_text("Comece por")
    expect(admin.locator("#content a", has_text="Partidas")).to_have_count(0)  # sem a lista global
    admin.locator('#content a[href="/admin/competitions/competition/"]').first.click()
    admin.get_by_role("link", name="Pernambucano Raiz").click()
    admin.locator(".field-stages_panel").get_by_role("link", name="Fase única").click()
    crumbs = admin.locator(".breadcrumbs")
    expect(crumbs).to_contain_text("Início")
    expect(crumbs).to_contain_text("Pernambucano Raiz")
    expect(crumbs).to_contain_text("Fase única")
    expect(admin.locator("body")).to_contain_text("a ao vivo inclui")

    group = admin.locator("#point_adjustments-group")
    group.locator(".add-row a").click()
    group.locator('select[name="point_adjustments-0-team"]').select_option(label="Santa Cruz")
    group.locator('input[name="point_adjustments-0-points"]').fill("-3")
    group.locator('input[name="point_adjustments-0-reason"]').fill("escalação irregular")
    admin.locator('input[name="_continue"]').click()
    expect(admin.locator(".messagelist")).to_be_visible()

    note = site.locator("#stage-standings .adjustments")
    expect(note).to_contain_text("Santa Cruz: \u22123 pts — escalação irregular", timeout=20_000)
    santa = site.locator("#stage-standings tr", has=site.locator(".team-cell__name", has_text="Santa Cruz"))
    expect(santa.locator(".col-pts .adj-mark")).to_have_attribute("title", "Punição: perdeu 3 pontos fora de campo")

    admin.locator('input[name="point_adjustments-0-DELETE"]').check()
    admin.locator('input[name="_continue"]').click()
    expect(note).to_have_count(0, timeout=20_000)
    expect(santa.locator(".adj-mark")).to_have_count(0)


# --- Tempo real: o servidor cai e volta ------------------------------------------------------------


def _live_minute(match: dict) -> dict:
    """Minuto corrente do jogo (mesma conta de clock.js), para lançar pela API."""
    clock = match["clock"]
    started = datetime.fromisoformat(match["period_started_at"].replace("Z", "+00:00"))
    elapsed = int((datetime.now(started.tzinfo) - started).total_seconds() // 60)
    minute = clock["offset"] + elapsed + 1
    if minute > clock["regular_end"]:
        return {"minute": clock["regular_end"], "stoppage": min(minute - clock["regular_end"], 30)}
    return {"minute": minute}


def test_stream_volta_sozinho_depois_que_o_servidor_reinicia(server, new_context, console, base_url):
    """Derrubar a conexão não perde mensagem: o servidor reinicia, a home mostra
    "Reconectando ao vivo…", volta sozinha (Last-Event-ID) e recebe o gol sem recarregar."""
    from playwright.sync_api import expect

    if server is None:
        pytest.skip("servidor externo: o teste não pode reiniciá-lo")
    # o navegador loga as tentativas de reconexão enquanto o servidor está fora
    console.allow(r"ERR_CONNECTION_REFUSED|ERR_EMPTY_RESPONSE|ERR_INCOMPLETE_CHUNKED_ENCODING|ERR_CONNECTION_RESET|/api/stream")
    match = _live_league_match(base_url)
    ctx = new_context()
    home = console.watch(ctx.new_page(), "home")
    home.goto("/")
    card = _card(home, match["id"])
    expect(card.locator('[data-hook="score-away"]')).to_have_text(str(match["away_score"]))
    home.evaluate("window.__sameDocument = true")

    server.stop()
    expect(home.locator("#live-status")).to_be_visible(timeout=20_000)
    expect(home.locator("#live-status")).to_contain_text("Reconectando ao vivo")
    server.start()

    api = ApiOperator(new_context().request)
    scorer = _player_on_field(base_url, match["id"], "away")
    current = _get_json(f"{base_url}/api/matches/{match['id']}")["match"]
    posted = api.event(match["id"], {"type": "goal", "team_id": match["away"]["id"], "payload": {"player": scorer}, **_live_minute(current)})
    expect(card.locator('[data-hook="score-away"]')).to_have_text(str(match["away_score"] + 1), timeout=40_000)
    expect(home.locator("#live-status")).to_be_hidden()
    expect(home.locator("#latest-goals-list > li").first).to_have_attribute("data-event-id", str(posted["event"]["id"]))

    # e segue ao vivo depois da volta: a anulação também chega
    current = _get_json(f"{base_url}/api/matches/{match['id']}")["match"]
    api.event(match["id"], {"type": "goal_annulled", "annuls_event_id": posted["event"]["id"], "payload": {"reason": "Impedimento"}, **_live_minute(current)})
    expect(card.locator('[data-hook="score-away"]')).to_have_text(str(match["away_score"]))
    assert home.evaluate("window.__sameDocument") is True
