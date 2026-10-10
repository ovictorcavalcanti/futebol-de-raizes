/**
 * Home (index.html): menu, relógio de Brasília, últimos gols com alertas e uma
 * seção por competição com os jogos do dia à esquerda e a classificação ao vivo à
 * direita (docs/PLANO.md "Blocos da home", docs/FRONTEND.md §4).
 *
 * Ciclo: busca GET /api/competitions + GET /api/home, desenha, assina o stream a
 * partir do `cursor` e substitui o trecho afetado a cada mensagem (sem merge).
 * O seletor ‹ › troca o dia (?date=AAAA-MM-DD na URL; sem ele, hoje).
 */
import { hooks, cloneTemplate, renderCompetitionNav, showToast } from './render.js';
import { ServerClock, mountClock } from './clock.js';
import { formatDateLong, dayKey } from './format.js';
import { createMatchCard, updateMatchCard, tickMatchCards, getCardMatch, createLatestGoal, createTieGroup, tiesFromMatches, syncTieCards, refreshMatchCards, groupMatchesByGroup, createMatchGroup } from './match-card.js';
import { createStandings, updateStandings } from './standings.js';
import { getHome, getCompetitions, getMatch } from './api.js';
import { createStream, liveStatusIndicator } from './stream.js';
import { createGoalAlerts, notificationSupport } from './alerts.js';

const $ = (id) => document.getElementById(id);
const els = {
  nav: document.querySelector('#competitions-nav [data-hook="competition-links"]'),
  clock: $('brasilia-clock'),
  liveStatus: $('live-status'),
  title: $('home-title'),
  date: $('home-date'),
  dayPrev: $('day-prev'),
  dayNext: $('day-next'),
  dayToday: $('day-today'),
  emptyTitle: $('home-empty-title'),
  latest: $('latest-goals'),
  latestList: $('latest-goals-list'),
  latestEmpty: $('latest-goals-empty'),
  alertRegion: $('goal-alert'),
  soundButton: $('toggle-sound'),
  notifyButton: $('toggle-notifications'),
  notifyHint: $('notifications-hint'),
  audio: $('goal-sound'),
  competitions: $('competitions'),
  empty: $('home-empty'),
  error: $('home-error'),
};

const clock = new ServerClock();
const now = () => clock.nowMs();

// O botão de notificações (e o aviso de HTTPS) já no primeiro desenho, antes dos dados: o
// cabeçalho dos "Últimos gols" não muda de altura quando a home chega (sem layout shift).
const notifySupport = notificationSupport(window);
if (els.notifyButton) els.notifyButton.hidden = !notifySupport.available;
if (els.notifyHint) els.notifyHint.hidden = notifySupport.reason !== 'insecure';
const DAY_RE = /^\d{4}-\d\d-\d\d$/;

/** Dia pedido na URL (?date=AAAA-MM-DD) ou null (hoje, acompanha a virada do dia). */
function dayFromUrl() {
  const value = new URLSearchParams(location.search).get('date');
  return value && DAY_RE.test(value) ? value : null;
}

const state = {
  selected: dayFromUrl(), // dia escolhido no seletor; null = hoje
  date: null, // dia da resposta exibida
  competitions: [], // menu
  liveKey: '',
  cards: new Map(), // match id → card
  standings: new Map(), // stage id → { el: <div class="standings">, matches: jogos do dia (fase de grupos) | null }
  latestGoals: [], // últimos gols desenhados (redesenho na troca do formato do minuto)
  reloadTimer: 0,
  reloading: null,
  reloadingFor: null, // dia do reload em andamento
};
let alerts = null;
let stream = null;
let clockMounted = false;

/* --- Dados -------------------------------------------------------------------------- */

function sync(data) {
  if (data?.server_time) clock.sync(data.server_time);
  if (!clockMounted && els.clock && clock.synced) {
    mountClock(els.clock, clock);
    clockMounted = true;
  }
}

function loadDetail(matchId) {
  return getMatch(matchId).then((data) => {
    sync(data);
    const card = state.cards.get(matchId);
    if (card && data?.match) updateMatchCard(card, data.match);
  });
}

/* --- Menu ------------------------------------------------------------------------------ */

function liveSlugs() {
  const slugs = new Set();
  for (const card of state.cards.values()) {
    const match = getCardMatch(card);
    if (match?.status === 'live' && match.competition?.slug) slugs.add(match.competition.slug);
  }
  return slugs;
}

function paintNav(force = false) {
  if (!els.nav) return;
  const slugs = liveSlugs();
  const key = [...slugs].sort().join('|');
  if (!force && key === state.liveKey) return;
  state.liveKey = key;
  renderCompetitionNav(els.nav, state.competitions, { liveSlugs: slugs });
}

/* --- Seletor de dias ------------------------------------------------------------------------ */

const DAY_MS = 86_400_000;
const keyMs = (key) => Date.parse(`${key}T00:00:00Z`);

function addDays(key, n) {
  return new Date(keyMs(key) + n * DAY_MS).toISOString().slice(0, 10);
}

/** Dias entre `key` e hoje em Brasília: 0 hoje, -1 ontem, 1 amanhã. */
function daysFromToday(key) {
  return Math.round((keyMs(key) - keyMs(clock.today())) / DAY_MS);
}

function paintDayLabels(key) {
  const diff = daysFromToday(key);
  const title = { 0: 'Jogos de hoje', [-1]: 'Jogos de ontem', 1: 'Jogos de amanhã' }[diff] ?? 'Jogos do dia';
  const empty =
    { 0: 'Hoje não tem jogo, visse?', [-1]: 'Ontem não teve jogo, visse?', 1: 'Amanhã não tem jogo, visse?' }[diff] ??
    (diff < 0 ? 'Nesse dia não teve jogo, visse?' : 'Nesse dia não tem jogo, visse?');
  if (els.title) els.title.textContent = title;
  if (els.emptyTitle) els.emptyTitle.textContent = empty;
  if (els.dayToday) {
    els.dayToday.hidden = diff === 0;
    // "Hoje" fica do lado do caminho de volta: num dia futuro, junto do ‹ (antes dele);
    // num passado, junto do › (depois dele). Muda no DOM, não só no CSS: a ordem do Tab
    // acompanha a da tela.
    if (diff > 0) els.dayPrev?.before(els.dayToday);
    else if (diff < 0) els.dayNext?.after(els.dayToday);
  }
}

function setDayBusy(busy) {
  for (const button of [els.dayPrev, els.dayNext, els.dayToday]) if (button) button.disabled = busy;
}

/** Troca o dia exibido: hoje sai da URL (e volta a acompanhar a virada do dia). */
function goToDay(key, { push = true } = {}) {
  state.selected = key && key !== clock.today() ? key : null;
  if (push) {
    const url = new URL(location.href);
    if (state.selected) url.searchParams.set('date', state.selected);
    else url.searchParams.delete('date');
    history.pushState(null, '', url);
  }
  setDayBusy(true);
  els.competitions.setAttribute('aria-busy', 'true');
  return reloadAndRestart().finally(() => setDayBusy(false));
}

els.dayPrev?.addEventListener('click', () => state.date && goToDay(addDays(state.date, -1)));
els.dayNext?.addEventListener('click', () => state.date && goToDay(addDays(state.date, 1)));
els.dayToday?.addEventListener('click', () => goToDay(null));
window.addEventListener('popstate', () => goToDay(dayFromUrl(), { push: false }));

/* --- Desenho ------------------------------------------------------------------------------ */

function cardFor(match, next) {
  let card = state.cards.get(match.id);
  if (card) updateMatchCard(card, match);
  else card = createMatchCard(match, { now, onExpand: loadDetail, showRound: !!match.tie }); // mata-mata: "Final · Jogo único"
  next.set(match.id, card);
  return card;
}

/**
 * Fase de grupos: só as tabelas dos grupos com jogo no dia (o grupo da partida e o de cada
 * time, para jogo entre grupos), com as punições desses times. Se nada casar (dado
 * incompleto), mostra todos os grupos.
 */
function dayStandings(standings, matches) {
  const groups = standings?.groups || [];
  if (groups.length < 2) return standings;
  const groupIds = new Set(matches.map((m) => m.group?.id).filter((id) => id != null));
  const teams = new Set(matches.flatMap((m) => [m.home?.id, m.away?.id]).filter((id) => id != null));
  const shown = groups.filter((g) => groupIds.has(g.id) || (g.rows || []).some((r) => teams.has(r.team?.id)));
  if (!shown.length || shown.length === groups.length) return standings;
  const kept = new Set(shown.flatMap((g) => (g.rows || []).map((r) => r.team?.id)));
  return { ...standings, groups: shown, adjustments: (standings.adjustments || []).filter((a) => kept.has(a.team?.id)) };
}

function standingsFor(stage, next) {
  const matches = stage.format === 'groups' ? stage.matches || [] : null;
  const data = matches ? dayStandings(stage.standings, matches) : stage.standings;
  let el = state.standings.get(stage.id)?.el;
  if (el) updateStandings(el, data);
  else el = createStandings(data);
  next.set(stage.id, { el, matches });
  return el;
}

function competitionSection(comp, nextCards, nextStandings) {
  const section = cloneTemplate('tpl-competition-section');
  const p = hooks(section);
  const href = `/competition.html?slug=${encodeURIComponent(comp.slug)}`;
  p['competition-name'].textContent = comp.name;
  p['competition-link'].href = href;
  p['competition-more'].href = href;
  section.dataset.competitionId = String(comp.id);
  const stages = comp.stages || [];
  for (const stage of stages) {
    const block = cloneTemplate('tpl-stage-block');
    const b = hooks(block);
    block.dataset.stageId = String(stage.id);
    b['stage-name'].textContent = stage.name;
    b['stage-name'].hidden = stages.length === 1 && stage.format !== 'knockout';
    if (stage.format === 'knockout') {
      // Mata-mata: um bloco por confronto (sem título); o agregado vem no card da volta.
      const groups = tiesFromMatches(stage.matches || []).map((tie) =>
        createTieGroup(tie, tie.matches.map((m) => cardFor(m, nextCards)), { title: false }));
      b.matches.classList.add('tie-groups');
      b.matches.replaceChildren(...groups);
      b.standings.remove();
      b['stage-grid'].classList.add('split--no-aside');
    } else {
      const matches = stage.matches || [];
      const grouped = stage.format === 'groups';
      b.matches.classList.toggle('match-groups', grouped);
      b.matches.replaceChildren(...(grouped
        ? groupMatchesByGroup(matches).map((g) => createMatchGroup(g.group, g.matches.map((m) => cardFor(m, nextCards))))
        : matches.map((m) => cardFor(m, nextCards))));
      if (stage.standings) b.standings.append(standingsFor(stage, nextStandings));
      else b.standings.remove();
    }
    p.stages.append(block);
  }
  return section;
}

function renderLatestGoals(goals = [], freshIds = null) {
  state.latestGoals = goals;
  els.latestList.replaceChildren(...goals.map((g) => createLatestGoal(g, { isNew: !!freshIds?.has(g.event_id) })));
  els.latestList.hidden = goals.length === 0;
  els.latestList.removeAttribute('aria-busy'); // sai o chip esqueleto do HTML
  els.latestEmpty.hidden = goals.length > 0;
}

function render(home) {
  state.date = home.date;
  if (els.date) {
    // meio-dia UTC = manhã em Brasília: o dia da resposta, sem depender do fuso do aparelho
    els.date.textContent = formatDateLong(`${home.date}T12:00:00Z`);
    els.date.dateTime = home.date;
  }
  paintDayLabels(home.date);
  els.error.hidden = true;
  const competitions = home.competitions || [];
  const nextCards = new Map();
  const nextStandings = new Map();
  const sections = competitions
    .slice()
    .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    .map((comp) => competitionSection(comp, nextCards, nextStandings));
  els.competitions.replaceChildren(...sections);
  els.competitions.removeAttribute('aria-busy');
  state.cards = nextCards;
  state.standings = nextStandings;

  const hasGames = sections.length > 0;
  els.empty.hidden = hasGames;
  els.competitions.hidden = !hasGames;
  els.latest.hidden = !hasGames; // sem jogo no dia, o bloco de últimos gols não aparece
  renderLatestGoals(home.latest_goals || []);
  paintNav(true);
}

function showError() {
  els.competitions.replaceChildren();
  els.competitions.removeAttribute('aria-busy');
  if (!state.date) els.latest.hidden = true; // nunca carregou: sem o esqueleto dos últimos gols
  els.error.hidden = false;
}

/* --- Stream ------------------------------------------------------------------------------- */

function onMatch(message) {
  const match = message?.match;
  if (!match) return;
  const card = state.cards.get(match.id);
  if (card) {
    const before = getCardMatch(card);
    updateMatchCard(card, match, { flash: true }); // pisca no gol; acordeão e aba continuam
    paintNav();
    syncTieCards(state.cards.values(), match); // agregado no card da volta
    // a ordem da fase depende do status e do início; início em outro dia tira o card da página;
    // time ou grupo trocado no admin muda o bloco do card e as tabelas de grupo exibidas
    const placement = (m) => [m.status, m.kickoff_at, m.home?.id, m.away?.id, m.group?.id].join('|');
    if (before && placement(before) !== placement(match)) scheduleReload();
  } else if (state.date && dayKey(match.kickoff_at) === state.date) {
    scheduleReload(); // jogo novo no dia (ex.: reagendado para hoje): busca a home de novo
  }
}

function onStandings(message) {
  const entry = state.standings.get(message?.stage_id);
  // a mensagem traz todos os grupos: na home, continuam só os com jogo no dia
  if (entry && message.standings) updateStandings(entry.el, entry.matches ? dayStandings(message.standings, entry.matches) : message.standings);
}

function onGoals(message) {
  if (!message) return;
  if (message.date && state.date && message.date !== state.date) {
    clock.checkDay(); // a lista é de outro dia: a virada busca a home de novo
    return;
  }
  const decisions = alerts ? alerts.handle(message) : [];
  const fresh = new Set(decisions.filter((d) => d.action === 'alert').map((d) => d.goal.event_id));
  renderLatestGoals(message.latest_goals || [], fresh);
}

/* --- Ciclo --------------------------------------------------------------------------------- */

/** Busca a home de novo e redesenha (sem alertas); devolve o cursor novo. */
function reload() {
  const wanted = state.selected;
  if (state.reloading && state.reloadingFor === wanted) return state.reloading;
  state.reloadingFor = wanted;
  const request = getHome(wanted ?? undefined)
    .then((home) => {
      if (state.selected !== wanted) return reload(); // o dia mudou no meio do caminho
      sync(home);
      render(home);
      alerts?.reset(home.latest_goals || []);
      return home.cursor ?? null;
    })
    .finally(() => {
      if (state.reloading === request) state.reloading = null;
    });
  state.reloading = request;
  return request;
}

function reloadAndRestart() {
  return reload()
    .then((cursor) => stream?.restart(cursor))
    .catch(() => {
      showToast('Não deu certo agora. Tente de novo em instantes.', { kind: 'error' });
      scheduleReload(30_000);
    });
}

function scheduleReload(delay = 2_000) {
  clearTimeout(state.reloadTimer);
  state.reloadTimer = setTimeout(reloadAndRestart, delay);
}

async function boot() {
  els.error.hidden = true;
  const [compsResult, homeResult] = await Promise.allSettled([getCompetitions(), getHome(state.selected ?? undefined)]);
  if (compsResult.status === 'fulfilled') state.competitions = compsResult.value?.competitions || [];
  if (homeResult.status !== 'fulfilled') {
    paintNav(true);
    showError();
    return;
  }
  const home = homeResult.value;
  sync(home);
  render(home);

  alerts = createGoalAlerts({
    region: els.alertRegion,
    soundButton: els.soundButton,
    notifyButton: els.notifyButton,
    audio: els.audio,
    hint: els.notifyHint,
    serverNow: now,
    initialGoals: home.latest_goals || [],
    isRelevant: (goal) => state.cards.has(goal.match_id ?? goal.match?.id), // só jogos que estão na home
    iconUrl: document.querySelector('link[rel="icon"]')?.href || '',
    support: notifySupport,
  });

  stream = createStream({
    clock,
    handlers: { match: onMatch, standings: onStandings, goals: onGoals },
    onStatus: liveStatusIndicator(els.liveStatus),
    onStale: reload, // 5 min sem stream: estado inteiro de novo, sem pedir reenvio
  });
  stream.start(home.cursor ?? null);

  clock.onTick((ms) => tickMatchCards(els.competitions, ms));
  clock.onDayChange(() => reloadAndRestart()); // virada do dia em Brasília
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') clock.checkDay();
  });
}

// Formato do minuto trocado no cabeçalho: redesenha cards e últimos gols com o que já há.
document.addEventListener('minuteformatchange', () => {
  refreshMatchCards(els.competitions);
  if (state.date) renderLatestGoals(state.latestGoals);
});

els.error?.querySelector('[data-hook="home-retry"]')?.addEventListener('click', () => {
  if (stream) reloadAndRestart();
  else boot();
});

boot();
