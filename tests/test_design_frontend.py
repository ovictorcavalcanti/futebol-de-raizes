"""Design system e cascas das páginas: marca, ganchos documentados em docs/FRONTEND.md,
ativos estáticos e o guia de estilo só com DEBUG. Sem banco."""

import os
import re
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from django.conf import settings

from matches import domain

ROOT = Path(settings.BASE_DIR)
STATIC = ROOT / "static"
SPRITE = ROOT / "templates" / "partials" / "icons.svg"


def _html(client, url):
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


def test_styleguide_so_com_debug(client, settings):
    settings.DEBUG = True
    html = _html(client, "/styleguide.html")
    assert "Guia de estilo" in html and "styleguide.js" in html
    settings.DEBUG = False
    assert client.get("/styleguide.html").status_code == 404


def test_base_tema_marca_e_fontes(client):
    html = _html(client, "/")
    assert 'data-theme="light"' in html
    assert 'localStorage.getItem("fdr-theme")' in html  # tema antes da 1ª pintura
    assert 'name="theme-color"' in html and 'data-dark="#0C1322"' in html
    # fontes pré-carregadas: texto, placar e a face dos títulos (h1 = LCP da home)
    preloads = re.findall(r'<link rel="preload" href="/static/fonts/([\w-]+\.woff2)" as="font" type="font/woff2" crossorigin>', html)
    assert html.count('rel="preload"') == len(preloads) == 3
    assert set(preloads) == {
        "barlow-latin-400-normal.woff2",
        "barlow-condensed-latin-700-normal.woff2",
        "alfa-slab-one-latin-400-normal.woff2",
    }
    assert "/static/img/logo.svg" in html and "/static/img/logo-dark.svg" in html
    assert 'alt="Futebol de Raízes"' in html
    assert "data-theme-toggle" in html and 'aria-pressed="false"' in html
    assert 'id="i-ball"' in html  # sprite embutido
    assert "Do Recife ao Sertão, futebol de raízes." in html
    assert "/operator.html" not in html  # a área do operador não aparece para o público
    assert "GoalNow" not in html


def test_logo_configuravel(client, settings):
    settings.BRAND = {
        **settings.BRAND,
        "logo_url": "https://cdn.example.com/marca.svg",
        "logo_dark_url": "",
        "logo_alt": "Minha marca",
    }
    html = _html(client, "/")
    assert (
        html.count('src="https://cdn.example.com/marca.svg"') == 2
    )  # sem versão escura, repete
    assert 'alt="Minha marca"' in html


def test_previsao_do_botao_de_notificacoes_segue_alerts_js(client):
    """O script inline da home (antes da 1ª pintura, sem layout shift) mostra o botão de
    notificações pela mesma regra de celular de alerts.js#isMobileDevice."""
    html = _html(client, "/")
    inline = re.search(r"<script>\(function\(\)\{var w=window.*?</script>", html, re.S).group(0)
    alerts = (STATIC / "js" / "alerts.js").read_text()
    mobile_re = re.search(r"if \((/Android[^/]*/i)\.test\(ua\)\) return true;", alerts).group(1)
    assert mobile_re in inline
    assert "userAgentData" in inline and "maxTouchPoints" in inline and "isSecureContext" in inline
    # roda depois do botão e do aviso (os dois já existem quando o script executa)
    assert html.index('id="toggle-notifications"') < html.index(inline)
    assert html.index('id="notifications-hint"') < html.index(inline)


def test_home_regioes_e_templates(client):
    html = _html(client, "/")
    for hook in (
        'id="competitions-nav"',
        'data-hook="competition-links"',
        'id="brasilia-clock"',
        'id="latest-goals"',
        'id="goal-alert"',
        'aria-live="polite"',
        'id="latest-goals-list"',
        'id="toggle-sound"',
        'id="toggle-notifications"',
        'id="competitions"',
        'id="tpl-competition-section"',
        'id="tpl-stage-block"',
        'id="home-empty"',
        'id="goal-sound"',
        "sounds/gol.wav",
        "js/home.js",
        'id="toasts"',
        'id="live-status"',
    ):
        assert hook in html, hook
    assert "Hoje não tem jogo, visse?" in html
    assert "Nenhum gol hoje ainda. Paciência, que ele vem." in html
    # o relógio fica fora da região aria-live
    assert "aria-live" not in re.search(
        r'<div class="clock">.*?</div>', html, re.S
    ).group(0)


def test_competicao_e_operador_ganchos(client):
    html = _html(client, "/competition.html?slug=pernambucano")
    for hook in (
        'id="stage-select"',
        'id="round-prev"',
        'id="round-next"',
        'id="round-label"',
        'id="round-matches"',
        'id="stage-standings"',
        'id="stage-ties"',
        "js/competition.js",
    ):
        assert hook in html, hook
    html = _html(client, "/operator.html")
    for hook in (
        'id="login-form"',
        'id="op-app"',
        'id="picker-list"',
        'id="action-grid"',
        'id="status-actions"',
        'id="event-form"',
        'id="op-timeline"',
        'id="confirm-dialog"',
        'id="status-dialog"',
        'id="void-dialog"',
        'id="tpl-field-team"',
        'id="tpl-field-player"',
        'id="tpl-op-event"',
        "js/operator.js",
    ):
        assert hook in html, hook
    assert 'id="competitions-nav"' not in html


def test_sprite_tem_todos_os_icones_do_dominio_e_do_front():
    sprite = SPRITE.read_text()
    symbols = set(re.findall(r'<symbol id="i-([a-z-]+)"', sprite))
    domain_icons = {spec.icon for spec in domain.CATALOG.values()} | set(domain.STATUS_ACTION_ICONS.values()) | {
        "ball-penalty",
        "ball-own",
        "card-second-yellow",
        "x-circle",
    }
    assert domain_icons <= symbols, domain_icons - symbols
    front_icons = set(
        re.findall(
            r"'([a-z-]+)'",
            (STATIC / "js" / "icons.js")
            .read_text()
            .split("ICONS = Object.freeze([")[1]
            .split("]")[0],
        )
    )
    assert front_icons <= symbols, front_icons - symbols
    used = set(
        re.findall(
            r'href="#i-([a-z-]+)"',
            "".join(p.read_text() for p in (ROOT / "templates").rglob("*.html")),
        )
    )
    assert used <= symbols, used - symbols


def test_icones_dos_eventos_no_front_espelham_o_catalogo():
    """icons.js EVENT_ICONS (reserva quando o evento vem sem `icon`) = ícone do CATALOG:
    o mesmo lance com o mesmo desenho no operador e no site."""
    block = (STATIC / "js" / "icons.js").read_text().split("EVENT_ICONS = Object.freeze({")[1].split("})")[0]
    front = dict(re.findall(r"(\w+): '([a-z-]+)'", block))
    assert front, block
    for event_type, name in front.items():
        assert domain.CATALOG[event_type].icon == name, event_type


def test_ativos_estaticos():
    css = (STATIC / "css" / "app.css").read_text()
    for font in re.findall(r'url\("\.\./fonts/([^"]+)"\)', css):
        assert (STATIC / "fonts" / font).is_file(), font
    assert "@layer tokens, base, layout, components, pages, utilities;" in css
    assert css.count("font-display: swap") >= 16
    for name in (
        "logo.svg",
        "logo-dark.svg",
        "logo-mark.svg",
        "favicon.svg",
        "azulejo.svg",
        "empty-sertao.svg",
    ):
        assert (STATIC / "img" / name).is_file(), name
    logo = (STATIC / "img" / "logo.svg").read_text()
    assert "<text" not in logo  # wordmark em curvas
    with wave.open(str(STATIC / "sounds" / "gol.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (22050, 1, 2)
        assert 1.0 <= w.getnframes() / w.getframerate() <= 1.5


def test_js_sem_innerhtml():
    """Contrato §6: texto só por textContent/<template>, nunca innerHTML."""
    pattern = re.compile(
        r"\.(?:inner|outer)HTML\s*\+?=|insertAdjacentHTML|document\.write"
    )
    for path in (STATIC / "js").glob("*.js"):
        assert not pattern.search(path.read_text()), path.name


@pytest.mark.skipif(shutil.which("node") is None, reason="node não instalado")
def test_modulos_puros_do_front_em_outro_fuso():
    result = subprocess.run(
        [
            shutil.which("node"),
            "--test",
            *sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs")),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TZ": "Asia/Tokyo"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- Contraste AA calculado a partir dos tokens do CSS -------------------------------


def _tokens(css: str, selector: str) -> dict[str, str]:
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\n  \}", css, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))


def _resolve(tokens: dict[str, str], name: str) -> str:
    value = tokens[name].strip()
    if value.startswith("#"):
        return value
    ref = re.fullmatch(r"var\(--([\w-]+)\)", value)
    if ref:
        return _resolve(tokens, ref.group(1))
    mix = re.fullmatch(
        r"color-mix\(in srgb, var\(--([\w-]+)\) (\d+)%, var\(--([\w-]+)\)\)", value
    )
    assert mix, f"--{name}: {value}"
    a, b = _rgb(_resolve(tokens, mix.group(1))), _rgb(_resolve(tokens, mix.group(3)))
    p = int(mix.group(2)) / 100
    return "#" + "".join(f"{round(x * p + y * (1 - p)):02x}" for x, y in zip(a, b))


def _rgb(hex_color: str) -> tuple[int, ...]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def _contrast(a: str, b: str) -> float:
    def lum(c):
        ch = [v / 255 for v in _rgb(c)]
        ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [  # (texto, fundo) — 4.5:1
    *[
        (ink, bg)
        for ink in ("ink", "ink-2", "ink-3")
        for bg in ("paper", "surface", "surface-2")
    ],
    ("ink-3", "azul-tint"),
    ("ink-3", "amarelo-tint"),
    ("ink-3", "vermelho-tint"),
    ("vermelho-ink", "surface"),
    ("vermelho-ink", "vermelho-tint"),
    ("verde-ink", "surface"),
    ("verde-ink", "verde-tint"),
    ("amarelo-ink", "surface"),
    ("amarelo-ink", "surface-2"),
    ("azul", "surface"),
    ("azul", "azul-tint"),
    ("on-azul", "azul"),
    ("on-vermelho", "vermelho"),
    ("on-amarelo", "amarelo"),
    ("on-ink", "ink"),
]
GRAPHIC_PAIRS = [  # (elemento de interface, fundo) — 3:1 (WCAG 1.4.11)
    ("field", "surface"),
    ("field", "paper"),
    ("field", "surface-2"),
    ("vermelho", "surface"),
    ("verde", "surface"),
    ("azul", "paper"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_contraste_aa_dos_tokens(theme):
    css = (STATIC / "css" / "app.css").read_text()
    tokens = _tokens(css, ":root")
    if theme == "dark":
        tokens = {**tokens, **_tokens(css, ':root[data-theme="dark"]')}
    failures = [
        f"--{fg} sobre --{bg}: {ratio:.2f}"
        for pairs, minimum in ((TEXT_PAIRS, 4.5), (GRAPHIC_PAIRS, 3.0))
        for fg, bg in pairs
        if (ratio := _contrast(_resolve(tokens, fg), _resolve(tokens, bg))) < minimum
    ]
    assert not failures, failures
    # campos de formulário e interruptor usam o token de 3:1, não a borda decorativa
    assert re.search(
        r"\.input, \.select select, \.textarea \{[^}]*border: 1\.5px solid var\(--field\)",
        css,
    )
    assert "background: var(--field); cursor: pointer;" in css  # trilho do interruptor


def test_botao_de_tema_tem_nome_fixo(client):
    """Alternância com aria-pressed: o nome não muda com o estado (senão o leitor anuncia o contrário)."""
    html = _html(client, "/")
    button = re.search(r"<button[^>]*data-theme-toggle[^>]*>", html).group(0)
    assert 'aria-label="Tema escuro"' in button and 'aria-pressed="false"' in button
    assert (
        "aria-label"
        not in (STATIC / "js" / "theme.js")
        .read_text()
        .split("function paintToggles")[1]
        .split("}\n")[0]
    )


@pytest.mark.parametrize("url", ["/", "/competition.html?slug=x", "/operator.html"])
def test_modulepreload_sem_cascata(client, url):
    """Todo import estático de um módulo pré-carregado também é pré-carregado (sem ida e volta extra)."""
    html = _html(client, url)
    preloaded = set(
        re.findall(r'rel="modulepreload" href="/static/js/([\w-]+\.js)"', html)
    )
    assert "match-card.js" in preloaded
    for name in preloaded:
        source = (STATIC / "js" / name).read_text()
        imports = set(
            re.findall(r"^import [^;]*? from '\./([\w-]+\.js)';", source, re.M)
        )
        assert imports <= preloaded, (name, imports - preloaded)


# --- Componentes no navegador (Playwright, se houver Chromium) -----------------------


# sem tráfego de fundo do Chromium (atualização de componentes, login, métricas)
CHROMIUM_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-pings",
    "--no-first-run",
]


def _chromium(playwright):
    try:
        return playwright.chromium.launch(args=CHROMIUM_ARGS)
    except Exception:
        roots = [
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
            "/opt/pw-browsers",
            str(Path.home() / ".cache" / "ms-playwright"),
        ]
        for root in filter(None, roots):
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


HARNESS = """<!doctype html><html lang="pt-BR" data-theme="light"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/app.css"></head><body>{sprite}
<button type="button" class="btn" data-theme-toggle aria-pressed="false" aria-label="Tema escuro">tema</button>
<div id="root"></div></body></html>"""
CONTENT_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".wav": "audio/wav",
}


@pytest.fixture(scope="module")
def browser_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    sprite = SPRITE.read_text()
    with sync_api.sync_playwright() as playwright:
        browser = _chromium(playwright)
        page = browser.new_page()

        def serve(route):
            path = route.request.url.split("fdr.test", 1)[1].split("?")[0]
            if path == "/":
                return route.fulfill(
                    body=HARNESS.format(sprite=sprite), content_type="text/html"
                )
            file = STATIC / path.removeprefix("/static/")
            if path.startswith("/static/") and file.is_file():
                return route.fulfill(
                    body=file.read_bytes(),
                    content_type=CONTENT_TYPES.get(
                        file.suffix, "application/octet-stream"
                    ),
                )
            return route.abort()

        page.route("http://fdr.test/**", serve)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://fdr.test/")
        yield page, errors
        browser.close()


COMPONENTS_JS = """async () => {
  const M = await import('/static/js/match-card.js');
  const S = await import('/static/js/standings.js');
  const F = await import('/static/js/fixtures.js');
  await import('/static/js/theme.js');
  const root = document.getElementById('root');
  const out = {};

  // 1) resposta atrasada (versão menor) não desfaz o placar mais novo
  const live = F.MATCHES.live;
  const card = M.createMatchCard(live, {});
  root.append(card);
  M.updateMatchCard(card, { ...live, version: live.version - 1, home_score: 0, away_score: 0 });
  out.staleIgnored = card.querySelector('[data-hook="score-home"]').textContent === String(live.home_score);

  // 2) versão nova resumida com o acordeão aberto: o detalhe é pedido de novo
  let calls = 0;
  const summary = F.summaryOf(live);
  const lazy = M.createMatchCard(summary, { onExpand: () => { calls += 1; return new Promise(() => {}); } });
  root.append(lazy);
  const details = lazy.querySelector('details');
  details.open = true;
  await new Promise((r) => setTimeout(r, 30));
  const callsAfterOpen = calls;
  M.updateMatchCard(lazy, live);                       // detalhe chegou
  M.updateMatchCard(lazy, { ...summary, version: live.version + 1 }); // stream/recarga resumida mais nova
  out.reRequested = callsAfterOpen === 1 && calls === 2;

  // 2b) detalhe atrasado (versão menor que o resumo novo): pede de novo uma vez; de novo velho → "Tentar de novo"
  let staleCalls = 0;
  const fresh = { ...summary, id: 555, version: live.version + 10 };
  const staleCard = M.createMatchCard(fresh, { onExpand: () => { staleCalls += 1; return new Promise(() => {}); } });
  root.append(staleCard);
  staleCard.querySelector('details').open = true;
  await new Promise((r) => setTimeout(r, 30));
  M.updateMatchCard(staleCard, { ...live, id: 555 });
  const afterFirstStale = staleCalls;
  M.updateMatchCard(staleCard, { ...live, id: 555 });
  out.staleRetry = afterFirstStale === 2 && staleCalls === 2 && !!staleCard.querySelector('.match__body [role="alert"] button');

  // 3) agregado na ordem do placar (mandante = team_b do confronto)
  const ko = F.MATCHES.knockoutPenalties;
  const koCard = M.createMatchCard(ko, {});
  root.append(koCard);
  out.homeIsTeamB = ko.home.id === ko.tie.team_b.id;
  out.aggregate = koCard.querySelector('.match__tie-agg').textContent;
  out.expectedAggregate = `${ko.home.short_name} ${ko.tie.aggregate.team_b} × ${ko.tie.aggregate.team_a} ${ko.away.short_name}`;
  // a ida não mostra agregado; o TieOut que chega na mensagem da ida atualiza o card da volta
  const legOne = { ...ko, id: 556, version: 1, home: ko.away, away: ko.home, tie: { ...ko.tie, leg: 1, complete: false, winner_team_id: null, aggregate: { team_a: 1, team_b: 0 } } };
  const legTwo = { ...ko, id: 557, version: 1, status: 'scheduled', home_score: null, away_score: null, home_penalties: null, away_penalties: null, tie: { ...legOne.tie, leg: 2 } };
  const legCards = [M.createMatchCard(legOne, {}), M.createMatchCard(legTwo, {})];
  root.append(...legCards);
  out.legOneTie = legCards[0].querySelector('.match__tie').textContent;
  out.legOneAgg = legCards[0].querySelectorAll('.match__tie-agg').length;
  // ícone de informação na frente de "Jogo de ida" e de "Agregado" (sem troféu, sem dica)
  const lead = (el) => el.querySelector('.match__tie-row').firstElementChild?.querySelector('use')?.getAttribute('href');
  out.legOneInfo = [lead(legCards[0]), lead(koCard), legCards[0].querySelectorAll('.match__tie use[href="#i-trophy"], .match__tie [title]').length];
  M.syncTieCards(legCards, { ...legOne, version: 2, tie: { ...legOne.tie, aggregate: { team_a: 2, team_b: 0 } } });
  out.legTwoAgg = legCards[1].querySelector('.match__tie-agg').textContent;
  out.legTwoLeg = M.getCardMatch(legCards[1]).tie.leg;
  // rodada "Final": quem vence é campeão, com a estrela no fim (sem o símbolo do "avança")
  const finalCard = M.createMatchCard({ ...ko, id: 558, tie: { ...ko.tie, round: { ...(ko.tie.round || {}), name: 'Final' } } }, {});
  root.append(finalCard);
  const adv = finalCard.querySelector('.match__tie .tie__adv');
  out.finalAdv = [adv.textContent, adv.classList.contains('tie__adv--champion'), adv.lastElementChild?.querySelector('use')?.getAttribute('href'), !!adv.querySelector('use[href="#i-advance"]')];
  // outras rodadas: "avança" com a seta dupla para cima no fim (sem o check antes)
  const semiAdv = koCard.querySelector('.match__tie .tie__adv');
  // atraso: linha nas observações abaixo do placar (como a do jogo de ida), com o alerta na
  // frente; nem no placar nem na faixa do horário; some quando o jogo começa
  const late = { ...F.MATCHES.scheduledToday, id: 559, status: 'delayed', status_label: 'Atrasado', status_note: 'Chuva forte' };
  const lateCard = M.createMatchCard(late, {});
  root.append(lateCard);
  const notes = lateCard.querySelector('.match__tie');
  out.delay = [notes.hidden, notes.textContent, notes.querySelector('.match__tie-row')?.firstElementChild?.querySelector('use')?.getAttribute('href'), lateCard.querySelector('.match__meta').textContent.includes('Chuva'), lateCard.querySelector('.match__board').textContent.includes('Chuva')];
  // a data da faixa diz o que é: "Previsto" (atrasado), "Era" (a original do adiado e do
  // cancelado), "Início" (suspenso) ou "Retomada" (suspenso com data nova)
  const whenShown = () => lateCard.querySelector('.match__when')?.textContent;
  out.delayWhen = whenShown();
  const noteRows = () => [...notes.querySelectorAll('.match__tie-row')].map((r) => [r.textContent, r.firstElementChild?.querySelector('use')?.getAttribute('href')]);
  M.updateMatchCard(lateCard, { ...late, status: 'postponed', status_label: 'Adiado', status_note: 'Falta de energia' });
  out.postponed = noteRows();
  out.postponedWhen = [whenShown(), lateCard.querySelector('.match__when-tag')?.textContent, lateCard.querySelector('.match__board').getAttribute('aria-label')];
  // suspenso: o motivo e, com data nova (parou num dia, volta no outro), a retomada com o
  // calendário; cancelado: o motivo
  const stopped = { ...late, status: 'suspended', status_label: 'Suspenso', period: 'second_half', period_label: '2º tempo', home_score: 0, away_score: 1, status_note: 'Chuva forte' };
  M.updateMatchCard(lateCard, stopped);
  out.suspended = noteRows();
  out.suspendedWhen = whenShown();
  // reagendado depois da suspensão: o kickoff_at já é o da volta (igual a resumes_at)
  const tomorrow = F.MATCHES.scheduledTomorrow.kickoff_at;
  M.updateMatchCard(lateCard, { ...stopped, kickoff_at: tomorrow, resumes_at: tomorrow });
  out.suspendedMoved = noteRows();
  out.suspendedMovedWhen = whenShown();
  M.updateMatchCard(lateCard, { ...stopped, status: 'cancelled', status_label: 'Cancelado', status_note: 'Sem condições de jogo', resumes_at: null });
  out.cancelled = noteRows();
  M.updateMatchCard(lateCard, { ...late, status: 'cancelled', status_label: 'Cancelado', status_note: 'Sem condições de jogo', home_score: null, away_score: null });
  out.cancelledWhen = whenShown();
  // W.O.: encerrado com o placar do W.O. e o aviso "W.O. em favor do {time} — motivo"
  M.updateMatchCard(lateCard, { ...stopped, status: 'finished', status_label: 'Encerrado', status_note: null, home_score: 0, away_score: 3, walkover: { team_id: late.away.id, reason: 'Central não compareceu' } });
  out.walkover = [noteRows(), `W.O. em favor do ${late.away.name} — Central não compareceu`];
  M.updateMatchCard(lateCard, { ...late, status: 'live', period: 'first_half', status_note: null, home_score: 0, away_score: 0 });
  out.delayGone = notes.hidden && !notes.textContent;
  out.semiAdv = [semiAdv.textContent, semiAdv.firstChild?.nodeType === 3, semiAdv.lastElementChild?.querySelector('use')?.getAttribute('href'), !!semiAdv.querySelector('use[href="#i-check"]')];

  // 4) lance sem time (VAR) mostra o minuto junto do texto
  const timeline = M.renderTimeline(live);
  const varItem = [...timeline.querySelectorAll('.tl-item--neutral')].find((li) => li.textContent.includes('VAR'));
  out.varMinute = varItem?.querySelector('.tl-item__min-inline')?.textContent || '';
  // 2º amarelo: um item só (o vermelho automático), não amarelo + amarelo + vermelho
  const cardItems = (name) => [...timeline.querySelectorAll('.tl-item')].filter((li) => li.querySelector('.tl-item__title')?.textContent === name);
  // cada cartão: [nome acessível do ícone, legenda]; a legenda só aparece quando acrescenta algo
  const describe = (li) => [li.querySelector('.tl-item__icon')?.getAttribute('aria-label') || '', li.querySelector('.tl-item__sub')?.textContent || ''];
  out.secondYellowItems = cardItems('Thiago Freitas').map(describe);
  out.plainYellowItems = cardItems('Zé Roberto').map(describe).filter(([label]) => label.startsWith('Cartão'));
  // gol de jogada sem assistência: sem a legenda "Gol" (o ícone já diz); com assistência, ela fica
  const goalSubs = [...timeline.querySelectorAll('.tl-item--goal')].map((li) => [...li.querySelectorAll('.tl-item__sub')].map((x) => x.textContent));
  out.goalSubs = goalSubs;
  // assistência: na tela "Assist.: Fulano"; "Assistência:" só para o leitor de tela; separador do placar = ·
  const assisted = [...timeline.querySelectorAll('.tl-item--goal')].find((li) => li.textContent.includes('Biel Ventura'));
  out.assistHidden = assisted?.querySelector('.tl-item__sub .visually-hidden')?.textContent || '';
  out.scoreSep = assisted?.querySelector('.tl-item__min-sep')?.textContent || '';
  // o time de cada lance de um lado, oculto na tela (a coluna diz só para quem vê); lance neutro, sem time
  const sided = [...timeline.querySelectorAll('.tl-item--home, .tl-item--away')];
  out.teamPrefix = [sided.length > 0, sided.filter((li) => li.querySelector('.tl-item__text > .visually-hidden')?.textContent !== `${live[li.classList.contains('tl-item--home') ? 'home' : 'away'].name}: `).length,
    [...timeline.querySelectorAll('.tl-item--neutral .tl-item__text > .visually-hidden')].length];
  out.yellowSpoken = [cardItems('Zé Roberto').find((li) => describe(li)[0] === 'Cartão amarelo')?.querySelector('.tl-item__text').textContent, `${live.home.name}: Zé Roberto`];
  // reagendado: a data nova e a de antes, sem "Hoje"/"Amanhã" maiúsculo no meio da frase
  const moved = { id: 99902, sequence: 9998, type: 'rescheduled', type_label: 'Reagendado', team_side: null,
    payload: { kickoff_at: '2030-01-05T20:00:00Z', previous_kickoff_at: '2030-01-02T22:00:00Z', reason: 'Chuva' } };
  out.rescheduled = M.renderTimeline({ ...live, events: [...live.events, moved] }).querySelector('[data-event-id="99902"] .tl-item__title')?.textContent;

  // 5) transmissões: javascript: não vira link
  const facts = M.createMatchCard({ ...live, version: 999, broadcasts: [
    { name: 'Malicioso', url: 'javascript:alert(1)', kind: 'streaming', kind_label: 'Streaming' },
    { name: 'TV Boa', url: 'https://tv.example.com/', kind: 'open_tv', kind_label: 'TV aberta' },
  ] }, {});
  root.append(facts);
  facts.querySelector('details').open = true;
  await new Promise((r) => setTimeout(r, 30));
  facts.querySelector('[data-tab="facts"]').click();
  const links = [...facts.querySelectorAll('.fact__links a')].map((a) => a.getAttribute('href'));
  out.links = links;
  out.maliciousAsText = [...facts.querySelectorAll('.fact__links li')].some((li) => li.textContent.startsWith('Malicioso') && !li.querySelector('a'));
  // arbitragem: cada nome numa peça que quebra inteira; estatísticas e lances com os dois times no topo
  const factsPanel = facts.querySelector('.tabpanel[data-tab="facts"]');
  out.officials = [...factsPanel.querySelectorAll('.fact--officials dd')].map((dd) => [dd.textContent, dd.querySelectorAll('.fact__person').length]);
  const sideNames = (el) => (el ? [...el.querySelectorAll('.sides__name')].map((n) => n.textContent).concat(el.getAttribute('aria-hidden'), el.querySelectorAll('.crest').length) : null);
  out.statsTeams = [sideNames(factsPanel.querySelector('.stats > .stats__teams')), factsPanel.querySelector('.stats__teams')?.previousElementSibling?.className];
  out.timelineTeams = [sideNames(facts.querySelector('.tabpanel[data-tab="events"] > .tl-sides')), facts.querySelector('.tl-sides')?.nextElementSibling?.className];
  out.teamNames = [live.home.name, live.away.name];

  // 6) aviso de correção: placar do gol que caiu riscado
  const alert = M.createGoalAlert(F.LATEST_GOALS[0], { kind: 'correction', reason: 'annulled' });
  out.correctionStruck = !!alert.querySelector('s.goal-alert__void-score');
  out.correctionTitle = alert.querySelector('.goal-alert__title').textContent;
  out.goalNotStruck = !M.createGoalAlert(F.LATEST_GOALS[0]).querySelector('s');

  // 7) classificação: atualizar sem opts mantém as da criação
  const table = S.createStandings(F.STANDINGS, { compact: true, legend: false });
  S.updateStandings(table, F.STANDINGS);
  out.standingsKeepsOpts = table.classList.contains('standings--compact') && !table.querySelector('.legend');
  // empatados em todos os critérios: a posição exibida (rank) se repete; a nota explica o "="
  out.tiedRanks = [...table.querySelectorAll('tbody tr')].map((tr) => tr.querySelector('.col-pos').firstChild.textContent);
  out.tiedNote = table.querySelector('.standings__note')?.textContent;

  // 8) brilho do gol: animationend de um filho não encerra o destaque do card
  const goal = { ...live, version: live.version + 5, home_score: live.home_score + 1 };
  M.updateMatchCard(card, goal, { flash: true });
  const pill = card.querySelector('.pill');
  pill.dispatchEvent(new AnimationEvent('animationend', { bubbles: true, animationName: 'pulse' }));
  out.goalRingKept = card.classList.contains('is-goal');
  card.dispatchEvent(new AnimationEvent('animationend', { bubbles: true, animationName: 'goal-ring' }));
  out.goalRingCleared = !card.classList.contains('is-goal');

  // 9) cor de time fora de #RRGGBB não vai para o CSS
  const evil = { ...live.home, color_primary: 'url(https://evil.example/x)' };
  const evilCard = M.createMatchCard({ ...live, id: 777, home: evil }, {});
  out.colorSanitized = !evilCard.querySelector('.team--home').getAttribute('style').includes('url(');

  // 10) botão de tema: estado em aria-pressed, nome fixo
  const toggle = document.querySelector('[data-theme-toggle]');
  toggle.click();
  out.themeDark = document.documentElement.dataset.theme === 'dark' && toggle.getAttribute('aria-pressed') === 'true';
  out.themeLabel = toggle.getAttribute('aria-label');
  toggle.click();

  // 11) gol anulado na linha do tempo: registro factual, sem o "Oxe!" (só o aviso ao vivo tem sotaque)
  const annulled = live.events.find((e) => e.type === 'goal_annulled');
  const loose = { ...annulled, id: 99901, sequence: 9999, annuls_event_id: 424242, payload: { reason: 'Mão na bola' } };
  const annulTimeline = M.renderTimeline({ ...live, events: [...live.events, loose] });
  out.annulStruck = annulTimeline.querySelector('.tl-item--annulled')?.textContent || '';
  out.annulTimeline = annulTimeline.textContent;
  return out;
}"""


def test_componentes_no_navegador(browser_page):
    page, errors = browser_page
    out = page.evaluate(COMPONENTS_JS)
    assert not errors, errors
    assert out["staleIgnored"], "versão menor sobrescreveu o card"
    assert out["reRequested"], "detalhe não foi pedido de novo"
    assert out["staleRetry"], (
        "detalhe atrasado: sem novo pedido ou sem 'Tentar de novo'"
    )
    assert out["homeIsTeamB"] and out["aggregate"] == out["expectedAggregate"]
    assert out["legOneTie"] == "Jogo de ida" and out["legOneAgg"] == 0
    assert out["legOneInfo"] == ["#i-info", "#i-info", 0]
    assert out["legTwoAgg"].split()[1:4] == ["0", "×", "2"] and out["legTwoLeg"] == 2  # volta: mandante = team_b
    assert out["finalAdv"] == ["Campeão nos pênaltis", True, "#i-star", False]
    assert out["semiAdv"] == ["avança nos pênaltis", True, "#i-advance", False]
    assert out["delay"] == [False, "Início atrasado — Chuva forte", "#i-alert", False, False] and out["delayGone"]
    # adiado: a data da faixa é a original ("Era ..."), e a observação diz que a nova não saiu
    assert out["postponed"] == [["Jogo adiado — Falta de energia", "#i-alert"], ["Nova data a definir", "#i-calendar"]]
    when, tag, label = out["postponedWhen"]
    assert re.fullmatch(r"Era hoje · \d\d:\d\d", when) and tag == "Era "
    assert re.search(r" contra .+, marcado para hoje às \d\d:\d\d\. Adiado: Falta de energia$", label), label
    assert re.fullmatch(r"Previsto hoje · \d\d:\d\d", out["delayWhen"])
    assert out["suspended"] == [["Jogo suspenso — Chuva forte", "#i-alert"]]
    assert re.fullmatch(r"Início hoje · \d\d:\d\d", out["suspendedWhen"])
    assert out["suspendedMoved"][0] == ["Jogo suspenso — Chuva forte", "#i-alert"]
    assert out["suspendedMoved"][1][0].startswith("Retomada: amanhã às ") and out["suspendedMoved"][1][1] == "#i-calendar"
    assert re.fullmatch(r"Retomada amanhã · \d\d:\d\d", out["suspendedMovedWhen"])
    assert out["cancelled"] == [["Jogo cancelado — Sem condições de jogo", "#i-alert"]]
    assert re.fullmatch(r"Era hoje · \d\d:\d\d", out["cancelledWhen"])
    assert out["walkover"][0] == [[out["walkover"][1], "#i-alert"]]
    assert out["varMinute"] == "31'"
    # 1º amarelo + o vermelho do 2º; o tipo vai no nome acessível do ícone, sem legenda repetida
    assert out["secondYellowItems"] == [["Cartão amarelo", ""], ["Cartão vermelho", "Segundo amarelo e vermelho"]]
    assert out["plainYellowItems"] == [["Cartão amarelo", ""]]
    assert ["Gol"] not in out["goalSubs"] and ["Assistência: Assist.: Biel Ventura"] in out["goalSubs"] and ["De pênalti"] in out["goalSubs"]
    assert out["assistHidden"] == "Assistência: " and out["scoreSep"] == "·"
    # cada lance de um lado diz o time ao leitor de tela; os neutros (VAR sem time, acréscimos) não
    assert out["teamPrefix"] == [True, 0, 0]
    assert out["yellowSpoken"][0] == out["yellowSpoken"][1]
    assert out["rescheduled"] == "Reagendado para sáb, 05/01 · 17:00 (era qua, 02/01 · 19:00) — Chuva"
    assert out["links"] == ["https://tv.example.com/"] and out["maliciousAsText"]
    assert ["Clóvis Amaral (PE), Bruna Alves (PE)", 2] in out["officials"] and ["Anderson Bezerra (PE)", 1] in out["officials"]
    home_name, away_name = out["teamNames"]
    assert out["statsTeams"] == [[home_name, away_name, "true", 2], "stats__title"]  # logo abaixo do título
    assert out["timelineTeams"] == [[home_name, away_name, "true", 2], "timeline"]  # logo acima da linha do tempo
    assert (
        out["correctionStruck"]
        and out["correctionTitle"] == "Oxe! Gol anulado."
        and out["goalNotStruck"]
    )
    assert out["standingsKeepsOpts"]
    assert out["tiedRanks"][3:7] == ["4", "5", "5", "7"]
    assert out["tiedNote"] == "= Empatados em todos os critérios de desempate"
    assert out["goalRingKept"] and out["goalRingCleared"]
    assert out["colorSanitized"]
    assert out["themeDark"] and out["themeLabel"] == "Tema escuro"
    # gol anulado riscado com minuto e motivo; anulação avulsa com o motivo; nada de "Oxe!"
    assert "Gol anulado aos 36' — Impedimento marcado pelo VAR" in out["annulStruck"]
    assert "Mão na bola" in out["annulTimeline"]
    assert "Oxe" not in out["annulTimeline"]


SHARE_JS = """async () => {
  const S = await import('/static/js/share.js');
  const M = await import('/static/js/match-card.js');
  const F = await import('/static/js/fixtures.js');
  const fin = F.MATCHES.finished;
  const out = {};
  out.names = [fin.home.name, fin.away.name];
  out.url = S.matchUrl(fin, 'https://x.test');
  out.text = S.matchShareText(fin, Date.now(), out.url).split('\\n');
  out.awayScorers = S.scorers(fin, 'away');
  out.results = S.resultsShareText('PE · Rodada 5', [fin, F.MATCHES.scheduledToday], Date.now(), 'https://x.test/r').split('\\n');
  out.later = [F.MATCHES.scheduledToday.home.name, F.MATCHES.scheduledToday.away.name];
  const opened = [];
  S.shareResultsOnWhatsApp('PE · Rodada 5', [fin], { url: 'https://x.test/r', win: { open: (...args) => opened.push(args) } });
  out.opened = opened.map(([href, target]) => [decodeURIComponent(href.split('?text=')[1]), href.split('?')[0], target]);
  // imagem: PNG 1200 × 800 (canvas limpo: o toBlob funciona); autores dos gols todos na mesma fonte
  const drawn = [];
  const fillText = CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText = function (text, ...rest) { drawn.push([String(text), this.font]); return fillText.call(this, text, ...rest); };
  let blob;
  try { blob = await S.renderMatchImage(fin, { brand: 'Futebol de Raízes', host: 'x.test' }); }
  finally { CanvasRenderingContext2D.prototype.fillText = fillText; }
  const bitmap = await createImageBitmap(blob);
  out.image = [blob.type, bitmap.width, bitmap.height, blob.size > 5000];
  const fontsOf = (texts) => drawn.filter(([t]) => texts.includes(t));
  const goalTexts = [...S.scorers(fin, 'home'), ...S.scorers(fin, 'away')];
  out.scorerFonts = [fontsOf(goalTexts).length, new Set(fontsOf(goalTexts).map(([, f]) => f)).size];
  out.nameFonts = new Set(fontsOf([fin.home.name.toUpperCase(), fin.away.name.toUpperCase()]).map(([, f]) => f)).size;
  // card: botão no canto; sem acordeão (placar do operador), sem botão
  const root = document.getElementById('root');
  const card = M.createMatchCard(fin, {});
  root.append(card);
  out.buttons = [!!card.querySelector(':scope > .match__share'), !!M.createMatchCard(fin, { details: false }).querySelector('.match__share')];
  const ready = (dialog) => new Promise((resolve) => {
    const start = Date.now();
    const timer = setInterval(() => {
      const done = !dialog.querySelector('.share-sheet__img').hidden;
      if (done || Date.now() - start > 8000) { clearInterval(timer); resolve(done); }
    }, 50);
  });
  const foot = (dialog) => [...dialog.querySelectorAll('.dialog__foot > *')].map((b) => [b.tagName, b.textContent.trim()]);
  // sem Web Share de arquivos (computador): o botão é o link do WhatsApp com a mensagem e copia a imagem
  card.querySelector('.match__share').click();
  let dialog = document.querySelector('dialog.share-sheet');
  out.cardDialog = [dialog?.open, await ready(dialog), foot(dialog)];
  dialog.close();
  await new Promise((resolve) => setTimeout(resolve, 150)); // o evento close chega numa tarefa depois
  out.removed = !document.querySelector('dialog.share-sheet');
  const copied = [];
  // ClipboardItem só existe em contexto seguro (o site é HTTPS; esta página de teste, não)
  if (typeof ClipboardItem !== 'function') window.ClipboardItem = class { constructor(data) { this.types = Object.keys(data); } };
  dialog = S.openMatchShare(fin, { nav: { clipboard: { write: async (items) => { copied.push(...items); } } } });
  await ready(dialog);
  const link = dialog.querySelector('[data-hook=share-whatsapp]');
  link.addEventListener('click', (event) => event.preventDefault()); // sem abrir a aba no teste
  link.click();
  await new Promise((resolve) => setTimeout(resolve, 100));
  out.desktop = [link.tagName, link.href.split('?')[0], link.target, decodeURIComponent(link.href.split('?text=')[1]) === S.matchShareText(fin, Date.now(), S.matchUrl(fin)),
    copied.length === 1 && copied[0].types.includes('image/png'), dialog.textContent.includes('Baixar')];
  dialog.close();
  // com Web Share de arquivos (celular): a imagem vai como arquivo, com a mensagem (e o link) na legenda
  const shared = [];
  const nav = { share: async (data) => { shared.push(data); }, canShare: () => true };
  dialog = S.openMatchShare(fin, { nav });
  await ready(dialog);
  out.mobileFoot = foot(dialog);
  dialog.querySelector('[data-hook=share-whatsapp]').click();
  await new Promise((resolve) => setTimeout(resolve, 50));
  const [sent] = shared;
  out.mobile = [shared.length, sent?.files?.[0]?.type, sent?.files?.[0]?.name, sent?.text === S.matchShareText(fin, Date.now(), S.matchUrl(fin)), 'url' in (sent || {})];
  dialog.close();
  return out;
}"""


def test_compartilhar_jogo_no_whatsapp_texto_imagem_e_janela(browser_page):
    page, errors = browser_page
    out = page.evaluate(SHARE_JS)
    assert not errors, errors
    home, away = out["names"]
    assert out["url"] == "https://x.test/?jogo=15"
    assert out["text"][0] == f"*{home} 1 × 2 {away}*" and out["text"][1].endswith("Encerrado")
    assert out["awayScorers"][0].startswith("Netinho do Pajeú 9") and out["awayScorers"][1].endswith("(contra)")
    assert out["text"][2:] == [
        "", f"⚽ *{home}:* Mauro Shampoo Jr. 40'", f"⚽ *{away}:* " + ", ".join(out["awayScorers"]), "", "🔗 https://x.test/?jogo=15",
    ]
    assert out["results"][:3] == ["*PE · Rodada 5*", "", out["text"][0].strip("*")]
    assert out["results"][3].startswith(f"{out['later'][0]} × {out['later'][1]} — ")  # ainda não começou: o horário
    assert out["results"][-2:] == ["", "🔗 https://x.test/r"]
    assert out["opened"] == [["*PE · Rodada 5*\n\n" + out["text"][0].strip("*") + "\n\n🔗 https://x.test/r", "https://api.whatsapp.com/send", "_blank"]]
    assert out["image"] == ["image/png", 1200, 800, True]  # 3:2
    assert out["scorerFonts"] == [3, 1] and out["nameFonts"] == 1  # mesmo tamanho para todos os autores (e os dois nomes)
    assert out["buttons"] == [True, False]
    assert out["cardDialog"] == [True, True, [["BUTTON", "Fechar"], ["A", "Compartilhar no WhatsApp"]]] and out["removed"]
    assert out["desktop"] == ["A", "https://api.whatsapp.com/send", "_blank", True, True, False]  # sem "baixar"
    assert out["mobileFoot"] == [["BUTTON", "Fechar"], ["BUTTON", "Compartilhar no WhatsApp"]]
    assert out["mobile"] == [1, "image/png", "jogo-15.png", True, False]


META_JS = """async () => {
  const M = await import('/static/js/match-card.js');
  const F = await import('/static/js/fixtures.js');
  const S = await import('/static/js/share.js');
  const St = await import('/static/js/standings.js');
  const root = document.getElementById('root');
  const read = (match) => {
    const card = M.createMatchCard(match, {});
    root.append(card);
    const meta = card.querySelector('.match__meta');
    return [meta.querySelector('.pill').textContent.trim(), meta.querySelector('.match__lead time')?.textContent ?? null,
      [...meta.querySelectorAll('.match__place-item')].map((item) => [item.querySelector('use').getAttribute('href'), item.lastChild.textContent])];
  };
  const later = F.MATCHES.scheduledToday;
  const standings = { stage_name: '1ª fase', kind: 'live', groups: [{ name: 'Grupo A', rows: [
    { rank: 1, position: 1, team: { name: 'Sport' }, points: 13, played: 5, goals_for: 11, goal_difference: 8, playing: true },
    { rank: 2, position: 2, team: { name: 'Náutico' }, points: 1, played: 5, goals_for: 3, goal_difference: -3 },
  ] }] };
  // jogo sem local nem cidade: só o "quando" (sem "null" na faixa)
  const noPlace = M.createMatchCard({ ...later, id: 9301, venue: '', city: null }, {});
  root.append(noPlace);
  // imagem: "JOGANDO AGORA" (etiqueta vermelha) ao lado de quem está em jogo; fase · rodada no subtítulo
  const drawn = [];
  const fillText = CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText = function (text, ...rest) { drawn.push(String(text)); return fillText.call(this, text, ...rest); };
  let blob;
  try { blob = await S.renderStandingsImage(F.STANDINGS, F.STANDINGS.groups[0], { competition: 'PE Raiz', stage: '1ª fase', round: 'Rodada 5', brand: 'Futebol de Raízes', host: 'x.test' }); }
  finally { CanvasRenderingContext2D.prototype.fillText = fillText; }
  const bitmap = await createImageBitmap(blob);
  // depois da imagem (a janela também desenha a dela, com a espiã do canvas ligada)
  // classificação geral (um "grupo" com o nome dela): não vira "Grupo ..."
  const ranking = St.createStandings({ stage_id: null, stage_name: 'Classificação geral', kind: 'official',
    groups: [{ id: null, name: 'Classificação geral', rows: F.STANDINGS.groups[0].rows }] }, { share: { title: 'PE Raiz' } });
  root.append(ranking);
  ranking.querySelector('.standings__share').click();
  const dialog = document.querySelector('dialog.share-sheet[open]'); // a aberta agora (outra, fechada, pode ainda não ter saído)
  const rankingTitle = dialog.querySelector('.share-sheet__title').textContent;
  dialog.close();
  return {
    standingsImage: [blob.type, bitmap.width, bitmap.height > 236 + F.STANDINGS.groups[0].rows.length * 54 + 110],
    playingBadges: [drawn.filter((t) => t === 'JOGANDO AGORA').length, F.STANDINGS.groups[0].rows.filter((r) => r.playing).length],
    subtitle: drawn.includes('1ª FASE · RODADA 5'), noPartial: !drawn.some((t) => /parcial|em jogo agora/i.test(t)),
    scheduled: read(later), finished: read(F.MATCHES.finished),
    names: [later.venue, later.city, F.MATCHES.finished.venue, F.MATCHES.finished.city],
    standings: S.standingsShareText({ competition: 'PE Raiz', stage: '1ª fase', round: 'Rodada 5', group: S.groupLabel('A') }, standings.groups[0], { url: 'https://x.test/c' }).split('\\n'),
    labels: [S.groupLabel('Grupo B'), S.groupLabel(''), S.groupLabel('A'), S.groupLabel('Chave 1')],
    headings: [S.standingsHeading('Grupo A'), S.standingsHeading('Chave 1'), S.standingsHeading('')],
    noPlace: [noPlace.querySelector('.match__meta').textContent.includes('null'), !!noPlace.querySelector('.match__place')],
    rankingTitle,
  };
}"""


def test_faixa_do_card_quando_e_onde_e_classificacao_parcial(browser_page):
    page, errors = browser_page
    out = page.evaluate(META_JS)
    assert not errors, errors
    venue, city, fin_venue, fin_city = out["names"]
    pill, when, place = out["scheduled"]  # "quando" (status, dia e hora) e "onde" (estádio e cidade, cada um com o seu ícone)
    assert pill == "Agendado" and re.fullmatch(r"Hoje · \d\d:\d\d", when) and place == [["#i-stadium", venue], ["#i-pin", city]]
    pill, when, place = out["finished"]
    assert pill == "Encerrado" and re.fullmatch(r"Hoje · \d\d:\d\d", when) and place == [["#i-stadium", fin_venue], ["#i-pin", fin_city]]
    assert out["standingsImage"] == ["image/png", 1080, True]  # 1080 de largura, altura pelo número de times
    assert out["labels"] == ["Grupo B", "", "Grupo A", "Chave 1"]
    assert out["headings"] == ["Classificação do Grupo A", "Classificação · Chave 1", "Classificação"]
    assert out["noPlace"] == [False, False]  # sem local: nada de "null" na faixa
    assert out["rankingTitle"] == "Classificação · PE Raiz"  # a classificação geral não é grupo
    assert out["playingBadges"][0] == out["playingBadges"][1] > 0 and out["subtitle"] and out["noPartial"]
    assert out["standings"] == [
        "*Classificação do Grupo A*", "PE Raiz · 1ª fase · Rodada 5", "",
        "1. Sport (jogando agora) — 13 pts · 5J · GP 11 · SG +8", "2. Náutico — 1 pt · 5J · GP 3 · SG -3", "", "🔗 https://x.test/c",
    ]


LONG_NAMES_JS = """async (width) => {
  const M = await import('/static/js/match-card.js');
  const F = await import('/static/js/fixtures.js');
  await document.fonts.load('700 16px "Barlow Condensed"'); // como na página (base.html pré-carrega)
  const root = document.getElementById('root');
  root.replaceChildren();
  root.style.cssText = `width:${width}px`;
  const live = F.MATCHES.live;
  const card = M.createMatchCard({ ...live, id: 9401, home: { ...live.home, name: 'Atlético Pernambucano' }, away: { ...live.away, name: 'Pernambucano' } }, {});
  const short = M.createMatchCard({ ...live, id: 9402 }, {});
  root.append(card, short);
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))); // ResizeObserver
  const read = (el) => [...el.querySelectorAll('.team__full')].map((full) => ({
    text: full.textContent, cut: full.scrollWidth - full.clientWidth, px: parseFloat(getComputedStyle(full).fontSize),
    sigla: getComputedStyle(full.parentElement.querySelector('.team__short')).display !== 'none' && getComputedStyle(full).display === 'none',
  }));
  return { long: read(card), short: read(short) };
}"""


@pytest.mark.parametrize("width", [288, 328, 343, 358])  # cards de celulares de 320 a 390 px
def test_nome_comprido_ao_lado_do_escudo_nao_e_cortado(browser_page, width):
    """No placar estreito, a palavra que não cabe ao lado do escudo ganha fonte menor (até 11 px),
    sem corte e sem quebrar no meio; se nem assim cabe (320 px), a sigla daquele time. O nome
    curto fica no tamanho do CSS (15 px)."""
    page, errors = browser_page
    out = page.evaluate(LONG_NAMES_JS, width)
    assert not errors, errors
    assert all(n["sigla"] or (n["cut"] <= 1 and n["px"] >= 11) for n in out["long"]), out["long"]
    if width >= 328:  # celular de 360 px para cima: o nome, menor, não a sigla
        assert not any(n["sigla"] for n in out["long"]), out["long"]
    assert all(not n["sigla"] and n["cut"] <= 1 and n["px"] == 15 for n in out["short"]), out["short"]


STANDINGS_WIDTH_JS = """async (width) => {
  const St = await import('/static/js/standings.js');
  const F = await import('/static/js/fixtures.js');
  await Promise.all(['700 16px "Barlow Condensed"', '600 16px "Barlow Condensed"', '600 14px Barlow'].map((f) => document.fonts.load(f)));
  const root = document.getElementById('root');
  root.replaceChildren();
  root.style.cssText = `width:${width}px`;
  // o pior caso: nomes compridos, dois dígitos em tudo, "10=" e "99*", quem está em campo e,
  // na classificação por posição, o grupo de cada time
  const big = (r, i) => ({ ...r, team: { ...r.team, name: i % 2 ? 'Atlético Pernambucano' : 'Sete de Setembro' }, rank: 10 + i,
    points: 99, played: 38, won: 30, drawn: 10, lost: 10, goals_for: 99, goal_difference: i % 2 ? -88 : 88, tied: i === 2 });
  const league = { ...F.STANDINGS, groups: [{ ...F.STANDINGS.groups[0], rows: F.STANDINGS.groups[0].rows.map(big) }] };
  const ranking = { stage_id: null, stage_name: 'Melhores segundos', kind: 'live', legend: [],
    groups: [{ id: null, name: 'Melhores segundos', rows: F.STANDINGS.groups[0].rows.slice(0, 4).map((r, i) => ({ ...big(r, i), group: `Grupo ${'ABCD'[i]}` })) }] };
  root.append(St.createStandings(league, { share: { title: 'PE Raiz' } }), St.createStandings(ranking));
  const visible = (el) => getComputedStyle(el).display !== 'none';
  return [...root.querySelectorAll('.standings__group')].map((group) => {
    const scroll = group.querySelector('.table-scroll');
    const names = [...group.querySelectorAll('tbody .team-cell__text')];
    return {
      overflow: scroll.scrollWidth - scroll.clientWidth,
      columns: [...group.querySelectorAll('thead th')].filter(visible).map((th) => th.textContent),
      cells: [...group.querySelectorAll('tbody tr')].every((tr) => [...tr.children].filter(visible).length === 9),
      sigla: names.every((t) => visible(t.querySelector('.team-cell__short')) && !visible(t.querySelector('.team-cell__name'))),
      full: names.every((t) => visible(t.querySelector('.team-cell__name')) && !visible(t.querySelector('.team-cell__short'))),
    };
  });
}"""


@pytest.mark.parametrize("width", [288, 328, 343, 358, 380, 400, 728])  # celular de 320 a 390 px, coluna do desktop, tablet
def test_classificacao_mesmas_colunas_em_toda_largura(browser_page, width):
    """A tabela tem as mesmas colunas em todas as larguras (# Time P J V E D GP SG; GC só na
    imagem compartilhada) e cabe sem rolagem lateral até no celular de 320 px: quando aperta, o
    nome vira a sigla (antes de qualquer coluna sumir)."""
    page, errors = browser_page
    out = page.evaluate(STANDINGS_WIDTH_JS, width)
    assert not errors, errors
    for table in out:
        assert table["columns"] == ["#", "Time", "P", "J", "V", "E", "D", "GP", "SG"], table
        assert table["cells"] and table["overflow"] <= 0, table
        assert table["sigla"] if width <= 328 else table["full"], table


STANDINGS_PARTS_JS = """async () => {
  const St = await import('/static/js/standings.js');
  const F = await import('/static/js/fixtures.js');
  await Promise.all(['600 16px "Barlow Condensed"', '400 13px Barlow'].map((f) => document.fonts.load(f)));
  const root = document.getElementById('root');
  root.replaceChildren();
  root.style.cssText = 'width:328px'; // celular de 360 px
  // fase de grupos: empate em tudo só no Grupo A (o 1º), alguém em campo só no Grupo B; o melhor 2º
  const [a, b] = F.STANDINGS_GROUPS.groups;
  const groups = { ...F.STANDINGS_GROUPS, legend: [
    { name: 'Classificados', color: '#12306B', from: 1, to: 1 },
    { name: 'Melhor 2º', color: '#7ACB8F', from: 2, to: 2, condition: 'só o 1º em “Melhores segundos”' },
  ], groups: [
    { ...a, rows: a.rows.map((r, i) => ({ ...r, tied: i === 1 || i === 2, rank: i === 2 ? 2 : r.rank })) },
    { ...b, rows: b.rows.map((r, i) => ({ ...r, playing: i === 0 })) },
  ] };
  const table = St.createStandings(groups);
  root.append(table);
  const item = [...table.querySelector('.legend').children].find((li) => li.textContent.startsWith('Melhor'));
  const lines = (el) => new Set([...el.getClientRects()].map((r) => Math.round(r.top))).size;
  const caption = (data, opts) => St.createStandings(data, opts).querySelector('caption').firstChild.textContent;
  return {
    foot: [...table.querySelectorAll('.standings__group')].map((g) => [
      g.querySelector('.standings__note')?.textContent ?? null,
      [...g.querySelectorAll('.legend__item--playing')].map((li) => li.textContent),
      !!g.querySelector('.criteria'),
    ]),
    dot: [...table.querySelectorAll('tbody .live-dot')].map((d) => [d.title, d.nextElementSibling?.textContent]),
    legend: [...table.querySelector('.legend').children].map((li) => li.textContent),
    legendLines: [lines(item.querySelector('.legend__name')), lines(item.querySelector('.legend__cond'))],
    condFont: getComputedStyle(item.querySelector('.legend__cond')).fontFamily,
    captions: [
      caption({ ...F.STANDINGS, season_stages: 2, stage_name: '2º turno' }),
      caption({ ...F.STANDINGS, season_stages: 1 }),
      caption({ ...F.STANDINGS, season_stages: 2, stage_name: '2º turno' }, { stageTitle: false }),
      caption({ ...F.STANDINGS }, { stageTitle: true }),
      caption({ ...F.STANDINGS, season_stages: 3, groups: [{ ...F.STANDINGS.groups[0], name: 'Classificação geral' }] }),
    ],
  };
}"""


def test_classificacao_legenda_nota_de_empate_e_titulo(browser_page):
    """Legenda como frase (o "Melhor 2º" sem a faixa repetida e sem quebrar no meio; a condição em
    texto corrido, não na fonte numérica condensada), "Jogando agora" na legenda da tabela que tem
    time em campo (o mesmo nome do ponto), a nota do "=" embaixo de cada tabela com empate e o
    título com a fase quando há outras ("Classificação · 2º turno")."""
    page, errors = browser_page
    out = page.evaluate(STANDINGS_PARTS_JS)
    assert not errors, errors
    note = "= Empatados em todos os critérios de desempate"
    # Grupo A: empate (nota), ninguém em campo; Grupo B: em campo, sem empate; critérios no último
    assert out["foot"] == [[note, [], False], [None, ["Jogando agora"], True]]
    assert out["dot"] == [["Jogando agora", ", jogando agora"]]
    assert out["legend"] == ["Classificados 1º", "Melhor 2º (só o 1º em “Melhores segundos”)"]  # a do Grupo A
    assert out["legendLines"][0] == 1 and "Condensed" not in out["condFont"]
    assert out["captions"] == ["Classificação · 2º turno", "Classificação", "Classificação", "Classificação · 1ª fase", "Classificação geral"]


LEGEND_LONG_JS = """async (width) => {
  const St = await import('/static/js/standings.js');
  const F = await import('/static/js/fixtures.js');
  await Promise.all(['600 16px "Barlow Condensed"', '400 13px Barlow', '600 13px "Barlow Condensed"'].map((f) => document.fonts.load(f)));
  const root = document.getElementById('root');
  root.replaceChildren();
  root.style.cssText = `width:${width}px`;
  const long = 'Classificados à Série D do Campeonato Brasileiro e Copa 2027'; // 60 caracteres (o limite do admin)
  const table = St.createStandings({ ...F.STANDINGS, legend: [
    { name: 'Classificados', color: '#12306B', from: 1, to: 2 },
    { name: long, color: '#7ACB8F', from: 3, to: 4 },
  ] });
  root.append(table);
  const group = table.querySelector('.standings__group').getBoundingClientRect();
  const lines = (el) => new Set([...el.getClientRects()].map((r) => Math.round(r.top))).size;
  return { length: long.length, items: [...table.querySelectorAll('.legend__item:not(.legend__item--playing)')].map((li) => {
    const name = li.querySelector('.legend__name');
    const range = li.querySelector('.legend__range');
    const pair = document.createRange();
    pair.setStartBefore(name);
    pair.setEndAfter(range);
    return { text: li.textContent, right: Math.round(li.getBoundingClientRect().right - group.right),
      nameLines: lines(name), rangeLines: lines(range), pairLines: lines(pair) };
  }) };
}"""


@pytest.mark.parametrize("width", [288, 328])  # card do celular de 320 e de 360 px
def test_legenda_nome_comprido_quebra_sem_cortar(browser_page, width):
    """O nome da zona vem do admin (até 60 caracteres): no card estreito ele quebra entre as
    palavras em vez de passar da borda do card (que corta o que sobra). A faixa não quebra no meio
    e o par curto ("Classificados 1º–2º") fica numa linha só."""
    page, errors = browser_page
    out = page.evaluate(LEGEND_LONG_JS, width)
    assert not errors, errors
    short, long = out["items"]
    assert out["length"] == 60
    assert short["text"] == "Classificados 1º–2º" and short["pairLines"] == 1 and short["right"] <= 0, short
    assert long["right"] <= 0 and long["nameLines"] > 1 and long["rangeLines"] == 1, long


CARD_LAYOUT_JS = """async () => {
  const M = await import('/static/js/match-card.js');
  const F = await import('/static/js/fixtures.js');
  const box = document.createElement('div');
  document.getElementById('root').append(box);
  const make = (match, width, opts = {}) => {
    const wrap = document.createElement('div');
    wrap.style.width = `${width}px`;
    box.append(wrap);
    const card = M.createMatchCard(match, opts);
    wrap.append(card);
    return card;
  };
  const open = async (card, tab) => {
    card.querySelector('details').open = true;
    await new Promise((r) => setTimeout(r, 30));
    if (tab) card.querySelector(`[data-tab="${tab}"]`).click();
  };
  // linhas de um bloco flex: centros verticais distintos dos filhos (alinhados ao centro)
  const lines = (el) => (el ? new Set([...el.children].map((c) => c.getBoundingClientRect()).filter((r) => r.height > 1).map((r) => Math.round((r.top + r.bottom) / 2))).size : 0);
  const metaLines = (card) => [lines(card.querySelector('.match__lead')), lines(card.querySelector('.match__place'))];
  const out = {};

  // 1) faixa no celular (card de 358 px): "quando" e "onde" numa linha cada; o estádio longo corta,
  // a cidade fica inteira; com a rodada, no máximo 3 linhas. Desktop (700 px): sem corte.
  const longVenue = { ...F.MATCHES.postponed, id: 9401, venue: 'Estádio Municipal Governador Paulo Guerra', city: 'São Lourenço da Mata, PE' };
  const kinds = { live: F.MATCHES.live, finished: F.MATCHES.finished, postponed: longVenue, cancelled: F.MATCHES.cancelled, scheduled: F.MATCHES.scheduledToday, suspended: F.MATCHES.suspended };
  out.mobile = Object.fromEntries(Object.entries(kinds).map(([k, m]) => [k, metaLines(make(m, 358))]));
  out.mobileRound = Object.fromEntries(Object.entries(kinds).map(([k, m]) => [k, metaLines(make(m, 358, { showRound: true })).reduce((a, b) => a + b)]));
  const cut = make(longVenue, 358);
  const text = cut.querySelector('.match__place-text');
  const city = cut.querySelector('.match__place-item--city');
  out.cut = [text.scrollWidth > text.clientWidth, city.scrollWidth <= city.clientWidth + 1, city.textContent, cut.querySelector('.match__place-item--venue').title,
    cut.querySelector('.match__place').getBoundingClientRect().right <= cut.getBoundingClientRect().right];
  const wide = make(longVenue, 700).querySelector('.match__place-text');
  out.desktopUncut = wide.scrollWidth <= wide.clientWidth + 1;

  // 2) lances no celular: faixa dos times no topo, presa ao rolar; lados mais largos
  const live = make(F.MATCHES.live, 358);
  box.append(Object.assign(document.createElement('div'), { style: 'height: 1500px' }));
  await open(live, 'events');
  const sides = live.querySelector('.tl-sides');
  const plain = [...live.querySelectorAll('.tl-item--away:not(.tl-item--goal) .tl-item__body')][0];
  out.sideWidth = Math.round(plain.getBoundingClientRect().width);
  out.sticky = getComputedStyle(sides).position;
  window.scrollTo(0, scrollY + sides.getBoundingClientRect().top + 300);
  out.stuckTop = Math.round(sides.getBoundingClientRect().top);
  const firstItem = live.querySelector('.timeline .tl-item');
  out.coversItems = getComputedStyle(sides).backgroundColor !== 'rgba(0, 0, 0, 0)' && Number(getComputedStyle(sides).zIndex) > Number(getComputedStyle(firstItem.querySelector('.tl-item__min')).zIndex);
  window.scrollTo(0, 0);

  // 3) ficha no desktop (768 px): a arbitragem na coluna larga, cada nome numa linha
  const long = { ...F.MATCHES.live, id: 9402, officials: [
    { role: 'referee', role_label: 'Árbitro', name: 'Gustavo Freitas Freitas', state: 'CE' },
    { role: 'assistant', role_label: 'Assistente', name: 'Elias Pereira Bezerra', state: 'PB' },
    { role: 'assistant', role_label: 'Assistente', name: 'Rodrigo Melo Almeida', state: 'CE' },
    { role: 'fourth', role_label: 'Quarto árbitro', name: 'Márcio Gomes Veloso', state: 'PE' },
    { role: 'var', role_label: 'VAR', name: 'Everton Freitas Monteiro', state: 'CE' }],
    attendance: 24339, revenue_cents: 126572600 };
  const facts = {};
  for (const width of [358, 768]) {
    const card = make(long, width);
    await open(card, 'facts');
    const [officials, other] = [...card.querySelectorAll('.fact')].map((f) => f.getBoundingClientRect());
    facts[width] = { oneLine: [...card.querySelectorAll('.fact__person')].every((p) => p.getClientRects().length === 1 && p.getBoundingClientRect().height < 30),
      officialsWider: officials.width > other.width, sideBySide: Math.round(officials.top) === Math.round(other.top) };
  }
  out.facts = facts;

  // 4) selo "Encerrado" discreto (tonal) e legível nos dois temas; "Ao vivo" continua o destaque
  const rgb = (c) => (c.startsWith('#') ? [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)) : c.match(/[\\d.]+/g).slice(0, 3).map(Number));
  const lum = (c) => { const v = rgb(c).map((x) => x / 255).map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4)); return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const ratio = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
  const finished = make(F.MATCHES.finished, 358).querySelector('.pill');
  const livePill = live.querySelector('.pill');
  const pills = {};
  for (const theme of ['light', 'dark']) {
    document.documentElement.dataset.theme = theme;
    const surface = getComputedStyle(document.documentElement).getPropertyValue('--surface').trim();
    const f = getComputedStyle(finished);
    pills[theme] = { text: ratio(f.color, f.backgroundColor) >= 4.5, quiet: ratio(f.backgroundColor, surface) < 1.5,
      liveLoud: ratio(getComputedStyle(livePill).backgroundColor, surface) >= 3 };
  }
  document.documentElement.dataset.theme = 'light';
  out.pills = pills;
  box.remove();
  return out;
}"""


def test_card_faixa_lances_ficha_e_selo_no_celular_e_no_desktop(browser_page):
    page, errors = browser_page
    out = page.evaluate(CARD_LAYOUT_JS)
    assert not errors, errors
    # "quando" numa linha e "onde" numa linha; o suspenso ("parou no 2º tempo") usa 2 no "quando"
    assert {k: v for k, v in out["mobile"].items() if k != "suspended"} == {
        k: [1, 1] for k in ("live", "finished", "postponed", "cancelled", "scheduled")
    }, out["mobile"]
    assert out["mobile"]["suspended"][1] == 1 and sum(out["mobile"]["suspended"]) <= 3
    assert all(total <= 3 for total in out["mobileRound"].values()), out["mobileRound"]  # com a rodada
    assert out["cut"] == [True, True, "Cidade: São Lourenço da Mata, PE", "Estádio Municipal Governador Paulo Guerra", True]
    assert out["desktopUncut"]
    # lances: lados com mais largura no celular; faixa dos times presa no topo ao rolar
    assert out["sideWidth"] >= 145, out["sideWidth"]
    assert out["sticky"] == "sticky" and out["stuckTop"] == 0 and out["coversItems"]
    # ficha: no desktop, a arbitragem ao lado de público e renda, mais larga; nomes sem quebrar
    assert out["facts"]["768"] == {"oneLine": True, "officialsWider": True, "sideBySide": True}
    assert out["facts"]["358"]["oneLine"] and not out["facts"]["358"]["sideBySide"]
    assert out["pills"] == {t: {"text": True, "quiet": True, "liveLoud": True} for t in ("light", "dark")}
