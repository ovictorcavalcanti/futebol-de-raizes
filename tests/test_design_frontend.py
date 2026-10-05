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
    domain_icons = {spec.icon for spec in domain.CATALOG.values()} | {
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

  // 4) lance sem time (VAR) mostra o minuto junto do texto
  const timeline = M.renderTimeline(live);
  const varItem = [...timeline.querySelectorAll('.tl-item--neutral')].find((li) => li.textContent.includes('VAR'));
  out.varMinute = varItem?.querySelector('.tl-item__min-inline')?.textContent || '';

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

  // 6) aviso de correção: placar do gol que caiu riscado
  const alert = M.createGoalAlert(F.LATEST_GOALS[0], { kind: 'correction', reason: 'annulled' });
  out.correctionStruck = !!alert.querySelector('s.goal-alert__void-score');
  out.correctionTitle = alert.querySelector('.goal-alert__title').textContent;
  out.goalNotStruck = !M.createGoalAlert(F.LATEST_GOALS[0]).querySelector('s');

  // 7) classificação: atualizar sem opts mantém as da criação
  const table = S.createStandings(F.STANDINGS, { compact: true, legend: false });
  S.updateStandings(table, F.STANDINGS);
  out.standingsKeepsOpts = table.classList.contains('standings--compact') && !table.querySelector('.legend');

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
    assert out["varMinute"] == "31'"
    assert out["links"] == ["https://tv.example.com/"] and out["maliciousAsText"]
    assert (
        out["correctionStruck"]
        and out["correctionTitle"] == "Oxe! Gol anulado."
        and out["goalNotStruck"]
    )
    assert out["standingsKeepsOpts"]
    assert out["goalRingKept"] and out["goalRingCleared"]
    assert out["colorSanitized"]
    assert out["themeDark"] and out["themeLabel"] == "Tema escuro"
    # gol anulado riscado com minuto e motivo; anulação avulsa com o motivo; nada de "Oxe!"
    assert "Gol anulado aos 36' — Impedimento marcado pelo VAR" in out["annulStruck"]
    assert "Mão na bola" in out["annulTimeline"]
    assert "Oxe" not in out["annulTimeline"]
