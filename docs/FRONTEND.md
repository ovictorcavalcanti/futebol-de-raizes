# Front-end — design system e ganchos das páginas

Guia para quem liga dados às páginas (`home.js`, `competition.js`, `operator.js`,
`api.js`, `stream.js`, `alerts.js`). A identidade está em `docs/IDENTIDADE.md`;
os formatos JSON, em `docs/CONTRACT.md` §3–6. Para ver tudo funcionando com dados
de exemplo: `DJANGO_DEBUG=1 python manage.py runserver` → **`/styleguide.html`**
(só com `DEBUG`; sem ele, 404).

## 1. Arquivos

| Arquivo | O que é |
| --- | --- |
| `static/css/app.css` | Folha única com `@layer tokens, base, layout, components, pages, utilities`. Mobile first; quebras 640/960/1200; container 1200 px; gutter 16 px no celular. |
| `static/fonts/*.woff2` | Alfa Slab One 400, Barlow 400–700, Barlow Condensed 500–700 (latin + latin-ext, `unicode-range`, `font-display: swap`). Licenças OFL ao lado. |
| `static/img/logo.svg`, `logo-dark.svg`, `logo-mark.svg`, `favicon.svg` | Marca: bola com a sombrinha do frevo e a estrela de Pernambuco; wordmark em curvas (gerado do Alfa Slab One com fontTools). |
| `static/img/azulejo.svg` | Padrão de azulejo (uma cor), usado como **máscara** — a cor vem do tema. |
| `static/img/empty-sertao.svg` | Ilustração do estado vazio (sol + mandacaru), versão arquivo. A versão que segue o tema é o símbolo `#ill-sertao` do sprite. |
| `static/sounds/gol.wav` | Fanfarra de metais, 1,2 s, 22050 Hz mono 16 bits. |
| `templates/base.html` | `<head>` (tema antes da 1ª pintura, `theme-color`, favicon, 2 fontes pré-carregadas, CSS), sprite embutido, cabeçalho, `<main>`, rodapé, `#toasts`, `theme.js`. |
| `templates/partials/` | `icons.svg` (sprite), `header.html`, `footer.html`, `logo.html`, `operator-templates.html`. |
| `templates/index.html`, `competition.html`, `operator.html` | Cascas das páginas com regiões, `<template>`s e `data-hook`s (seção 4). |
| `templates/styleguide.html` + `static/js/styleguide.js` | Guia de estilo para QA visual. |
| `static/js/*.js` | Módulos ES sem dependências (seção 5). |

### Blocos dos templates

`base.html` expõe `title`, `description`, `head` (ex.: `modulepreload`), `main`,
`templates` (os `<template>` da página) e `scripts`. Cada página já inclui o seu
script: `index.html` → `js/home.js`, `competition.html` → `js/competition.js`,
`operator.html` → `js/operator.js` (enquanto esses arquivos não existem, o
navegador registra um 404 — esperado).

## 2. Marca configurável

O contexto `brand` (`core/context_processors.py`, `settings.BRAND`) alimenta:
`brand.logo_src`, `brand.logo_dark_src`, `brand.logo_alt`, `brand.name`,
`brand.tagline`, `brand.favicon_src`, `brand.theme_color`.

* O cabeçalho tem dois `<img>`: `.brand__logo--light` e `.brand__logo--dark`; o CSS
  mostra um ou outro conforme `data-theme`. O escuro tem `loading="lazy"` (só baixa
  quando aparece).
* Sem `BRAND_LOGO_DARK_URL`: com o logo padrão, o tema escuro usa
  `img/logo-dark.svg`; com logo próprio, repete o mesmo arquivo.
* O símbolo do rodapé e do login usa `brand.favicon_src`.

## 3. Tema e tokens

* Claro é o padrão. O script inline do `<head>` lê `localStorage["fdr-theme"]`
  (com `try/catch`) e aplica `data-theme` no `<html>` e o `theme-color` antes da
  primeira pintura. `theme.js` liga todo botão `[data-theme-toggle]` (estado em
  `aria-pressed`; o nome acessível fica fixo, "Tema escuro", para o leitor de tela
  não anunciar o contrário do estado), guarda a escolha, sincroniza abas (`storage`)
  e dispara o evento `themechange` no `document`.
* Todas as cores são tokens em `:root` / `:root[data-theme="dark"]`:
  `--paper --surface --surface-2 --ink --ink-2 --ink-3 --line --line-strong --azul
  --vermelho --amarelo --verde`, `--field` (borda de campo de formulário e trilho do
  interruptor, ≥ 3:1 — WCAG 1.4.11), mais variantes AA para texto pequeno
  (`--verde-ink --amarelo-ink --vermelho-ink`), texto sobre cor (`--on-azul`…),
  tintas (`--azul-tint`…), `--frevo-stripe`, `--hatch`.
* **Ajuste de contraste (desvio de IDENTIDADE.md):** `--ink-3` claro passou de
  `#7B7365` para `#6F675A` e o escuro de `#8E8778` para `#A8A193` — os originais
  ficavam abaixo de 4.5:1 sobre `--paper`/`--surface-2` e, no escuro, sobre as tintas
  `--azul-tint`/`--amarelo-tint` (item selecionado, linha destacada). Campos de
  formulário usam `--field` (`#8F826C` / `#6A7AA0`) em vez de `--line-strong`, que
  dava 1.8:1. Cores que vêm da API (zona da tabela, amostra da legenda) ganham um
  contorno em tinta para não sumirem no fundo (ex.: azul-marinho no tema escuro).
  `tests/test_design_frontend.py` recalcula esses contrastes a partir do CSS. `--verde` claro (`#0E8A4A`,
  4.1:1) só é usado como elemento gráfico; texto verde usa `--verde-ink`.

## 4. Ganchos das páginas

Convenção: `id` para regiões únicas da página; `data-hook` para partes de
templates clonados (use `hooks(el)` de `render.js`). Regiões com `hidden` começam
escondidas; quem liga os dados tira o `hidden`.

### Comuns (base.html)

| Gancho | Uso |
| --- | --- |
| `#competitions-nav` (`aria-busy="true"`) › `ul[data-hook="competition-links"]` | Menu de competições. Preencha com `renderCompetitionNav(list, competitions, {activeSlug, liveSlugs})` (tira o `aria-busy`). Não existe no operador. |
| `#brasilia-clock` (`<time>`) | Relógio de Brasília: `mountClock(el, serverClock)`. Fora de qualquer `aria-live`. |
| `#live-status` (`hidden`) › `[data-hook="live-status-text"]` | "Reconectando ao vivo…" quando o stream cai. |
| `#toasts` | Região dos avisos passageiros: `showToast(texto, {kind})`. |
| `[data-theme-toggle]` | Botão de tema (já ligado por `theme.js`). |

### Home (`index.html`)

| Gancho | Uso |
| --- | --- |
| `#home-date` (`<time>`) | Data do dia: `formatDateLong(server_time)`. |
| `#latest-goals` (`hidden`) | Bloco "Últimos gols". Some quando não há jogo no dia. |
| `#goal-alert` (`aria-live="polite"`) | Avisos: `createGoalAlert(goal)` / `createGoalAlert(goal, {kind: 'correction', reason})`. Use `prepend`. |
| `#toggle-sound`, `#toggle-notifications` (`hidden`) | Botões liga/desliga (`aria-pressed`). Os rótulos trocam sozinhos via `.when-on`/`.when-off`. Mostre o de notificações só onde `new Notification()` funciona. |
| `#latest-goals-list` (`<ol>`) | `createLatestGoal(goal, {isNew})` por item, do mais novo ao mais antigo. |
| `#latest-goals-empty` (`hidden`) | "Nenhum gol hoje ainda. Paciência, que ele vem." |
| `#competitions` (`aria-busy="true"`) | Contém esqueletos; substitua por uma seção por competição e tire o `aria-busy`. |
| `#home-empty` (`hidden`) | "Hoje não tem jogo, visse?" |
| `#home-error` (`hidden`) › `[data-hook="home-retry"]` | Erro genérico com "Tentar de novo". |
| `#goal-sound` (`<audio preload="none">`) | Som do gol. |
| `<template id="tpl-competition-section">` | `data-hook`: `competition` (raiz), `competition-link`, `competition-name`, `competition-more` (links para `/competition.html?slug=`), `stages`. |
| `<template id="tpl-stage-block">` | `data-hook`: `stage` (raiz), `stage-name` (esconda quando a competição tem uma fase só), `stage-grid`, `matches` (cards), `standings` (aside). Fase sem tabela (mata-mata): remova o aside e acrescente `split--no-aside` ao `stage-grid`. |

Exemplo (é o que `styleguide.js` faz):

```js
const section = cloneTemplate('tpl-competition-section');
const p = hooks(section);
p['competition-name'].textContent = comp.name;
p['competition-link'].href = p['competition-more'].href = `/competition.html?slug=${encodeURIComponent(comp.slug)}`;
for (const stage of comp.stages) {
  const block = cloneTemplate('tpl-stage-block');
  const b = hooks(block);
  b['stage-name'].textContent = stage.name;
  b.matches.replaceChildren(...stage.matches.map((m) => createMatchCard(m, { now: () => clock.nowMs(), onExpand: loadMatch })));
  if (stage.standings) b.standings.append(createStandings(stage.standings));
  else { b.standings.remove(); b['stage-grid'].classList.add('split--no-aside'); }
  p.stages.append(block);
}
```

### Competição (`competition.html`)

| Gancho | Uso |
| --- | --- |
| `[data-hook="competition-name"]`, `[data-hook="season"]`, `[data-hook="competition-eyebrow"]` | Título (contém um esqueleto até ser preenchido), ano da temporada, rótulo acima. |
| `#stage-select` (`disabled`) | `<option value=stage.id>` por fase; habilite ao preencher. |
| `#round-prev`, `#round-next` (`disabled`), `#round-label` (`aria-live="polite"`) | Navegação ‹ Rodada 5 ›. |
| `#round-matches` (`aria-busy="true"`) | Cards da rodada (`createMatchCard`). |
| `#round-empty` (`hidden`) | "Nenhum jogo nesta rodada." |
| `#stage-standings` | `createStandings(stage.standings)` (fase com tabela). |
| `#stage-ties` (`hidden`) › `[data-hook="ties-list"]` | Mata-mata: `createTieCard(tie)` por confronto (TieDetailOut). |
| `#competition-missing` (`hidden`) | Slug inexistente. |
| `#competition-error` (`hidden`) › `[data-hook="competition-retry"]` | Erro genérico. |

### Operador (`operator.html`)

| Gancho | Uso |
| --- | --- |
| `#op-boot` | Esqueleto enquanto `/api/auth/me` não responde (esconda depois). |
| `#op-login` (`hidden`), `#login-form` (`username`, `password`), `#login-error` › `[data-hook="login-error-text"]`, `#login-submit` | Login (use `aria-busy="true"` no botão durante o envio). |
| `#op-app` (`hidden`) | Painel. `[data-hook="user-name"]`, `[data-hook="user-roles"]`, `[data-hook="admin-link"]` (`hidden`; mostre com `permissions.admin_site`), `#logout-button`. |
| `#picker-date`, `#picker-refresh`, `#picker-list` (`aria-busy`), `#picker-empty` | Escolha da partida; itens com `tpl-pick-item` (`aria-current="true"` no selecionado). |
| `#op-placeholder` | Estado vazio até escolher a partida. |
| `#op-scoreboard` (`hidden`) | Placar: `createMatchCard(match, {details: false, showRound: true, showCompetition: true})`. |
| `#op-work` (`hidden`) | Área de trabalho. |
| `#action-grid` | Botões `tpl-action-button` dos tipos em `available.events` (classe `action-btn--goal` no gol, `action-btn--structural` nos estruturais; `aria-pressed="true"` no que está com formulário aberto). |
| `#status-actions` | Botões `tpl-status-button` de `catalog.status_actions` (cancelar com `btn--danger`; `disabled` fora de `available.status`). |
| `#event-form-card` (`hidden`), `#event-form` | Formulário. `[data-hook]`: `event-form-icon` (`<use>`), `event-form-title`, `event-warnings` (+ `event-warnings-list`, para `confirmation_required`), `event-error`, `event-fields`, `event-cancel`, `event-submit`, `event-submit-label`. |
| `#op-timeline`, `#op-timeline-empty`, `[data-hook="timeline-count"]` | Lançamentos (mais recente primeiro) com `tpl-op-event`. |
| `#confirm-dialog` | Confirmação genérica: `confirm-title`, `confirm-text`, `confirm-ok`; `returnValue` = `"confirm"` ou `"cancel"`. |
| `#status-dialog` | `status-title`, `status-text`, `status-kickoff-field` (`hidden`; mostre no reagendar) › `#status-kickoff`, `#status-reason`, `status-ok`. |
| `#void-dialog` | `void-text`, `#void-reason`, `void-ok` (botão de perigo). |

Templates do operador (`partials/operator-templates.html`, um por `kind` do catálogo):

| Template | `data-hook`s |
| --- | --- |
| `tpl-pick-item` | `pick` (botão), `home-crest`, `home`, `score`, `away`, `away-crest`, `status` (troque por uma `.pill.pill--sm`), `time`, `competition` |
| `tpl-action-button` | `action` (raiz), `action-icon` (`<use>`: `setAttribute('href', '#i-' + spec.icon)`), `action-label` |
| `tpl-status-button` | `status-action` (raiz), `status-icon`, `status-label` |
| `tpl-field-minute` | `label`, `input` (minuto), `stoppage` (acréscimo), `hint` |
| `tpl-field-team` | `label` (legend), `input-home`/`input-away` (rádios: dê o mesmo `name` e `value=team.id`), `crest-home`/`crest-away`, `name-home`/`name-away` |
| `tpl-field-player` | `label`, `input`, `datalist` (dê ids e ligue `list=`; opções da escalação) |
| `tpl-field-text` | `label`, `input` (`<textarea>`) |
| `tpl-field-choice` | `label`, `input` (`<select>`; `choices` do catálogo) |
| `tpl-field-int` | `label`, `input` |
| `tpl-field-bool` | `label`, `input` (checkbox em `.switch`) |
| `tpl-field-event-ref` | `label`, `input` (`<select>` dos gols válidos; 1ª opção vazia = "gol ainda não lançado") |
| `tpl-field-datetime` | `label`, `input` (`datetime-local`, horário de Brasília) |
| `tpl-op-event` | `op-event` (raiz; classes `op-event--goal`, `--annulled`, `--structural`), `minute`, `icon` (`<use>`), `title`, `sub`, `void` (desabilite em derivados: o vermelho automático só cai com o amarelo) |

Ao clonar campos, gere `id` únicos e ligue `label.htmlFor`. Campos aparecem/somem com `hidden`.

## 5. Módulos JS (assinaturas)

Todos são módulos ES puros, sem dependências; texto entra só por
`textContent`/atributos (há teste que barra `innerHTML` em `static/js`).

### `render.js`
* `h(tag, attrs?, ...children) → HTMLElement` — `attrs`: `class` (string ou array), `text`, `dataset`, `style` (objeto, aceita `--vars`), `on<Evento>`, `true` = atributo vazio, `null/false` = omitido.
* `s(tag, attrs?, ...children) → SVGElement`; `setAttrs(el, attrs)`; `append(parent, children[])`.
* `clear(el)`, `setText(el, text)` (só escreve se mudou).
* `svgUse(href, attrs?) → <svg><use>`.
* `cloneTemplate(idOuTemplate) → Element|DocumentFragment`; `hook(root, nome)`; `hooks(root) → {nome: el}`.
* `showToast(texto, {kind: 'info'|'ok'|'error'|'warn', timeout=4500, action?: {label, onClick}}) → {close}`.
* `renderCompetitionNav(ul, competitions, {activeSlug?, liveSlugs?: Set, href?: (c) => url})`.

### `icons.js`
* `icon(nome, {className?, label?}) → SVGSVGElement` — com `label` vira `role="img"`.
* `eventIconName(event) → string` — espelha `domain.event_icon` (pênalti, contra, 2º amarelo, cobrança perdida); usa `event.icon` se vier.
* `ICONS` (todos os nomes), `EVENT_ICONS` (tipo → ícone).

### `format.js` (sempre `America/Sao_Paulo`)
* `formatTime(v) → "16:30"`, `formatClock(v) → "16:30:05"`, `formatDate(v) → "sáb, 03/10"`, `formatDateLong(v) → "sábado, 3 de outubro"`.
* `dayKey(v) → "2026-10-03"` (dia em Brasília, formato do `?date=`), `dayDiff(v, now)`, `relativeDay(v, now) → "Hoje"|"Amanhã"|"Ontem"|null`, `formatDay(v, now)`, `formatWhen(v, now) → "Hoje · 16:30"`.
* `formatInt(n) → "42.318"`, `formatMoney(centavos) → "R$ 2.341.550,00"`, `formatScore(a, b) → "2 × 1"`, `minuteLabel(min, acr) → "45+2'"`, `ordinal(n)`, `toDate(v)`.

### `clock.js`
* `new ServerClock({samples = 5})`
  * `sync(server_time, localMs = Date.now())` — registra uma amostra (resposta da API ou `ping`); o desvio em uso é o **maior** dos 5 últimos.
  * `offset`, `synced`, `nowMs()`, `now()`, `today()`.
  * `onTick(cb(nowMs)) → unsubscribe` — a cada segundo, alinhado à virada do segundo.
  * `onDayChange(cb(novoDia, diaAnterior)) → unsubscribe`; `checkDay()` (chame no `visibilitychange`).
  * `start()`, `stop()`.
* `mountClock(timeEl, clock) → unsubscribe` — relógio do cabeçalho.
* `liveMinute(match, nowMs) → {minute, stoppage}|null` e `liveMinuteLabel(match, nowMs) → "72'"|"45+2'"|"INT"|"PÊN"|período|""` — `clock.offset + minutos inteiros desde period_started_at + 1`; passou de `regular_end` vira acréscimo.

### `crest.js`
* `createCrest(team, {size = 40, className?}) → <img loading=lazy>|<svg>` — `crest_url` com fallback para o escudo gerado se a imagem falhar.
* `shieldSvg(team, opts)` (escudo com `color_primary`/`color_secondary` e sigla), `contrastInk(hex)`.

### `theme.js`
* Liga-se sozinho ao ser importado. `getTheme()`, `setTheme('light'|'dark', {persist = true})`, `toggleTheme()`, `initTheme()`, `THEME_KEY`.

### `match-card.js`
* `createMatchCard(match, opts) → <article class="match">`
* `updateMatchCard(el, match, opts)` — substitui o estado (sem merge), **mantém o `<details>` aberto e a aba ativa** (e o foco na aba). Se a mensagem nova vier resumida com a mesma `version`, o detalhe já carregado continua; se vier resumida com `version` maior e o acordeão estiver aberto, o card chama `onExpand` de novo. **`version` menor que a desenhada é ignorada** (ex.: o `GET /api/matches/:id` pedido ao abrir chega depois de uma mensagem `match` mais nova do stream) — a página pode chamar sem se preocupar com a ordem das respostas.
* `opts`: `now: () => ms` (relógio do servidor), `onExpand(matchId) → void|Promise` (chamado na 1ª abertura sem `match.events`; com Promise rejeitada aparece "Tentar de novo"), `flash: true` (só no update: pisca o placar que subiu e destaca lances novos por 5 s; respeita `prefers-reduced-motion`), `details: false` (sem acordeão), `showRound`, `showCompetition`.
* `tickMatchCards(root, nowMs)` — atualiza só o minuto dos jogos ao vivo (chame no `onTick`).
* `setMatchCardError(el)`, `getCardMatch(el)`, `renderTimeline(match, newIds?)`.
* `safeHref(url) → string|null` — só `http(s)`; usado nos links de transmissão (barra `javascript:`/`data:`).
* `createTieCard(tieDetail, {now}) → card do confronto` (agregado, vencedor, forma da decisão, jogos).
* `createLatestGoal(latestGoal, {isNew}) → <li>` da lista de últimos gols.
* `createGoalAlert(latestGoal, {kind: 'goal'|'correction', reason?: 'annulled'|'voided', onClose?}) → aviso` ("É gol!" / "Oxe! Gol anulado." + "Lance corrigido pelo operador.").

O card cobre todos os status: agendado (pílula com horário, dia relativo), ao vivo
(minuto correndo, faixa vermelha no topo), intervalo (amarelo), encerrado
(vencedor em destaque, perdedor esmaecido), pênaltis `(4) × (3) pên.`, suspenso,
adiado e cancelado (hachura). Linha do confronto no mata-mata (agregado e quem
avança, com o agregado na mesma ordem mandante × visitante do placar do card).
Resumo sem abrir: autores dos gols e vermelhos. Abas só com dado:
**Lances** (linha do tempo de dois lados com trilho central e separadores de
período; gol anulado riscado com o motivo; lance sem time, como o VAR, vai ao
centro com o minuto junto do texto; em cards estreitos o placar parcial
desce para o trilho), **Escalações** e **Ficha** (arbitragem, público e renda,
transmissões com link externo, estatísticas em barras com as cores dos times).
No aviso de correção (`kind: 'correction'`), o placar do gol que caiu aparece
riscado (`<s>`), nunca como placar atual.

### `standings.js`
* `createStandings(stageStandings, {compact?, legend = true, criteria = true, highlightTeamIds?: Set}) → <div class="standings">`
* `updateStandings(el, stageStandings, opts?)` — redesenha no lugar; sem `opts`, valem as da criação (a mensagem `standings` do stream não precisa repassá-las).
* Uma `<table>` por grupo com `<caption>`; faixa de zona (cor da API em `--zone`) + nome da zona em texto (visível no início de cada zona, e no texto acessível de toda linha); ponto pulsante em quem está `playing`; `=` em `tied`; legenda com `<svg><rect fill="cor da API">`; critérios em `<ol>` na ordem configurada. Colunas somem por container query (GP/GC abaixo de 440 px; V/E/D abaixo de 330 px; sigla abaixo de 280 px); `compact` força a versão sem GP/GC.

### `fixtures.js` (só para o guia de estilo)
`SERVER_TIME`, `TIMEZONE`, `TEAMS`, `COMPETITIONS`, `MATCHES` (`live`, `halfTime`, `scheduledToday`, `scheduledTomorrow`, `finished`, `postponed`, `suspended`, `cancelled`, `knockoutLeg1`, `knockoutPenalties`, `knockoutSingle`), `TIES`, `STANDINGS`, `STANDINGS_GROUPS`, `LATEST_GOALS`, `HOME`, `COMPETITION`, `ME`, `CATALOG`, `AVAILABLE`, `summaryOf(match)`. Os horários são relativos ao carregamento do módulo.

## 6. Ciclo sugerido de uma página pública

```js
import { ServerClock, mountClock } from './clock.js';
const clock = new ServerClock();
const data = await getHome();              // api.js
clock.sync(data.server_time);
mountClock(document.getElementById('brasilia-clock'), clock);
// desenhar com createMatchCard/createStandings…
clock.onTick((ms) => tickMatchCards(document, ms));
clock.onDayChange(() => reload());
document.addEventListener('visibilitychange', () => document.hidden || clock.checkDay());
// stream.js: a cada ping → clock.sync(ping.server_time);
// mensagem match → updateMatchCard(card, msg.match, { flash: true });
// mensagem standings → updateStandings(el, msg.standings);
```

## 7. Desempenho e acessibilidade

* Uma folha de estilo, sprite embutido (zero requisições de ícone), 2 fontes
  pré-carregadas, `modulepreload` dos módulos da página, imagens com
  `width/height`, `loading="lazy"` no logo escuro e nos escudos remotos,
  `content-visibility: auto` nas seções de competição, detalhe do jogo desenhado
  só com o acordeão aberto, minuto atualizado sem redesenhar o card.
* Foco visível (anel azul + halo amarelo), `prefers-reduced-motion`, impressão
  básica, sem rolagem horizontal a 390 px, cor nunca é o único sinal (status em
  texto, nome da zona, "avança", `aria-label` no placar: "Sport 2 a 1 Náutico").
* Testes: `tests/test_design_frontend.py` (sem banco: ganchos, marca, contraste AA
  recalculado dos tokens, `modulepreload` sem cascata e, se houver Chromium do
  Playwright, os componentes no navegador — versão atrasada ignorada, detalhe pedido
  de novo, agregado na ordem do placar, link `javascript:` barrado, aviso de correção,
  botão de tema) e `tests/js/*.test.mjs` (`node --test tests/js/format_clock.test.mjs
  tests/js/match_card.test.mjs`; o pytest roda também em outro fuso).
