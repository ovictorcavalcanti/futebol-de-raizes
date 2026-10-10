/**
 * Tela do operador (operator.html) — fase 7: "Um operador lança um jogo inteiro
 * pela tela, sem chamar a API à mão" (docs/PLANO.md, docs/FRONTEND.md §4).
 *
 * * GET /api/auth/me primeiro (seta o cookie CSRF); login/logout por sessão.
 * * Mostra só o que me.permissions permite (lançar, cancelar, mudar status).
 * * Escolha da partida por data (Brasília), agrupada por competição.
 * * Botões a partir de `available` (GET /api/matches/:id) — nenhuma regra de jogo
 *   aqui —: lances de jogo primeiro; o andamento do jogo (estruturais: início, fim de
 *   tempo, pênaltis, fim) num grupo à parte que sempre pede confirmação no envio;
 *   formulário montado pelo catálogo (GET /api/ops/catalog), minuto
 *   sugerido pelo relógio do jogo, confirmação de avisos (422 confirmation_required)
 *   com a MESMA chave de idempotência, linha do tempo com "cancelar lançamento" e
 *   ações de status com <dialog>.
 * * Não assina o stream: busca a partida de novo depois de cada envio.
 *
 * As partes puras (minuto sugerido, corpo do lançamento, data em Brasília, grupos de
 * botões e texto da confirmação do andamento) são
 * exportadas e testadas com node --test; a tela só liga quando há #op-app.
 */
import { h, hooks, cloneTemplate, showToast } from './render.js';
import { eventIconName, icon } from './icons.js';
import { createCrest } from './crest.js';
import { ServerClock, mountClock, liveMinute } from './clock.js';
import { formatTime, formatWhen, dayKey, TIME_ZONE } from './format.js';
import { createMatchCard, updateMatchCard, tickMatchCards, sortEventsByClock, GOAL_TO_CONFIRM } from './match-card.js';
import * as api from './api.js';

/* ==========================================================================
   Partes puras
   ========================================================================== */

/** Períodos com relógio correndo (o minuto é obrigatório nos lances de jogo). */
export const CLOCK_PERIODS = new Set(['first_half', 'second_half', 'extra_time', 'extra_second_half']);

/** Minutos aceitos ao acertar o relógio (igual a domain.clock_set_range): 0–45, 46–90, 91–105, 106–120. */
const CLOCK_SET_RANGES = { first_half: [0, 45], second_half: [46, 90], extra_time: [91, 105], extra_second_half: [106, 120] };
export function clockSetRange(period) {
  return CLOCK_SET_RANGES[period] || null;
}
/** Eventos estruturais que fecham um período (aceitam acréscimo no minuto final). */
const CLOSING_EVENTS = new Set(['half_time', 'extra_half_time', 'match_end']);
const POSITION_SHORT = { GK: 'GOL', LAD: 'LAD', DF: 'ZAG', LAE: 'LAE', VOL: 'VOL', MF: 'MEI', FW: 'ATA' };

/**
 * Exigência do minuto agora (espelha domain.minute_mode): lance de jogo com minuto
 * "required" vale com o relógio correndo; no intervalo e nos pênaltis é opcional.
 * @returns {'required'|'optional'|'none'}
 */
export function effectiveMinuteMode(spec, period) {
  if (!spec) return 'none';
  const mode = spec.minute || 'required';
  if (spec.kind === 'game' && mode === 'required' && !CLOCK_PERIODS.has(period)) return 'optional';
  return mode;
}

/**
 * Minuto SUGERIDO para o formulário, a partir de period_started_at e do relógio do
 * servidor (mesma conta do minuto ao vivo do card). O operador pode mudar.
 * * lance de jogo com relógio correndo → minuto corrente ("45+2" vira {45, 2});
 * * fim do 1º tempo / fim de jogo com relógio correndo → minuto final do período,
 *   com o acréscimo já jogado;
 * * demais casos (intervalo, pênaltis, início de período, sem relógio) → null
 *   (em branco: o back usa o minuto padrão do lance, quando houver).
 * @returns {{minute: number, stoppage: number|null}|null}
 */
export function suggestMinute(match, spec, nowMs) {
  if (!match || !spec || effectiveMinuteMode(spec, match.period) === 'none') return null;
  const live = liveMinute(match, nowMs);
  if (!live) return null;
  if (spec.kind === 'structural') {
    if (!CLOSING_EVENTS.has(spec.type) || !match.clock) return null;
    return { minute: match.clock.regular_end, stoppage: live.minute >= match.clock.regular_end ? live.stoppage ?? null : null };
  }
  if (spec.kind !== 'game') return null;
  return { minute: live.minute, stoppage: live.stoppage ?? null };
}

/** Coloca um valor no corpo do lançamento: "payload.x" → body.payload.x; senão body[name]. */
export function assignField(body, name, value) {
  if (name.startsWith('payload.')) {
    body.payload = body.payload || {};
    body.payload[name.slice('payload.'.length)] = value;
  } else {
    body[name] = value;
  }
  return body;
}

/**
 * Corpo de POST /api/ops/matches/:id/events (CONTRACT §4).
 * @param {string} type
 * @param {Array<[string, any]>} entries [nome do campo do catálogo, valor]; null/'' ficam de fora (false fica)
 * @param {{minute?: number|null, stoppage?: number|null}} [minute]
 */
export function buildEventBody(type, entries = [], { minute = null, stoppage = null } = {}) {
  const body = { type };
  if (Number.isInteger(minute)) {
    body.minute = minute;
    if (Number.isInteger(stoppage) && stoppage > 0) body.stoppage = stoppage;
  }
  for (const [name, value] of entries) {
    if (value == null || value === '' || Number.isNaN(value)) continue;
    assignField(body, name, value);
  }
  return body;
}

const F_LOCAL = new Intl.DateTimeFormat('en-CA', {
  timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

/** Instante → valor de <input type="datetime-local"> no horário de Brasília ("2026-10-03T16:30"). */
export function toBrasiliaInput(value) {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  const parts = Object.fromEntries(F_LOCAL.formatToParts(d).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

/**
 * Separa os tipos de `available.events` em lances de jogo (gol, cartões, substituição…)
 * e andamento do jogo (estruturais: início, fim de tempo, prorrogação, pênaltis, fim),
 * mantendo a ordem da API em cada grupo. Os lances vêm primeiro na tela; o andamento
 * fica num grupo à parte para ninguém trocar o período por engano.
 * @param {string[]} types
 * @param {(type: string) => {kind?: string}} specOf
 * @returns {{game: string[], flow: string[]}}
 */
export function splitActions(types = [], specOf = () => ({})) {
  const game = [];
  const flow = [];
  for (const type of types) (specOf(type)?.kind === 'structural' ? flow : game).push(type);
  return { game, flow };
}

/**
 * Texto do #confirm-dialog antes de lançar um estrutural (todo o andamento do jogo pede
 * confirmação: mudar o período mexe no relógio, na tabela e no que o torcedor vê).
 * @param {string} type
 * @param {object} match  partida atual (home/away/home_score/away_score)
 * @param {string} [label] rótulo do catálogo, para um tipo novo sem texto próprio
 * @returns {{title: string, text: string, ok: string}}
 */
export function structuralConfirm(type, match, label = '') {
  const name = (team, fallback) => team?.name || team?.short_name || fallback;
  const home = name(match?.home, 'Mandante');
  const away = name(match?.away, 'Visitante');
  const score = `${home} ${match?.home_score ?? 0} × ${match?.away_score ?? 0} ${away}`;
  switch (type) {
    case 'match_start':
      return { title: 'Iniciar a partida?', text: `${home} × ${away}. O relógio do jogo começa a contar.`, ok: 'Iniciar' };
    case 'half_time':
      return { title: 'Encerrar o 1º tempo?', text: `${score}. O jogo vai para o intervalo.`, ok: 'Encerrar 1º tempo' };
    case 'second_half_start':
      return { title: 'Iniciar o 2º tempo?', text: `${score}. O relógio do jogo volta a contar.`, ok: 'Iniciar 2º tempo' };
    case 'extra_time_start':
      return { title: 'Iniciar a prorrogação?', text: `${score}. O relógio do jogo volta a contar.`, ok: 'Iniciar prorrogação' };
    case 'extra_half_time':
      return { title: 'Encerrar o 1º tempo da prorrogação?', text: `${score}. O jogo vai para o intervalo da prorrogação.`, ok: 'Encerrar 1º tempo' };
    case 'extra_second_half_start':
      return { title: 'Iniciar o 2º tempo da prorrogação?', text: `${score}. O relógio do jogo volta a contar.`, ok: 'Iniciar 2º tempo' };
    case 'penalties_start':
      return { title: 'Ir para os pênaltis?', text: `${score}. Começa a disputa de pênaltis.`, ok: 'Iniciar pênaltis' };
    case 'match_end':
      return { title: 'Encerrar a partida?', text: `${score}. A tabela oficial é recalculada.`, ok: 'Encerrar' };
    default:
      return { title: `${label || 'Mudar o andamento do jogo'}?`, text: `${score}. Isto muda o período da partida.`, ok: 'Confirmar' };
  }
}

/** Aviso da correção de um lance. `voided`: ids que caíram junto (na correção, só o
 * vermelho automático cujo amarelo deixou de ser o 2º). */
export function editedToast(summary, voided = []) {
  const n = voided?.length || 0;
  return `Corrigido: ${summary}.${n > 0 ? ` ${n === 1 ? 'O vermelho automático caiu' : `${n} vermelhos automáticos caíram`} junto.` : ''}`;
}

const LIVE_GROUP = new Set(['live', 'delayed', 'suspended']);
const UPCOMING_GROUP = new Set(['scheduled', 'postponed']);

/** Mesma ordem da página Jogos do admin: ao vivo > agendado > encerrado
 * (encerrados do mais recente para o mais antigo). */
export function sortByStatus(matches = []) {
  const rank = (m) => (LIVE_GROUP.has(m.status) ? 0 : UPCOMING_GROUP.has(m.status) ? 1 : 2);
  const time = (m) => Date.parse(m.kickoff_at || '') || 0;
  return matches.slice().sort((a, b) => rank(a) - rank(b) || (rank(a) === 2 ? time(b) - time(a) : time(a) - time(b)) || a.id - b.id);
}

/** Partidas agrupadas por competição (na ordem da API), cada grupo ordenado por status. */
export function groupByCompetition(matches = []) {
  const groups = new Map();
  for (const match of matches) {
    const key = match.competition?.id ?? 0;
    if (!groups.has(key)) groups.set(key, { competition: match.competition || { name: 'Outras' }, matches: [] });
    groups.get(key).matches.push(match);
  }
  return [...groups.values()].map((group) => ({ ...group, matches: sortByStatus(group.matches) }));
}

/** Jogadores da escalação para o <datalist> (time escolhido; gol contra → adversário). */
export function lineupPlayers(match, teamId = null, { opponent = false } = {}) {
  const lineups = match?.lineups || {};
  let sides = ['home', 'away'];
  if (teamId != null) {
    const side = match?.home?.id === teamId ? 'home' : match?.away?.id === teamId ? 'away' : null;
    if (side) sides = [opponent ? (side === 'home' ? 'away' : 'home') : side];
  }
  const seen = new Set();
  const players = [];
  for (const side of sides) {
    const lineup = lineups[side];
    for (const p of [...(lineup?.starters || []), ...(lineup?.substitutes || [])]) {
      if (!p?.name || seen.has(p.name)) continue;
      seen.add(p.name);
      players.push(p);
    }
  }
  return players;
}

/* ==========================================================================
   Tela (só no navegador)
   ========================================================================== */

let els = null;
const clock = new ServerClock();
const now = () => clock.nowMs();
const STATUS_ICON = { postpone: 'calendar', suspend: 'pause', resume: 'play', reschedule: 'calendar', cancel: 'x-circle' };
const ERROR_FIELD = {
  invalid_minute: 'minute',
  team_required: 'team_id',
  team_not_in_match: 'team_id',
  player_required: 'payload.player',
  annul_target_invalid: 'annuls_event_id',
};

const state = {
  me: null,
  catalog: null,
  specs: new Map(), // type → EventSpecOut
  date: '',
  matches: [],
  matchId: null,
  match: null,
  available: { events: [], status: [] },
  card: null,
  formType: null,
  formKey: null, // chave de idempotência do conteúdo atual do formulário
  controls: new Map(), // campo do catálogo → controle (reaproveitado entre tipos)
  minute: null, // controle do minuto
  sending: false,
  clockMounted: false,
};

let uidSeq = 0;
const uid = (prefix) => `op-${prefix}-${++uidSeq}`;
const can = (permission) => !!state.me?.user?.permissions?.[permission];
const teamShort = (team) => team?.short_name || team?.name || '';
const teamName = (team) => team?.name || team?.short_name || '';

function sync(data) {
  if (data?.server_time) clock.sync(data.server_time);
  if (!state.clockMounted && els.clock && clock.synced) {
    mountClock(els.clock, clock);
    state.clockMounted = true;
  }
}

function genericError(error) {
  if (error?.isNetwork) return 'Sem resposta do servidor. Confira a conexão e tente de novo.';
  return error?.message || api.GENERIC_ERROR;
}

/* --- Sessão --------------------------------------------------------------------------- */

function showLogin(message = '') {
  els.boot.hidden = true;
  els.app.hidden = true;
  els.login.hidden = false;
  state.me = null;
  closeForm({ restoreFocus: false });
  if (message) {
    els.loginErrorText.textContent = message;
    els.loginError.hidden = false;
  } else {
    els.loginError.hidden = true;
  }
  els.loginForm.elements.password.value = '';
  (els.loginForm.elements.username.value ? els.loginForm.elements.password : els.loginForm.elements.username).focus();
}

function sessionExpired() {
  showLogin('Sua sessão expirou. Entre de novo.');
}

async function onLogin(event) {
  event.preventDefault();
  const form = els.loginForm;
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  els.loginError.hidden = true;
  els.loginSubmit.disabled = true;
  els.loginSubmit.setAttribute('aria-busy', 'true');
  try {
    let result;
    try {
      result = await api.login(username, password);
    } catch (error) {
      if (error?.code !== 'csrf_failed') throw error;
      await api.getMe(); // cookie CSRF vencido: renova e tenta uma vez
      result = await api.login(username, password);
    }
    form.elements.password.value = '';
    await showApp({ authenticated: true, user: result.user });
  } catch (error) {
    els.loginErrorText.textContent = error?.status === 401 ? 'Usuário ou senha incorretos.' : genericError(error);
    els.loginError.hidden = false;
    form.elements.password.focus();
    form.elements.password.select();
  } finally {
    els.loginSubmit.disabled = false;
    els.loginSubmit.removeAttribute('aria-busy');
  }
}

async function onLogout() {
  els.logout.disabled = true;
  try {
    await api.logout();
  } catch {
    /* sessão já encerrada: segue para o login */
  } finally {
    els.logout.disabled = false;
  }
  resetPanel();
  els.loginForm.elements.username.value = '';
  showLogin();
  api.getMe().catch(() => {}); // cookie CSRF novo para o próximo login
}

async function showApp(me) {
  state.me = me;
  const user = me.user || {};
  els.boot.hidden = true;
  els.login.hidden = true;
  els.app.hidden = false;
  els.userName.textContent = user.name || user.username || '';
  els.userRoles.textContent = (user.roles || []).join(', ');
  els.userRoles.hidden = !(user.roles || []).length;
  els.adminLink.hidden = !can('admin_site');
  els.actionsCard.hidden = !can('post_event') && !can('change_status');
  els.actionsBlock.hidden = !can('post_event');
  els.statusBlock.hidden = !can('change_status');

  const catalogLoad = state.catalog ? Promise.resolve() : loadCatalog();
  await Promise.all([catalogLoad, loadPicker()]);
  const wanted = Number(new URLSearchParams(window.location.search).get('match'));
  if (Number.isInteger(wanted) && wanted > 0 && wanted !== state.matchId) selectMatch(wanted);
}

async function loadCatalog() {
  try {
    const catalog = await api.getCatalog();
    state.catalog = catalog;
    state.specs = new Map((catalog?.events || []).map((spec) => [spec.type, spec]));
    if (state.match) paintActions();
  } catch (error) {
    if (error?.status === 401) return sessionExpired();
    showToast('Não deu para carregar os tipos de lance. Tente de novo.', {
      kind: 'error',
      timeout: 0,
      action: { label: 'Tentar de novo', onClick: loadCatalog },
    });
  }
}

/* --- Escolha da partida ------------------------------------------------------------------ */

function writeUrl() {
  const search = new URLSearchParams(window.location.search);
  if (state.date) search.set('date', state.date);
  if (state.matchId) search.set('match', String(state.matchId));
  else search.delete('match');
  window.history.replaceState(null, '', `${window.location.pathname}?${search}`);
}

function pickPill(match) {
  let cls = 'scheduled';
  let text = match.status_label || match.status;
  if (match.status === 'live') {
    const interval = match.period === 'half_time' || match.period === 'extra_half_time';
    cls = interval ? 'interval' : 'live';
    if (interval) text = 'Intervalo';
  } else if (['finished', 'postponed', 'suspended', 'cancelled', 'delayed'].includes(match.status)) {
    cls = match.status;
  }
  return h('span', { class: `pill pill--sm pill--${cls}`, text });
}

function pickItem(match) {
  const li = cloneTemplate('tpl-pick-item');
  const p = hooks(li);
  li.dataset.matchId = String(match.id);
  p['home-crest'].replaceWith(createCrest(match.home, { size: 20 }));
  p['away-crest'].replaceWith(createCrest(match.away, { size: 20 }));
  p.home.textContent = teamShort(match.home);
  p.away.textContent = teamShort(match.away);
  const started = !['scheduled', 'postponed', 'cancelled'].includes(match.status);
  p.score.textContent = started ? `${match.home_score ?? 0}–${match.away_score ?? 0}` : '×';
  p.status.replaceWith(pickPill(match));
  p.time.textContent = formatTime(match.kickoff_at);
  p.time.dateTime = match.kickoff_at || '';
  p.competition.textContent = [match.round?.name, match.venue].filter(Boolean).join(' · ');
  p.pick.setAttribute('aria-label', `${teamName(match.home)} × ${teamName(match.away)}, ${match.status_label || ''}, ${formatTime(match.kickoff_at)}`);
  if (match.id === state.matchId) p.pick.setAttribute('aria-current', 'true');
  p.pick.addEventListener('click', () => {
    selectMatch(match.id);
    // celular/tablet: a lista fica acima do painel; leva o operador até o placar
    if (window.matchMedia?.('(max-width: 959px)').matches) {
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      els.panel.scrollIntoView({ block: 'start', behavior: reduce ? 'auto' : 'smooth' });
    }
  });
  return li;
}

function renderPicker() {
  // lista de listas: <li> da competição (título) › <ul> com as partidas — semântica de lista válida
  const items = groupByCompetition(state.matches).map((group, index) => {
    const titleId = uid(`picker-group-${group.competition?.id ?? index}`);
    return h('li', { class: 'match-picker__group' },
      h('h3', { class: 'match-picker__group-title', id: titleId, text: group.competition?.name || '' }),
      h('ul', { class: 'match-picker__sublist', 'aria-labelledby': titleId }, ...group.matches.map(pickItem)));
  });
  els.pickerList.replaceChildren(...items);
  els.pickerList.removeAttribute('aria-busy');
  els.pickerEmpty.hidden = state.matches.length > 0;
}

async function loadPicker() {
  els.pickerList.setAttribute('aria-busy', 'true');
  const date = state.date;
  try {
    const data = await api.listMatches({ date });
    if (date !== state.date) return;
    sync(data);
    state.matches = data?.matches || [];
    renderPicker();
  } catch (error) {
    els.pickerList.removeAttribute('aria-busy');
    if (error?.status === 401) return sessionExpired();
    showToast(genericError(error), { kind: 'error' });
  }
}

function updatePickItem(match) {
  const index = state.matches.findIndex((m) => m.id === match.id);
  if (index < 0) return;
  state.matches[index] = match;
  const old = els.pickerList.querySelector(`li[data-match-id="${match.id}"]`);
  if (old) old.replaceWith(pickItem(match));
}

function markPicked() {
  for (const button of els.pickerList.querySelectorAll('.pick-item')) {
    const li = button.closest('li');
    if (Number(li?.dataset.matchId) === state.matchId) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }
}

/* --- Partida -------------------------------------------------------------------------------- */

function resetPanel() {
  closeForm({ restoreFocus: false });
  state.matchId = null;
  state.match = null;
  state.card = null;
  state.controls = new Map();
  state.minute = null;
  els.scoreboard.replaceChildren();
  els.scoreboard.hidden = true;
  els.work.hidden = true;
  els.placeholder.hidden = false;
}

async function selectMatch(matchId) {
  if (state.matchId !== matchId) {
    resetPanel();
    state.matchId = matchId;
    markPicked();
    writeUrl();
  }
  els.panel.setAttribute('aria-busy', 'true');
  try {
    const data = await api.getMatch(matchId);
    if (state.matchId !== matchId) return;
    sync(data);
    applyMatch(data.match, data.available);
  } catch (error) {
    if (state.matchId !== matchId) return;
    if (error?.status === 401) return sessionExpired();
    showToast(error?.status === 404 ? 'Partida não encontrada.' : genericError(error), { kind: 'error' });
  } finally {
    els.panel.removeAttribute('aria-busy');
  }
}

/** Busca a partida de novo (depois de cada envio: a tela não assina o stream). */
function refreshMatch() {
  const matchId = state.matchId;
  if (!matchId) return Promise.resolve();
  return api.getMatch(matchId).then((data) => {
    if (state.matchId !== matchId) return;
    sync(data);
    applyMatch(data.match, data.available);
  }).catch((error) => {
    if (error?.status === 401) sessionExpired();
  });
}

function applyMatch(match, available) {
  if (!match || match.id !== state.matchId) return;
  // resposta atrasada (versão menor que a desenhada) não desfaz o estado mais novo
  if (state.match && typeof match.version === 'number' && typeof state.match.version === 'number' && match.version < state.match.version) return;
  state.match = match;
  if (available) state.available = { events: available.events || [], status: available.status || [] };

  if (state.card) updateMatchCard(state.card, match, { flash: true });
  else {
    state.card = createMatchCard(match, { now, details: false, showRound: true, showCompetition: true });
    els.scoreboard.replaceChildren(state.card);
  }
  els.placeholder.hidden = true;
  els.scoreboard.hidden = false;
  paintTools(match);
  els.work.hidden = false;

  paintActions();
  paintTimeline();
  updatePickItem(match);

  if (state.formType && !state.editing) {
    if (!state.available.events.includes(state.formType)) {
      closeForm({ restoreFocus: false });
      showToast('Este lance não está mais disponível para a partida.', { kind: 'warn' });
    } else {
      refreshFormOptions();
    }
  }
}

/**
 * Atalhos da partida: "Informações parciais" (quem muda o status) e, para quem entra no
 * Django Admin, escalação de cada time e público e renda (nova aba).
 */
function paintTools(match) {
  if (!els.tools) return;
  const items = [];
  if (can('change_status')) items.push(partialInfoSwitch(match));
  if (can('admin_site')) {
    const link = (href, iconName, text) => h('a', { class: 'btn btn--secondary btn--sm', href, target: '_blank', rel: 'noopener' },
      icon(iconName), h('span', { text }));
    items.push(
      link(`/admin/escalacao/${match.id}/home/`, 'shirt', `Escalação ${teamShort(match.home)}`),
      link(`/admin/escalacao/${match.id}/away/`, 'shirt', `Escalação ${teamShort(match.away)}`),
      link(`/admin/matches/match/${match.id}/change/`, 'people', 'Público e renda'),
    );
  }
  els.tools.replaceChildren(...items);
  els.tools.hidden = items.length === 0;
}

/** Liga/desliga "Informações parciais": o jogo sai sem relógio e com o selo no lugar de "Ao vivo". */
function partialInfoSwitch(match) {
  const input = h('input', { type: 'checkbox', id: 'op-partial-info' });
  input.checked = !!match.partial_info;
  input.addEventListener('change', async () => {
    const matchId = match.id;
    const wanted = input.checked;
    input.disabled = true;
    try {
      const result = await api.setPartialInfo(matchId, wanted);
      if (state.matchId !== matchId) return;
      showToast(wanted ? 'Jogo com informações parciais: sem relógio na home.' : 'Informações completas: o relógio volta a aparecer.', { kind: 'ok' });
      applyMatch(result?.match, result?.available);
    } catch (error) {
      input.checked = !wanted;
      input.disabled = false;
      if (error?.status === 401) return sessionExpired();
      showToast(error?.message || 'Não deu para mudar agora. Tente de novo.', { kind: 'error' });
    }
  });
  return h('label', { class: 'switch op-tools__switch' }, input, h('span', { text: 'Informações parciais' }));
}

/* --- Botões de lance e de status ----------------------------------------------------------- */

function specFor(type) {
  return state.specs.get(type) || { type, label: type, kind: 'game', icon: 'info', minute: 'optional', fields: [] };
}

/** Botões de lance dos dois grupos (lances de jogo e andamento do jogo). */
function actionButtons() {
  return [...els.actionGrid.querySelectorAll('.action-btn'), ...els.flowActions.querySelectorAll('.action-btn')];
}

function findActionButton(type) {
  return actionButtons().find((btn) => btn.dataset.type === type) || null;
}

function paintActions() {
  // os botões são redesenhados: o foco volta para o botão equivalente
  const focused = document.activeElement;
  const focusType = els.actionsBlock.contains(focused) ? focused.dataset.type : null;
  const focusAction = els.statusActions.contains(focused) ? focused.dataset.action : null;
  paintButtons();
  if (focusType) findActionButton(focusType)?.focus();
  if (focusAction) {
    const btn = els.statusActions.querySelector(`[data-action="${focusAction}"]`);
    if (btn && !btn.disabled) btn.focus();
  }
}

function actionButton(type) {
  const spec = specFor(type);
  const btn = cloneTemplate('tpl-action-button');
  const p = hooks(btn);
  p['action-icon'].setAttribute('href', `#i-${spec.icon || 'info'}`);
  p['action-label'].textContent = spec.label;
  btn.dataset.type = type;
  btn.classList.toggle('action-btn--goal', type === 'goal');
  btn.classList.toggle('action-btn--structural', spec.kind === 'structural');
  btn.setAttribute('aria-pressed', String(state.formType === type));
  btn.addEventListener('click', () => (state.formType === type ? cancelForm() : openForm(type)));
  return btn;
}

function paintButtons() {
  if (can('post_event')) {
    // tudo vem de `available`: lances de jogo primeiro; estruturais no grupo "Andamento do jogo"
    const { game, flow } = splitActions(state.available.events || [], specFor);
    const empty = flow.length ? 'Nenhum lance de jogo neste momento.' : 'Nenhum lance disponível neste momento.';
    els.actionGrid.replaceChildren(...(game.length ? game.map(actionButton) : [h('p', { class: 'match__empty', text: empty })]));
    els.flowActions.replaceChildren(...flow.map(actionButton));
    els.flowBlock.hidden = flow.length === 0;
  }
  if (can('change_status')) {
    const allowed = new Set(state.available.status || []);
    els.statusActions.replaceChildren(...(state.catalog?.status_actions || []).map((item) => {
      const btn = cloneTemplate('tpl-status-button');
      const p = hooks(btn);
      p['status-icon'].setAttribute('href', `#i-${STATUS_ICON[item.action] || 'info'}`);
      p['status-label'].textContent = item.label;
      btn.dataset.action = item.action;
      if (item.action === 'cancel') btn.classList.replace('btn--secondary', 'btn--danger');
      btn.disabled = !allowed.has(item.action);
      btn.addEventListener('click', () => openStatusDialog(item));
      return btn;
    }));
  }
}

/* --- Formulário do lance ------------------------------------------------------------------------ */

function enableControl(control, on) {
  control.el.hidden = !on;
  if (control.enable) control.enable(on);
  else for (const input of control.inputs) input.disabled = !on;
}

function teamControl() {
  const el = cloneTemplate('tpl-field-team');
  const p = hooks(el);
  const name = uid('team');
  const home = p['input-home'];
  const away = p['input-away'];
  home.name = away.name = name;
  const none = h('input', { type: 'radio', name, value: '' });
  const noneLabel = h('label', { class: 'segmented__option' }, none, h('span', { class: 'segmented__label', text: 'Nenhum' }));
  p.options.append(noneLabel);
  let required = true;
  const control = {
    el,
    inputs: [home, away, none],
    configure(field, match) {
      p.label.textContent = field.label;
      required = !!field.required;
      for (const [side, input] of [['home', home], ['away', away]]) {
        const team = match[side];
        const value = String(team?.id ?? '');
        if (input.value !== value) {
          input.value = value;
          input.checked = false;
        }
        p[`crest-${side}`].replaceChildren(createCrest(team, { size: 26 }));
        p[`name-${side}`].textContent = teamName(team);
      }
      home.required = required;
      noneLabel.hidden = required;
      if (required && none.checked) none.checked = false;
      if (!required && control.read() == null) none.checked = true;
    },
    enable(on) {
      home.disabled = away.disabled = !on;
      none.disabled = !on || required;
    },
    read() {
      const checked = [home, away].find((input) => input.checked);
      return checked && checked.value ? Number(checked.value) : null;
    },
    reset() {
      home.checked = away.checked = false;
      none.checked = !required;
    },
    write(value) {
      home.checked = value != null && home.value === String(value);
      away.checked = value != null && away.value === String(value);
      none.checked = value == null && !required;
    },
    focusTarget: () => [home, away, none].find((input) => input.checked && !input.disabled) || home,
  };
  return control;
}

function playerControl() {
  const el = cloneTemplate('tpl-field-player');
  const p = hooks(el);
  const id = uid('player');
  const listId = uid('players');
  p.input.id = id;
  p.label.htmlFor = id;
  p.datalist.id = listId;
  return {
    el,
    inputs: [p.input],
    configure(field) {
      p.label.textContent = field.label;
      p.input.required = !!field.required;
      p.input.name = field.name;
    },
    setPlayers(players) {
      if (!players.length) {
        p.input.removeAttribute('list');
        p.datalist.replaceChildren();
        return;
      }
      p.input.setAttribute('list', listId);
      p.datalist.replaceChildren(...players.map((pl) => h('option', {
        value: pl.name,
        label: [pl.number, POSITION_SHORT[pl.position] || pl.position].filter((x) => x != null && x !== '').join(' · ') || null,
      })));
    },
    read: () => p.input.value.trim() || null,
    reset() {
      p.input.value = '';
    },
    write(value) {
      p.input.value = value == null ? '' : String(value);
    },
    focusTarget: () => p.input,
  };
}

function inputControl(templateId, { parse = (v) => v.trim() || null, setup = null } = {}) {
  const el = cloneTemplate(templateId);
  const p = hooks(el);
  const id = uid('field');
  p.input.id = id;
  p.label.htmlFor = id;
  return {
    el,
    inputs: [p.input],
    configure(field) {
      p.label.textContent = field.label;
      p.input.required = !!field.required;
      p.input.name = field.name;
      setup?.(p.input, field);
    },
    read: () => parse(p.input.value),
    reset() {
      p.input.value = '';
    },
    write(value) {
      p.input.value = value == null ? '' : String(value);
    },
    focusTarget: () => p.input,
  };
}

function choiceControl() {
  const control = inputControl('tpl-field-choice', {
    setup(select, field) {
      const previous = select.value;
      const options = (field.choices || []).map(([value, label]) => h('option', { value, text: label }));
      if (!field.required) options.unshift(h('option', { value: '', text: '— Não informado —' }));
      select.replaceChildren(...options);
      if ([...select.options].some((o) => o.value === previous)) select.value = previous;
    },
  });
  const baseReset = control.reset;
  control.reset = () => {
    baseReset();
    const select = control.inputs[0];
    select.selectedIndex = 0;
  };
  return control;
}

function boolControl() {
  const el = cloneTemplate('tpl-field-bool');
  const p = hooks(el);
  return {
    el,
    inputs: [p.input],
    configure(field) {
      p.label.textContent = field.label;
      p.input.name = field.name;
      // "obrigatório" no catálogo = a chave vai sempre (true/false); nunca `required` no checkbox
    },
    read: () => p.input.checked,
    reset() {
      p.input.checked = false;
    },
    write(value) {
      p.input.checked = value === true || value === 'true';
    },
    focusTarget: () => p.input,
  };
}

function goalOptionText(event, match) {
  const team = event.team_side ? match[event.team_side] : null;
  const score = event.score_after ? `${event.score_after.home} × ${event.score_after.away}` : '';
  return [event.minute_label, `${event.player?.name || event.payload?.player || GOAL_TO_CONFIRM} (${teamShort(team)})`, score].filter(Boolean).join(' · ');
}

function eventRefControl() {
  const control = inputControl('tpl-field-event-ref', {
    parse: (v) => (v ? Number(v) : null),
    setup(select, field) {
      const match = state.match;
      const previous = select.value;
      const goals = (match?.events || []).filter((e) => e.type === 'goal' && !e.annulled).sort((a, b) => b.sequence - a.sequence);
      const empty = h('option', { value: '', text: '— Gol ainda não lançado —' });
      select.replaceChildren(empty, ...goals.map((e) => h('option', { value: String(e.id), text: goalOptionText(e, match) })));
      select.required = !!field.required;
      if ([...select.options].some((o) => o.value === previous)) select.value = previous;
    },
  });
  return control;
}

function makeControl(field) {
  switch (field.kind) {
    case 'team': return teamControl();
    case 'player': return playerControl();
    case 'choice': return choiceControl();
    case 'int': return inputControl('tpl-field-int', { parse: (v) => (v === '' ? null : Number(v)) });
    case 'bool': return boolControl();
    case 'event_ref': return eventRefControl();
    case 'datetime': return inputControl('tpl-field-datetime');
    default: return inputControl('tpl-field-text');
  }
}

function minuteControl() {
  const el = cloneTemplate('tpl-field-minute');
  const p = hooks(el);
  const id = uid('minute');
  p.input.id = id;
  const control = {
    el,
    inputs: [p.input, p.stoppage],
    mode: 'none',
    dirty: false,
    configure(mode) {
      control.mode = mode;
      p.input.required = mode === 'required';
      p.label.textContent = mode === 'required' ? 'Minuto' : 'Minuto (opcional)';
      p.hint.textContent = mode === 'required'
        ? 'Sugerido pelo relógio do jogo. Acréscimo só no fim de cada tempo.'
        : 'Em branco, vale o minuto padrão do lance.';
    },
    suggest(value) {
      const minute = value?.minute != null ? String(value.minute) : '';
      const stoppage = value?.stoppage ? String(value.stoppage) : '';
      if (p.input.value !== minute) p.input.value = minute;
      if (p.stoppage.value !== stoppage) p.stoppage.value = stoppage;
    },
    read() {
      const minute = p.input.value === '' ? null : Number(p.input.value);
      const stoppage = p.stoppage.value === '' ? null : Number(p.stoppage.value);
      return { minute, stoppage: minute == null ? null : stoppage };
    },
    reset() {
      p.input.value = p.stoppage.value = '';
      control.dirty = false;
    },
    write(value) {
      p.input.value = value?.minute != null ? String(value.minute) : '';
      p.stoppage.value = value?.stoppage ? String(value.stoppage) : '';
      control.dirty = true; // não deixa a sugestão do relógio sobrescrever
    },
    focusTarget: () => p.input,
  };
  for (const input of control.inputs) input.addEventListener('input', () => { control.dirty = true; });
  return control;
}

/** Controles ativos do tipo aberto, na ordem do catálogo: [[campo, controle]]. */
function activeControls() {
  const spec = state.formType ? specFor(state.formType) : null;
  return (spec?.fields || []).map((field) => [field, state.controls.get(`${field.name}|${field.kind}`)]).filter(([, c]) => c);
}

/** Campos que dependem de outros: time some quando o gol anulado já foi escolhido; jogadores da escalação. */
function refreshDependencies() {
  const match = state.match;
  if (!match) return;
  const active = activeControls();
  const byName = new Map(active.map(([field, control]) => [field.name, control]));
  const target = byName.get('annuls_event_id');
  const team = byName.get('team_id');
  if (team) enableControl(team, !(target && target.read() != null)); // o gol anulado herda o time do gol
  const teamId = team && !team.el.hidden ? team.read() : null;
  const ownGoal = byName.get('payload.origin')?.read() === 'own_goal';
  const clockMinute = state.formType === 'clock_adjust' ? byName.get('payload.minute') : null;
  if (clockMinute) {
    // Acertar o minuto: só com a ação "set", obrigatório e dentro do tempo corrente.
    const setting = byName.get('payload.action')?.read() === 'set';
    const range = clockSetRange(match.period);
    enableControl(clockMinute, setting);
    const [input] = clockMinute.inputs;
    input.required = setting;
    if (range) {
      [input.min, input.max] = range.map(String);
      input.placeholder = `${range[0]} a ${range[1]}`;
    }
  }
  for (const [field, control] of active) {
    if (field.kind !== 'player' || !control.setPlayers) continue;
    control.setPlayers(lineupPlayers(match, teamId, { opponent: ownGoal && field.name === 'payload.player' }));
  }
}

function refreshFormOptions() {
  const spec = specFor(state.formType);
  for (const [field, control] of activeControls()) control.configure(field, state.match);
  state.minute?.configure(effectiveMinuteMode(spec, state.match?.period));
  refreshDependencies();
  refreshSuggestion();
}

function refreshSuggestion(nowMs = now()) {
  const control = state.minute;
  if (!control || control.dirty || control.el.hidden || !state.formType) return;
  control.suggest(suggestMinute(state.match, specFor(state.formType), nowMs));
}

function paintTypeSelect() {
  // mesma ordem dos botões: lances de jogo, depois o andamento do jogo (em grupo próprio)
  const { game, flow } = splitActions(state.available.events || [], specFor);
  const option = (type) => h('option', { value: type, text: specFor(type).label });
  els.eventType.replaceChildren(
    ...game.map(option),
    ...(flow.length ? [h('optgroup', { label: 'Andamento do jogo' }, ...flow.map(option))] : []),
  );
  els.eventType.value = state.formType || '';
}

function hideFormMessages() {
  els.eventWarnings.hidden = true;
  els.eventWarningsList.replaceChildren();
  els.eventError.hidden = true;
  els.eventError.textContent = '';
}

function openForm(type, { focus = true } = {}) {
  if (!state.match || !can('post_event')) return;
  const spec = specFor(type);
  const changed = state.formType !== type;
  state.formType = type;
  if (changed) state.formKey = null;
  hideFormMessages();

  els.formIcon.setAttribute('href', `#i-${spec.icon || 'info'}`);
  els.formTitle.textContent = state.editing ? `Editar · ${spec.label}` : spec.label;
  els.eventSubmitLabel.textContent = state.editing ? 'Salvar correção' : 'Lançar';
  els.eventType.disabled = !!state.editing;
  paintTypeSelect();

  // campos do tipo, na ordem do catálogo; os demais ficam escondidos (hidden) e desligados
  if (!state.minute) state.minute = minuteControl();
  const mode = effectiveMinuteMode(spec, state.match.period);
  state.minute.configure(mode);
  enableControl(state.minute, mode !== 'none');
  const order = [state.minute.el];
  const used = new Set();
  for (const field of spec.fields || []) {
    const key = `${field.name}|${field.kind}`;
    let control = state.controls.get(key);
    if (!control) {
      control = makeControl(field);
      state.controls.set(key, control);
    }
    used.add(key);
    control.configure(field, state.match);
    enableControl(control, true);
    order.push(control.el);
  }
  for (const [key, control] of state.controls) {
    if (!used.has(key)) {
      enableControl(control, false);
      order.push(control.el);
    }
  }
  els.eventFields.replaceChildren(...order);
  refreshDependencies();
  if (changed) state.minute.dirty = false;
  refreshSuggestion();

  els.formCard.hidden = false;
  for (const btn of actionButtons()) btn.setAttribute('aria-pressed', String(btn.dataset.type === type));
  if (focus) {
    const first = [state.minute, ...activeControls().map(([, c]) => c)].find((c) => c && !c.el.hidden);
    (first?.focusTarget() || els.eventType).focus();
    els.formCard.scrollIntoView?.({ block: 'nearest' });
  }
}

function resetFormValues() {
  for (const control of state.controls.values()) control.reset();
  state.minute?.reset();
  state.formKey = null;
}

function closeForm({ restoreFocus = true } = {}) {
  const type = state.formType;
  state.formType = null;
  state.formKey = null;
  state.editing = null;
  if (els) {
    els.eventSubmitLabel.textContent = 'Lançar';
    els.eventType.disabled = false;
  }
  if (!els) return;
  els.formCard.hidden = true;
  hideFormMessages();
  for (const btn of actionButtons()) btn.setAttribute('aria-pressed', 'false');
  if (restoreFocus && type) (findActionButton(type) || actionButtons()[0])?.focus();
}

/** "Cancelar"/Esc/clique no mesmo botão: desiste do lance — os valores não passam para o próximo. */
function cancelForm() {
  resetFormValues();
  closeForm();
}

function collectBody() {
  const spec = specFor(state.formType);
  const entries = [];
  for (const [field, control] of activeControls()) {
    if (control.el.hidden) continue;
    entries.push([field.name, control.read()]);
  }
  const minute = state.minute && !state.minute.el.hidden ? state.minute.read() : {};
  return buildEventBody(spec.type, entries, minute);
}

function setSending(on) {
  state.sending = on;
  els.eventSubmit.disabled = on;
  if (on) els.eventSubmit.setAttribute('aria-busy', 'true');
  else els.eventSubmit.removeAttribute('aria-busy');
}

function eventSummary(event) {
  if (!event) return '';
  const player = event.player?.name || event.payload?.player || '';
  return [event.type_label, player].filter(Boolean).join(' · ') + (event.minute_label ? ` (${event.minute_label})` : '');
}

async function onSubmitEvent(event) {
  event.preventDefault();
  if (state.sending || !state.formType || !state.match) return;
  hideFormMessages();
  if (!els.eventForm.reportValidity()) return; // validação nativa (o form tem novalidate para não validar no Enter dos campos escondidos)
  const body = collectBody();
  const spec = specFor(state.formType);
  if (spec.kind === 'structural') {
    // andamento do jogo (início, fim de tempo, pênaltis, fim): sempre confirma antes de enviar
    const ok = await askConfirm(structuralConfirm(spec.type, state.match, spec.label));
    if (!ok) return;
  }
  if (state.editing) {
    await sendEdit(state.editing, body, false);
    return;
  }
  state.formKey = state.formKey || api.newIdempotencyKey();
  await sendEvent(body, state.formKey, false);
}

/** "Editar": abre o formulário do tipo do lance já preenchido com os dados dele. */
function openEditForm(event) {
  if (!state.match || !can('post_event') || !can('void_event')) return;
  resetFormValues();
  state.editing = event;
  openForm(event.type, { focus: false });
  state.minute?.write({ minute: event.minute, stoppage: event.stoppage });
  for (const [field, control] of activeControls()) {
    let value = null;
    if (field.name === 'team_id') value = event.team_id;
    else if (field.name === 'annuls_event_id') value = event.annuls_event_id;
    else if (field.name.startsWith('payload.')) value = event.payload?.[field.name.slice(8)];
    control.write?.(value);
  }
  refreshDependencies();
  (state.minute && !state.minute.el.hidden ? state.minute.focusTarget() : els.eventType).focus();
  els.formCard.scrollIntoView?.({ block: 'nearest' });
}

async function sendEdit(original, body, confirm) {
  const matchId = state.matchId;
  setSending(true);
  try {
    const result = await api.editEvent(matchId, original.id, { ...body, type: original.type, confirm });
    if (state.matchId !== matchId) return;
    showToast(editedToast(eventSummary(result?.event) || specFor(original.type).label, result?.voided), { kind: 'ok' });
    resetFormValues();
    closeForm();
    applyMatch(result?.match, result?.available);
    refreshMatch();
  } catch (error) {
    if (state.matchId !== matchId) return;
    if (error?.code === 'confirmation_required') {
      const ok = await askConfirm({ title: 'Confirmar a correção?', text: error.message, items: (error.warnings || []).map((w) => w.message), ok: 'Corrigir mesmo assim' });
      if (ok) await sendEdit(original, body, true);
      return;
    }
    if (error?.status === 401) return sessionExpired();
    showToast(genericError(error), { kind: 'error' });
  } finally {
    setSending(false);
  }
}

async function sendEvent(body, key, confirm) {
  const matchId = state.matchId;
  setSending(true);
  try {
    const result = await api.postEvent(matchId, { ...body, confirm, source: 'operator' }, key);
    if (state.matchId !== matchId) return;
    const label = eventSummary(result?.event) || specFor(body.type).label;
    showToast(result?.replayed ? `Já estava lançado: ${label}.` : `Lançado: ${label}.`, { kind: 'ok' });
    resetFormValues();
    closeForm();
    applyMatch(result?.match, result?.available);
    refreshMatch();
  } catch (error) {
    if (state.matchId !== matchId) return;
    await handleEventError(error, body, key);
  } finally {
    setSending(false);
  }
}

async function handleEventError(error, body, key) {
  if (error?.code === 'confirmation_required') {
    const warnings = error.warnings || [];
    els.eventWarningsList.replaceChildren(...warnings.map((w) => h('li', { text: w.message })));
    els.eventWarnings.hidden = warnings.length === 0;
    const ok = await askConfirm({
      title: 'Confirme o lançamento',
      text: error.message,
      items: warnings.map((w) => w.message),
      ok: 'Confirmar',
    });
    if (ok) {
      setSending(false);
      await sendEvent(body, key, true); // mesma chave: é o mesmo lançamento, agora confirmado
    }
    return;
  }
  if (error?.status === 401) return sessionExpired();
  if (error?.code === 'csrf_failed') {
    await api.getMe().catch(() => {});
    showToast('Sessão renovada. Envie de novo.', { kind: 'warn' });
    return;
  }
  if (error?.status === 403) {
    showToast('Seu perfil não tem permissão para esta ação.', { kind: 'error' });
    return;
  }
  if (error?.isNetwork) {
    // o lance pode ter chegado: a mesma chave (state.formKey) segue valendo para o reenvio
    showToast('Sem resposta do servidor. Confira a conexão e envie de novo — o lance não será duplicado.', { kind: 'error', timeout: 8000 });
    return;
  }
  if (error?.status === 422 || error?.status === 400) {
    state.formKey = null; // nada foi gravado: o próximo envio é outro pedido
    els.eventError.textContent = error.message;
    els.eventError.hidden = false;
    const fieldName = ERROR_FIELD[error.code] || error.details?.field;
    const control = fieldName === 'minute' ? state.minute : activeControls().find(([f]) => f.name === fieldName)?.[1];
    (control && !control.el.hidden ? control.focusTarget() : els.eventError).focus?.();
    if (control === state.minute) state.minute.dirty = true;
    return;
  }
  showToast(genericError(error), { kind: 'error' });
}

/* --- Diálogos ------------------------------------------------------------------------------------ */

function openDialog(dialog) {
  const opener = document.activeElement;
  return new Promise((resolve) => {
    dialog.returnValue = '';
    dialog.addEventListener('close', () => {
      resolve(dialog.returnValue === 'confirm');
      if (opener?.isConnected && typeof opener.focus === 'function') opener.focus();
    }, { once: true });
    dialog.showModal();
  });
}

function askConfirm({ title, text, items = [], ok = 'Confirmar' }) {
  const p = hooks(els.confirmDialog);
  p['confirm-title'].textContent = title;
  p['confirm-text'].textContent = text || '';
  p['confirm-list'].replaceChildren(...items.map((item) => h('li', { text: item })));
  p['confirm-list'].hidden = items.length === 0;
  p['confirm-ok'].textContent = ok;
  return openDialog(els.confirmDialog);
}

async function openStatusDialog(item) {
  const match = state.match;
  if (!match) return;
  const p = hooks(els.statusDialog);
  const reschedule = item.action === 'reschedule';
  const delay = item.action === 'delay';
  p['status-title'].textContent = delay ? 'Marcar a partida como atrasada?' : `${item.label} a partida?`;
  p['status-text'].textContent = `${teamName(match.home)} × ${teamName(match.away)} · ${formatWhen(match.kickoff_at, now())} · ${match.status_label || ''}`;
  p['status-kickoff-field'].hidden = !reschedule;
  p['status-kickoff'].required = reschedule;
  p['status-kickoff'].disabled = !reschedule;
  p['status-kickoff'].value = reschedule ? toBrasiliaInput(match.kickoff_at) : '';
  p['status-reason'].value = delay ? match.status_note || '' : '';
  p['status-reason'].required = delay;
  p['status-reason-label'].textContent = delay ? 'Observação: razão do atraso (obrigatória)' : 'Motivo (opcional)';
  p['status-ok'].textContent = item.label;
  p['status-ok'].classList.toggle('btn--danger', item.action === 'cancel');
  p['status-ok'].classList.toggle('btn--primary', item.action !== 'cancel');
  if (!(await openDialog(els.statusDialog))) return;
  const body = { action: item.action };
  if (reschedule) body.kickoff_at = p['status-kickoff'].value; // sem fuso = horário de Brasília (CONTRACT §2)
  const reason = p['status-reason'].value.trim();
  if (reason) body.reason = reason;
  sendStatus(body, api.newIdempotencyKey(), item.label);
}

async function sendStatus(body, key, label) {
  const matchId = state.matchId;
  setStatusBusy(true);
  try {
    const result = await api.changeStatus(matchId, body, key);
    if (state.matchId !== matchId) return;
    showToast(`${label}: feito. Status da partida: ${result?.match?.status_label || 'atualizado'}.`, { kind: 'ok' });
    applyMatch(result?.match, result?.available);
    refreshMatch();
  } catch (error) {
    if (error?.status === 401) return sessionExpired();
    if (error?.isNetwork) {
      showToast('Sem resposta do servidor. A mudança de status pode não ter sido registrada.', {
        kind: 'error',
        timeout: 0,
        action: { label: 'Tentar de novo', onClick: () => sendStatus(body, key, label) }, // mesma chave
      });
      return;
    }
    showToast(error?.status === 403 ? 'Seu perfil não tem permissão para esta ação.' : genericError(error), { kind: 'error', timeout: 8000 });
  } finally {
    setStatusBusy(false);
  }
}

function setStatusBusy(on) {
  for (const btn of els.statusActions.querySelectorAll('button')) {
    if (on) {
      btn.dataset.wasDisabled = String(btn.disabled);
      btn.disabled = true;
    } else if (btn.dataset.wasDisabled) {
      btn.disabled = btn.dataset.wasDisabled === 'true';
      delete btn.dataset.wasDisabled;
    }
  }
}

/* --- Linha do tempo -------------------------------------------------------------------------------- */

function eventTitle(event) {
  const player = event.player?.name || event.payload?.player || (event.type === 'goal' ? 'informações a confirmar' : '');
  if (event.type === 'substitution') return event.type_label;
  return [event.type_label, player].filter(Boolean).join(' · ');
}

function eventSub(event, match) {
  const p = event.payload || {};
  const parts = [];
  if (event.team_side && match[event.team_side]) parts.push(teamShort(match[event.team_side]));
  switch (event.type) {
    case 'goal':
      if (p.origin === 'penalty') parts.push('de pênalti');
      if (p.origin === 'own_goal') parts.push('contra');
      if (p.assist) parts.push(`assistência de ${p.assist}`);
      if (event.score_after) parts.push(`${event.score_after.home} × ${event.score_after.away}`);
      if (event.annulled) parts.push('anulado');
      break;
    case 'goal_annulled': {
      const goal = (match.events || []).find((e) => e.id === event.annuls_event_id);
      if (goal) parts.push(`gol de ${goal.player?.name || goal.payload?.player || '?'} (${goal.minute_label})`);
      if (p.reason) parts.push(p.reason);
      break;
    }
    case 'substitution':
      if (p.player_in) parts.push(`entra ${p.player_in}`);
      if (p.player_out) parts.push(`sai ${p.player_out}`);
      break;
    case 'red_card':
      if (p.reason === 'second_yellow') parts.push('2º amarelo (automático)');
      break;
    case 'var_review':
      parts.push([p.incident, p.decision].filter(Boolean).join(' → '));
      break;
    case 'stoppage_time':
      if (p.minutes != null) parts.push(`+${p.minutes} min`);
      break;
    case 'shootout_kick':
      parts.push(p.scored ? 'convertida' : 'perdida');
      break;
    case 'rescheduled':
      if (p.kickoff_at) parts.push(`para ${formatWhen(p.kickoff_at, now())}`);
      if (p.reason) parts.push(p.reason);
      break;
    default:
      if (p.reason && typeof p.reason === 'string') parts.push(p.reason);
  }
  return parts.filter(Boolean).join(' · ');
}

function paintTimeline() {
  const match = state.match;
  const events = sortEventsByClock(match.events || [], { desc: true }); // mais recente no jogo primeiro
  els.timelineCount.textContent = String(events.length);
  els.timelineEmpty.hidden = events.length > 0;
  els.timeline.replaceChildren(...events.map((event) => {
    const li = cloneTemplate('tpl-op-event');
    const p = hooks(li);
    const title = eventTitle(event);
    li.dataset.eventId = String(event.id);
    p.minute.textContent = event.minute_label || event.period_short || '';
    p.icon.setAttribute('href', `#i-${eventIconName(event)}`);
    p.title.textContent = title;
    p.sub.textContent = eventSub(event, match);
    li.classList.toggle('op-event--goal', event.type === 'goal');
    li.classList.toggle('op-event--annulled', !!event.annulled);
    li.classList.toggle('op-event--structural', event.kind === 'structural');
    const editable = event.kind === 'game' && !event.derived && can('post_event') && can('void_event');
    if (!editable) {
      p.edit.remove();
    } else {
      p.edit.setAttribute('aria-label', `Editar lance: ${title}${event.minute_label ? `, ${event.minute_label}` : ''}`);
      p.edit.addEventListener('click', () => openEditForm(event));
    }
    // O vermelho automático (derivado) não tem botão: ele só cai junto com o amarelo que o gerou.
    if (!can('void_event') || event.derived) {
      p.void.remove();
    } else {
      p.void.setAttribute('aria-label', `Cancelar lançamento: ${title}${event.minute_label ? `, ${event.minute_label}` : ''}`);
      p.void.addEventListener('click', () => openVoidDialog(event));
    }
    return li;
  }));
}

async function openVoidDialog(event) {
  const p = hooks(els.voidDialog);
  p['void-text'].textContent = `${eventSummary(event)}: o lance sai do placar, da tabela e dos últimos gols. Lances ligados a ele caem junto.`;
  p['void-reason'].value = '';
  if (!(await openDialog(els.voidDialog))) return;
  sendVoid(event, p['void-reason'].value.trim());
}

async function sendVoid(event, reason) {
  const matchId = state.matchId;
  const buttons = [...els.timeline.querySelectorAll('.op-event__void:not(:disabled)')];
  for (const b of buttons) b.disabled = true;
  try {
    const result = await api.voidEvent(matchId, event.id, reason);
    if (state.matchId !== matchId) return;
    const others = (result?.voided?.length || 1) - 1;
    showToast(`Lançamento cancelado: ${eventSummary(event)}.${others > 0 ? ` ${others === 1 ? 'Um lance ligado caiu' : `${others} lances ligados caíram`} junto.` : ''}`, { kind: 'ok' });
    applyMatch(result?.match, result?.available);
    refreshMatch();
  } catch (error) {
    for (const b of buttons) if (b.isConnected) b.disabled = false;
    if (error?.status === 401) return sessionExpired();
    if (error?.isNetwork) {
      showToast('Sem resposta do servidor. Confira a conexão e tente de novo.', {
        kind: 'error',
        timeout: 0,
        action: { label: 'Tentar de novo', onClick: () => sendVoid(event, reason) }, // cancelar de novo é seguro (already_voided)
      });
      return;
    }
    showToast(error?.status === 403 ? 'Seu perfil não tem permissão para esta ação.' : genericError(error), { kind: 'error', timeout: 9000 });
    if (error?.code === 'already_voided' || error?.code === 'event_not_found') refreshMatch();
  }
}

/* --- Início ----------------------------------------------------------------------------------------- */

function bindElements() {
  const $ = (id) => document.getElementById(id);
  const form = $('event-form');
  const fh = hooks(form);
  return {
    clock: $('brasilia-clock'),
    boot: $('op-boot'),
    login: $('op-login'),
    loginForm: $('login-form'),
    loginError: $('login-error'),
    loginErrorText: document.querySelector('#login-error [data-hook="login-error-text"]'),
    loginSubmit: $('login-submit'),
    app: $('op-app'),
    userName: document.querySelector('#op-app [data-hook="user-name"]'),
    userRoles: document.querySelector('#op-app [data-hook="user-roles"]'),
    adminLink: document.querySelector('#op-app [data-hook="admin-link"]'),
    logout: $('logout-button'),
    pickerDate: $('picker-date'),
    pickerRefresh: $('picker-refresh'),
    pickerList: $('picker-list'),
    pickerEmpty: $('picker-empty'),
    panel: $('op-panel'),
    placeholder: $('op-placeholder'),
    scoreboard: $('op-scoreboard'),
    tools: $('op-match-tools'),
    work: $('op-work'),
    actionsCard: document.querySelector('[data-hook="actions-card"]'),
    actionsBlock: document.querySelector('[data-hook="actions-block"]'),
    statusBlock: document.querySelector('[data-hook="status-block"]'),
    actionGrid: $('action-grid'),
    flowBlock: $('flow-block'),
    flowActions: $('flow-actions'),
    statusActions: $('status-actions'),
    formCard: $('event-form-card'),
    eventForm: form,
    eventType: $('event-type'),
    formIcon: fh['event-form-icon'],
    formTitle: fh['event-form-title'],
    eventWarnings: fh['event-warnings'],
    eventWarningsList: fh['event-warnings-list'],
    eventError: fh['event-error'],
    eventFields: fh['event-fields'],
    eventCancel: fh['event-cancel'],
    eventSubmit: fh['event-submit'],
    eventSubmitLabel: fh['event-submit-label'],
    timeline: $('op-timeline'),
    timelineEmpty: $('op-timeline-empty'),
    timelineCount: document.querySelector('[data-hook="timeline-count"]'),
    confirmDialog: $('confirm-dialog'),
    statusDialog: $('status-dialog'),
    voidDialog: $('void-dialog'),
  };
}

function bindEvents() {
  els.loginForm.addEventListener('submit', onLogin);
  els.logout.addEventListener('click', onLogout);
  els.pickerDate.addEventListener('change', () => {
    if (!els.pickerDate.value) return;
    state.date = els.pickerDate.value;
    writeUrl();
    loadPicker();
  });
  els.pickerRefresh.addEventListener('click', () => {
    loadPicker();
    if (state.matchId) refreshMatch();
  });
  // setas ↑/↓ andam pela lista de partidas
  els.pickerList.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const buttons = [...els.pickerList.querySelectorAll('.pick-item')];
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    buttons[Math.max(0, Math.min(buttons.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))].focus();
  });
  els.eventForm.addEventListener('submit', onSubmitEvent);
  els.eventForm.addEventListener('input', (event) => {
    if (event.target !== els.eventType) state.formKey = null; // conteúdo mudou: outro lançamento
  });
  els.eventForm.addEventListener('change', (event) => {
    if (event.target === els.eventType) return;
    state.formKey = null;
    refreshDependencies();
  });
  els.eventForm.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      cancelForm();
    }
  });
  els.eventType.addEventListener('change', () => {
    if (els.eventType.value && els.eventType.value !== state.formType) openForm(els.eventType.value, { focus: false });
  });
  els.eventCancel.addEventListener('click', () => cancelForm());
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.me && state.matchId && !state.sending) refreshMatch();
  });
}

async function boot() {
  els = bindElements();
  bindEvents();
  const params = new URLSearchParams(window.location.search);
  state.date = /^\d{4}-\d{2}-\d{2}$/.test(params.get('date') || '') ? params.get('date') : dayKey(now());
  els.pickerDate.value = state.date;
  clock.onTick((ms) => {
    if (state.card) tickMatchCards(els.scoreboard, ms);
    refreshSuggestion(ms);
  });
  try {
    const me = await api.getMe();
    sync(me); // relógio do cabeçalho certo já na tela de login
    if (me?.authenticated && me.user) await showApp(me);
    else showLogin();
  } catch {
    els.boot.hidden = true;
    showLogin('Não deu certo agora. Tente de novo em instantes.');
  }
}

if (typeof document !== 'undefined' && document.getElementById('op-app')) boot();
