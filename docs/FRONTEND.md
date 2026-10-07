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
| `templates/base.html` | `<head>` (tema antes da 1ª pintura, `theme-color`, favicon, 3 fontes pré-carregadas — Barlow 400, Barlow Condensed 700 e Alfa Slab One, a face do h1, que é o LCP da home —, CSS), sprite embutido, cabeçalho, `<main>`, rodapé, `#toasts`, `theme.js`. |
| `templates/404.html`, `403.html`, `500.html` | Páginas de erro com DEBUG desligado. 404 e 403 estendem `base.html` (renderizadas com o request: marca, tema, sprite; sem `page`, então sem o menu de competições) com o estado vazio do sertão e "Ver os jogos de hoje". A 500 é autossuficiente (o `server_error` do Django renderiza sem request nem context processors): CSS embutido com papel/tinta e a faixa do frevo, tema escuro pela escolha salva, "Não deu certo agora." e link para `/`. |
| `templates/admin/base_site.html` | Admin com a marca: logo (largura limitada), `brand.name · Administração`, faixa do frevo, paleta nos dois temas e o tema sincronizado com o do site (seção 3). |
| `templates/partials/` | `icons.svg` (sprite), `header.html`, `footer.html`, `logo.html`, `operator-templates.html`. |
| `templates/index.html`, `competition.html`, `operator.html` | Cascas das páginas com regiões, `<template>`s e `data-hook`s (seção 4). |
| `templates/styleguide.html` + `static/js/styleguide.js` | Guia de estilo para QA visual. |
| `static/js/*.js` | Módulos ES sem dependências (seção 5). |

### Blocos dos templates

`base.html` expõe `title`, `description`, `head` (ex.: `modulepreload`), `main`,
`templates` (os `<template>` da página) e `scripts`. Cada página já inclui o seu
script: `index.html` → `js/home.js`, `competition.html` → `js/competition.js`,
`operator.html` → `js/operator.js` (comportamento na seção 6).

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
* Logo de qualquer proporção: `.brand` encolhe (`flex: 0 1 auto`) e `.brand__logo` tem
  altura fixa (36/40/44 px) com largura limitada a `min(240px, 100%)` e `object-fit:
  contain` — um logo horizontal largo (10:1) não empurra relógio e tema para fora da tela.
  No admin, `max-width: min(240px, 55vw)`.
* `BRAND_NAME` vale também para o `alt` do logo (quando `BRAND_LOGO_ALT` não vem) e para o
  cabeçalho e o título do admin (`brand.name · Administração`).

## 3. Tema e tokens

* Claro é o padrão. O script inline do `<head>` lê `localStorage["fdr-theme"]`
  (com `try/catch`) e aplica `data-theme` no `<html>` e o `theme-color` antes da
  primeira pintura. `theme.js` liga todo botão `[data-theme-toggle]` (estado em
  `aria-pressed`; o nome acessível fica fixo, "Tema escuro", para o leitor de tela
  não anunciar o contrário do estado), guarda a escolha, sincroniza abas (`storage`)
  e dispara o evento `themechange` no `document`.
* **Admin no mesmo tema:** o `admin/js/theme.js` do Django começaria em "auto" (segue o
  sistema) com um ciclo de 3 estados e chave própria (`localStorage["theme"]`). O
  `templates/admin/base_site.html` sobrescreve o bloco `dark-mode-vars` e, **antes** do
  script do Django, copia a escolha do site (`fdr-theme`; claro sem ela) para `theme`; um
  ouvinte de clique em fase de captura troca o ciclo do botão do admin por claro ⇄ escuro e
  grava nas duas chaves — a escolha vale nos dois sentidos (site ⇄ admin).
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
| `#competitions-nav` (`aria-busy="true"`) › `ul[data-hook="competition-links"]` | Menu de competições. Preencha com `renderCompetitionNav(list, competitions, {activeSlug, liveSlugs})` (tira o `aria-busy`). Só nas páginas com `page` público (home, competição, guia): não existe no operador nem nas páginas de erro (sem script para preenchê-lo). Esqueleto e links têm a mesma altura (`.comp-nav__list { min-height: 44px }`), sem layout shift. |
| `#brasilia-clock` (`<time>`) | Relógio de Brasília: `mountClock(el, serverClock)`. Fora de qualquer `aria-live`. |
| `#live-status` (`hidden`) › `[data-hook="live-status-text"]` | "Reconectando ao vivo…" quando o stream cai. |
| `#toasts` | Região dos avisos passageiros: `showToast(texto, {kind})`. |
| `[data-theme-toggle]` | Botão de tema (já ligado por `theme.js`). |

### Home (`index.html`)

| Gancho | Uso |
| --- | --- |
| `#home-date` (`<time>`) | Data do dia: já vem no HTML (`{% now %}`, Brasília) e o JS reescreve com `formatDateLong` da data da resposta — a linha nunca aparece depois (sem layout shift). |
| `#latest-goals` | Bloco "Últimos gols", **visível desde a 1ª pintura** com um chip esqueleto (altura de um `.goal-chip`), sem layout shift quando os dados chegam. `home.js` o esconde no dia sem jogo (o estado vazio toma o lugar) e no erro da carga inicial. |
| `#goal-alert` (`aria-live="polite"`) | Avisos: `createGoalAlert(goal)` / `createGoalAlert(goal, {kind: 'correction', reason})`. Use `prepend`. |
| `#toggle-sound`, `#toggle-notifications` (`hidden`) | Botões liga/desliga (`aria-pressed`). Os rótulos trocam sozinhos via `.when-on`/`.when-off`. O de notificações só aparece onde `new Notification()` funciona: um script inline logo depois dele (antes da 1ª pintura) aplica a mesma regra de `isMobileDevice`/`notificationSupport` (sem o teste do construtor) e `alerts.js` confirma depois — o botão não surge com os módulos empurrando a página. |
| `#notifications-hint` (`hidden`) | "As notificações do sistema só funcionam com HTTPS." — aparece quando a página não está em contexto seguro. |
| `#latest-goals-list` (`<ol tabindex="0">`, `aria-busy="true"`) | `createLatestGoal(goal, {isNew})` por item, do mais novo ao mais antigo (substitui o chip esqueleto; `home.js` tira o `aria-busy`). Rola para o lado: `tabindex="0"` dá foco pelo teclado (os chips não são focáveis) e a borda direita esmaece como dica de rolagem. |
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
| `[data-hook="picker"]` (`<div role="region">`), `#picker-date`, `#picker-refresh`, `#picker-list` (`aria-busy`), `#picker-empty` | Escolha da partida (região nomeada, não `<aside>`: fica dentro da seção do painel). `#picker-list` é uma lista de listas: um `li.match-picker__group` por competição com `h3.match-picker__group-title` e `ul.match-picker__sublist` (`aria-labelledby` no título) com os itens `tpl-pick-item` (`aria-current="true"` no selecionado). |
| `#op-placeholder` | Estado vazio até escolher a partida. |
| `#op-scoreboard` (`hidden`) | Placar: `createMatchCard(match, {details: false, showRound: true, showCompetition: true})`. |
| `#op-work` (`hidden`) | Área de trabalho. |
| `#action-grid` | Botões `tpl-action-button` dos **lances de jogo** de `available.events` (gol, cartões, substituição…; classe `action-btn--goal` no gol; `aria-pressed="true"` no que está com formulário aberto), na ordem da API. Sem nenhum: "Nenhum lance disponível neste momento." (ou "Nenhum lance de jogo neste momento." quando só há andamento). |
| `#flow-block` (`hidden`) › `#flow-actions` | **Andamento do jogo**: os estruturais de `available.events` (início, fim do 1º tempo, 2º tempo, prorrogação, pênaltis, fim), depois dos lances e com outra cara (`.action-grid--flow`: botão baixo e largo, fundo rebaixado, faixa amarela, classe `action-btn--structural`). Some sem estrutural disponível. Todo envio daqui passa pelo `#confirm-dialog`. |
| `#status-actions` | Botões `tpl-status-button` de `catalog.status_actions` (cancelar com `btn--danger`; `disabled` fora de `available.status`). |
| `[data-hook="actions-card"]` › `actions-block` (lances + andamento) / `status-block` | Cartão das ações; cada bloco some sem a permissão (`post_event` / `change_status`), o cartão some sem as duas. |
| `#event-form-card` (`hidden`), `#event-form` | Formulário. `[data-hook]`: `event-form-icon` (`<use>`), `event-form-title`, `event-warnings` (+ `event-warnings-list`, para `confirmation_required`), `event-error`, `event-type` (`#event-type`: `<select>` dos tipos em `available.events`, lances primeiro e os estruturais num `<optgroup label="Andamento do jogo">`), `event-fields`, `event-cancel`, `event-submit`, `event-submit-label`. |
| `#op-timeline`, `#op-timeline-empty`, `[data-hook="timeline-count"]` | Lançamentos (mais recente primeiro) com `tpl-op-event`. |
| `#confirm-dialog` | Confirmação genérica (andamento do jogo e avisos `confirmation_required`): `confirm-title`, `confirm-text`, `confirm-list` (`<ul>` dos avisos, `hidden` sem itens), `confirm-ok`; `returnValue` = `"confirm"` ou `"cancel"` ("Voltar"). |
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
| `tpl-op-event` | `op-event` (raiz; classes `op-event--goal`, `--annulled`, `--structural`), `minute`, `icon` (`<use>`), `title`, `sub`; `edit` e `void` dentro de `.op-event__actions`, numa linha própria embaixo do texto (some quando os dois botões saem; desabilite `void` em derivados: o vermelho automático só cai com o amarelo) |

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
* `annulledGoalNote(goalAnnulledEvent) → "Gol anulado aos 60' — Impedimento (VAR)"` — a linha do gol riscado na linha do tempo. A linha do tempo é registro (dado): sem o "Oxe!", que fica só no aviso ao vivo (IDENTIDADE §5); a anulação avulsa mostra o motivo sob o título "Gol anulado".

O card cobre todos os status: agendado (pílula com horário, dia relativo), ao vivo
(minuto correndo, faixa vermelha no topo), intervalo (amarelo), encerrado
(vencedor em destaque, perdedor esmaecido), pênaltis `(4) × (3) pên.`, suspenso,
adiado e cancelado (hachura). Linha do confronto no mata-mata (agregado e quem
avança, com o agregado na mesma ordem mandante × visitante do placar do card).
Nome do time: sigla no celular, nome completo a partir de 520 px de card; quebra só entre
palavras (`overflow-wrap: normal`, `text-wrap: balance`; a sigla nunca quebra) e o ✓ do
vencedor fica fora do fluxo (no canto, junto da faixa do time), sem apertar o nome; em card
estreito (< 380 px) escudo, vão e coluna do placar diminuem. Resumo sem abrir: autores dos
gols e vermelhos. Abas só com dado:
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
* Punição/bonificação em pontos: linha com `points_adjustment ≠ 0` ganha `<abbr class="adj-mark">*</abbr>` nos pontos (título acessível "Punição: perdeu 3 pontos fora de campo", mais texto escondido para leitor de tela); `adjustments` vira a lista `.adjustments` embaixo da legenda ("Santa Cruz: −3 pts — escalação irregular", sempre visível, mesmo com `legend: false`). Funções puras exportadas: `adjustmentLabel(points)`, `adjustmentNote(item)`, `adjustmentTitle(points)` (tests/js/standings.test.mjs).

### `api.js`
* `apiFetch(path, {method, body, query, headers, idempotencyKey, timeout = 15000, signal}) → Promise<json>` — `credentials: 'same-origin'`, `X-CSRFToken` (cookie `csrftoken`; reserva: `csrf_token` de `/api/auth/me`) só nos métodos que mudam estado, `Idempotency-Key` quando pedido, tempo limite com `AbortController`.
* `ApiError {status, code, message, details, warnings, isNetwork}` — formato de erro do contrato; o padrão do ninja (`{detail}`) vira `csrf_failed`/`invalid_input`/… ; falha de rede = `network_error`, tempo esgotado = `timeout` (status 0).
* Rotas: `getMe`, `login`, `logout`, `getHome(date?)`, `getCompetitions`, `getCompetition(slug, {stage, round})`, `getStageStandings`, `listMatches({roundId, date, status, stageId})`, `getMatch`, `getCatalog`, `postEvent(id, body, key)`, `voidEvent(id, eventId, reason)`, `changeStatus(id, body, key)`.
* `newIdempotencyKey()` (`crypto.randomUUID`, com reserva fora de contexto seguro), `getCookie`, `queryString`, `toApiError`.

### `stream.js`
* `createStream({handlers: {match, standings, goals, ping}, onStatus, onStale, clock}) → {start(cursor), restart(cursor), reconnect(), stop(), lastId, status}` — um `EventSource` em `/api/stream?after=`; guarda o id da última mensagem; cada `ping` faz `clock.sync(server_time)`.
* Recria (com `after` = último id) ao voltar ao primeiro plano (`visibilitychange`, `pageshow` do bfcache, `online`), depois de 45 s sem ping e quando o navegador desiste (`CLOSED`, espera de 1 s a 30 s). Ao voltar ao primeiro plano com a conexão aberta e contato há menos de 25 s, mantém (refinamento: não derruba uma conexão comprovadamente viva).
* Mais de 5 min sem stream: não pede reenvio — chama `onStale()` (a página busca o estado inteiro e devolve o cursor novo) e reabre dali.
* Decisões puras exportadas: `decideReconnect({trigger, readyState, lastContactAt, openedAt, nowMs}) → 'none'|'replay'|'refetch'`, `backoffDelay`, `streamUrl`, `parseEventId`; `liveStatusIndicator(#live-status)` mostra "Reconectando ao vivo…" depois de 4 s sem conexão — mais que o `retry: 3000`, para não piscar numa reconexão normal (no celular, só o ícone).

### `alerts.js` (só a home)
* `GoalAlertTracker` (puro): `decide(changes, serverNowMs, {isRelevant}) → [{action: 'alert'|'silent'|'correction'|'restore'|'skip', goal, reason}]` — um alerta por id de evento; lista inicial = já alertada; gol com `created_at` mais de 2 min atrás (relógio do servidor) entra sem alerta; `removed` → correção (uma vez); `restored` → volta sem alerta; gol de jogo fora da home não alerta.
* `createGoalAlerts({region, soundButton, notifyButton, audio, hint, serverNow, initialGoals, isRelevant, support?}) → {handle(goalsMessage), reset(goals)}` (`support`: resultado de `notificationSupport()` já calculado pela página, que mostra o botão antes dos dados) — aviso na página (`createGoalAlert`, máx. 3, somem em 2 min; a correção toma o lugar do "É gol!" do mesmo gol), som (`#goal-sound`; o clique em "Ativar som" toca a amostra e libera o áudio; recusa do navegador volta o botão a "Ativar som"), `Notification` (fecha a do gol e abre a de correção). Preferências em `localStorage` (`fdr-sound`, `fdr-notifications`, com `try/catch`).
* `notificationSupport(window)` — API presente, contexto seguro e computador (`userAgentData.mobile`, user agent, iPadOS com toque); sem permissão, testa `new Notification()` em `try/catch`.

### `operator.js` (partes puras exportadas)
`effectiveMinuteMode(spec, period)` (espelha `domain.minute_mode`), `suggestMinute(match, spec, nowMs)`, `buildEventBody(type, entries, {minute, stoppage})`, `toBrasiliaInput(iso)`, `groupByCompetition`, `lineupPlayers(match, teamId, {opponent})`, `splitActions(types, specOf) → {game, flow}` (lances de jogo × estruturais, na ordem da API), `structuralConfirm(type, match, label?) → {title, text, ok}` (texto da confirmação de cada estrutural, com o placar). A tela só liga quando a página tem `#op-app`.

### `fixtures.js` (só para o guia de estilo)
`SERVER_TIME`, `TIMEZONE`, `TEAMS`, `COMPETITIONS`, `MATCHES` (`live`, `halfTime`, `scheduledToday`, `scheduledTomorrow`, `finished`, `postponed`, `suspended`, `cancelled`, `knockoutLeg1`, `knockoutPenalties`, `knockoutSingle`), `TIES`, `STANDINGS`, `STANDINGS_GROUPS`, `LATEST_GOALS`, `HOME`, `COMPETITION`, `ME`, `CATALOG`, `AVAILABLE`, `summaryOf(match)`. Os horários são relativos ao carregamento do módulo.

## 6. Comportamento das páginas

Ciclo das páginas públicas: busca o estado, desenha, assina o stream a partir do
`cursor` e substitui o trecho afetado a cada mensagem (sem merge).

```js
const clock = new ServerClock();
const data = await getHome();                       // api.js
clock.sync(data.server_time);                       // e a cada resposta/ping
mountClock(document.getElementById('brasilia-clock'), clock);
// desenhar com createMatchCard/createStandings…
const stream = createStream({ clock, handlers: { match, standings, goals }, onStale: reload,
  onStatus: liveStatusIndicator(document.getElementById('live-status')) });
stream.start(data.cursor);
clock.onTick((ms) => tickMatchCards(root, ms));
clock.onDayChange(() => reload().then((cursor) => stream.restart(cursor)));
document.addEventListener('visibilitychange', () => document.hidden || clock.checkDay());
```

### Home (`home.js`)
* `GET /api/competitions` (menu, com o ponto de "ao vivo" recalculado a cada `match`) + `GET /api/home`.
* Uma seção por competição (`tpl-competition-section`, ordem de `position`), cards à
  esquerda e classificação ao vivo à direita; sem jogo no dia: menu, relógio e
  `#home-empty` (sem o bloco de últimos gols).
* `match` → `updateMatchCard(card, match, {flash: true})` (acordeão e aba continuam);
  jogo desconhecido com início no dia → busca a home de novo. `standings` →
  `updateStandings` da fase. `goals` → `alerts.handle` + lista da mensagem
  (gols alertados com `isNew`). Acordeão sem lances → `GET /api/matches/:id`.
* Recarga (virada do dia, 5 min sem stream, "Tentar de novo") reaproveita os cards
  (acordeões abertos continuam), marca a lista como já alertada e reabre o stream no
  cursor novo.

### Competição (`competition.js`)
* `competition.html?slug=&stage=&round=` → `GET /api/competitions/:slug?stage=&round=`
  (sem os dois, fase e rodada atuais; `current_round_id` = rodada exibida). Fase/rodada
  da URL que não existe mais (404) → tenta sem elas; slug inexistente → `#competition-missing`.
* ‹ › trocam a rodada sem recarregar (`history.replaceState`): liga/grupos com
  `GET /api/matches?roundId=`; **mata-mata com `GET /api/competitions/:slug?stage=&round=`**,
  que traz os confrontos com todos os jogos (a ida pode estar em outra rodada).
  Botões desabilitados nas pontas. O `<select>` troca a fase.
* Liga/grupos: cards + classificação (legenda e critérios). Mata-mata: cards (com "Semifinal · Ida"
  na faixa de meta, também na home) + confrontos — empilhado (< 960 px), os confrontos vêm antes
  dos cards (`split--aside-first`): quem avança é o resumo da rodada
  (`createTieCard`); a mensagem `match` redesenha o confronto do jogo (agregado e
  vencedor vêm no `TieOut`). Sem alertas nesta página.

### Operador (`operator.js`)
* `GET /api/auth/me` primeiro (seta o cookie CSRF e acerta o relógio com `server_time`) → login (`POST /api/auth/login`;
  falha de CSRF renova o cookie e tenta uma vez) ou painel; 401 em qualquer chamada
  volta ao login ("Sua sessão expirou").
* Só aparece o que `me.permissions` permite: botões de lance (`post_event`), de status
  (`change_status`), "Cancelar" na linha do tempo (`void_event`), link do admin (`admin_site`).
* Partidas da data (`GET /api/matches?date=`, padrão hoje em Brasília), agrupadas por
  competição; ↑/↓ andam pela lista; `?date=&match=` na URL.
* Botões só de `available` (nada de regra de jogo no front): **lances de jogo primeiro**
  (`#action-grid`) e o **andamento do jogo** (estruturais) num grupo à parte, depois dos
  lances (`#flow-actions`), para ninguém mudar o período por engano. Formulário pelo catálogo:
  `<select>` de tipo limitado a `available.events`; campos por `EventSpec.fields`
  (reaproveitados entre tipos — trocar o tipo mantém o que já foi digitado; "Cancelar", Esc
  ou outro clique no mesmo botão desistem do lance e limpam tudo —, os que não valem ficam
  `hidden` + `disabled`); time em
  rádios com os dois times (opcional ganha "Nenhum"); jogador com `<datalist>` da
  escalação (gol contra → elenco adversário); gol anulado lista os gols válidos e, com o
  gol escolhido, esconde o time (herdado); minuto conforme `minute_mode`, pré-preenchido
  com a sugestão do relógio (atualiza a cada segundo até o operador mexer). **Todo
  estrutural pede confirmação** no envio (`#confirm-dialog` com o placar: "Iniciar a
  partida?", "Encerrar o 1º tempo?", "Iniciar o 2º tempo?", "Iniciar a prorrogação?", "Ir
  para os pênaltis?", "Encerrar a partida?"); "Voltar" não envia nada.
* Envio: validação nativa (`reportValidity`), botão desabilitado até a resposta,
  `X-CSRFToken`, `Idempotency-Key` nova por lançamento (`crypto.randomUUID()`).
  `422 confirmation_required` → `#confirm-dialog` com os avisos; "Confirmar" reenvia com
  `confirm: true` e a MESMA chave. Outro 422/400 → mensagem da regra no formulário (foco no
  campo). Falha de rede → mantém a chave para o reenvio do mesmo conteúdo (o servidor
  devolve o original se o primeiro tinha chegado); mudar qualquer campo gera chave nova.
* Depois de cada envio: desenha a resposta e busca `GET /api/matches/:id` de novo (a
  tela não assina o stream; respostas com `version` menor são ignoradas).
* Linha do tempo (mais recente primeiro) com "Cancelar lançamento" → `#void-dialog`
  (motivo opcional) → `POST …/void`. Status → `#status-dialog` (reagendar com
  `datetime-local` em Brasília, enviado sem fuso) → `POST …/status`. Avisos por toast.

## 7. Desempenho e acessibilidade

* Uma folha de estilo, sprite embutido (zero requisições de ícone), 3 fontes
  pré-carregadas (Barlow 400, Barlow Condensed 700 e Alfa Slab One — sem o preload, a face
  do h1 "Jogos de hoje", o LCP, só era descoberta depois do CSS e trocava ~0,8 s depois numa
  rede lenta), `modulepreload` dos módulos da página, imagens com
  `width/height`, `loading="lazy"` no logo escuro e nos escudos remotos,
  `content-visibility: auto` nas seções de competição, detalhe do jogo desenhado
  só com o acordeão aberto, minuto atualizado sem redesenhar o card.
* **Sem layout shift no carregamento** (CLS medido < 0,1; ~0,00 a 1280 px e no celular):
  `<main>` com `min-height: 100dvh` na home e na competição (o rodapé não aparece na 1ª
  pintura para depois pular), menu com a mesma altura como esqueleto e com links, últimos
  gols visíveis com chip esqueleto, data do dia já no HTML, botão de notificações decidido
  antes da 1ª pintura e esqueleto do título da competição com a altura de uma linha do h1.
  Resta a troca do Barlow 600 (botões) numa janela estreita de computador (~0,085 a 390 px,
  ainda "bom"): pré-carregar mais um peso custaria ~0,1 s de FCP numa rede lenta para todos,
  e no celular não há o que ganhar.
* Foco visível (anel azul + halo amarelo), `prefers-reduced-motion`, impressão
  básica, sem rolagem horizontal a 390 px, cor nunca é o único sinal (status em
  texto, nome da zona, "avança", `aria-label` no placar: "Sport 2 a 1 Náutico").
* Testes: `tests/test_design_frontend.py` (sem banco: ganchos, marca, fontes
  pré-carregadas, contraste AA recalculado dos tokens, `modulepreload` sem cascata, a regra
  inline do botão de notificações igual à de `alerts.js` e, se houver Chromium do
  Playwright, os componentes no navegador — versão atrasada ignorada, detalhe pedido
  de novo, agregado na ordem do placar, link `javascript:` barrado, aviso de correção,
  botão de tema, gol anulado sem "Oxe!" na linha do tempo) e `tests/js/*.test.mjs`
  (`node --test tests/js/*.test.mjs`; o pytest roda também em outro fuso).
* `tests/e2e/test_e2e_browser.py` (só com `E2E=1`: `E2E=1 python -m pytest tests/e2e -o addopts=""`):
  ponta a ponta de verdade — banco próprio com o `seed`, uvicorn (um processo) e Chromium. O
  operador lança um jogo inteiro pela tela (reagendar, início, gols, cartão, aviso com
  confirmação, gol anulado, "Cancelar lançamento", suspender/retomar, intervalo, 2º tempo,
  fim); home e competição abertas antes acompanham gol e anulação sem recarregar (aviso, som,
  `Notification`, últimos gols, classificação); a final vai aos pênaltis com a página aberta;
  rodadas, fases, tema, logo e nome da configuração (também no admin), console limpo, 390 px
  sem rolagem lateral, o stream voltando sozinho depois de reiniciar o servidor, nomes dos
  times sem quebrar no meio da palavra (320–1024 px), CLS < 0,1 no carregamento (1280 e
  390 px) e o admin claro por padrão e no mesmo tema do site. `E2E_BASE_URL` reaproveita um
  servidor já de pé. Capturas das páginas reais em `docs/screenshots/`.
* `tests/test_front_pages.py`: os testes puros das páginas em node (alertas, reconexão,
  minuto sugerido, `api.js`), os ganchos dos templates e as três páginas no Chromium com
  a API simulada por `page.route()` (gol pelo stream com alerta único, correção e
  reconexão com `Last-Event-ID`; rodadas e fases da competição; login, formulário e
  confirmação com a mesma chave no operador; lances antes do andamento do jogo e a
  confirmação dos estruturais; lista de partidas como lista de listas; nomes dos times a
  320–1024 px; logo 10:1 sem empurrar o cabeçalho), as páginas de erro (404/403 com a
  marca em PT-BR, 500 sem contexto), o preload da fonte dos títulos, os últimos gols
  visíveis com esqueleto e focáveis e o nome da marca no admin e no `alt` do logo.
