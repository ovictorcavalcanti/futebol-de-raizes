/**
 * Guia de estilo (/styleguide.html, só com DEBUG): desenha todos os componentes
 * com os dados de exemplo de fixtures.js — QA visual dos dois temas.
 * Também serve de exemplo de uso dos módulos e templates para as páginas.
 */
import { h, hook, hooks, cloneTemplate, showToast, renderCompetitionNav } from './render.js';
import { icon, ICONS, eventIconName } from './icons.js';
import { createCrest } from './crest.js';
import { ServerClock, mountClock } from './clock.js';
import { formatTime } from './format.js';
import { createMatchCard, updateMatchCard, tickMatchCards, createTieCard, createLatestGoal, createGoalAlert } from './match-card.js';
import { createStandings } from './standings.js';
import * as F from './fixtures.js';

const clock = new ServerClock();
clock.sync(F.SERVER_TIME);
const now = () => clock.nowMs();
const $ = (name) => document.querySelector(`[data-hook="${name}"]`);

/* --- Cabeçalho: menu de competições e relógio de Brasília --------------------------- */
renderCompetitionNav(document.querySelector('[data-hook="competition-links"]'), F.COMPETITIONS, {
  activeSlug: 'pernambucano',
  liveSlugs: new Set(['pernambucano']),
  href: () => '#sg-home',
});
const clockEl = document.getElementById('brasilia-clock');
if (clockEl) mountClock(clockEl, clock);

/* --- Cores ----------------------------------------------------------------------------- */
const TOKENS = ['paper', 'surface', 'surface-2', 'ink', 'ink-2', 'ink-3', 'line', 'azul', 'vermelho', 'amarelo', 'verde'];
function paintColors() {
  const css = getComputedStyle(document.documentElement);
  $('sg-colors').replaceChildren(...TOKENS.map((t) => h('div', { class: 'sg-swatch' },
    h('span', { class: 'sg-swatch__chip', style: { background: `var(--${t})` } }),
    h('span', { class: 'sg-swatch__name', text: `--${t}` }),
    h('span', { class: 'sg-swatch__value', text: css.getPropertyValue(`--${t}`).trim() }),
  )));
}
paintColors();
document.addEventListener('themechange', paintColors);

/* --- Ícones e escudos ------------------------------------------------------------------- */
$('sg-icons').replaceChildren(...ICONS.map((name) => h('div', { class: 'sg-icon' }, icon(name), name)));
$('sg-crests').replaceChildren(...Object.values(F.TEAMS).map((t) => h('div', { class: 'stack', style: { 'justify-items': 'center', 'font-size': '.75rem', '--stack-gap': '4px' } },
  createCrest(t, { size: 48 }), h('span', { class: 'num', text: t.short_name }))));

/* --- Cards de jogo: todas as variantes ------------------------------------------------- */
const variants = [
  ['Ao vivo · 2º tempo (detalhe aberto)', F.MATCHES.live, { open: true }],
  ['Intervalo', F.MATCHES.halfTime],
  ['Agendado · hoje', F.MATCHES.scheduledToday],
  ['Agendado · amanhã (rodada visível)', F.MATCHES.scheduledTomorrow, { showRound: true }],
  ['Encerrado · vitória do visitante', F.MATCHES.finished],
  ['Mata-mata · decidido nos pênaltis', F.MATCHES.knockoutPenalties, { showRound: true }],
  ['Mata-mata · jogo de ida', F.summaryOf(F.MATCHES.knockoutLeg1), { showRound: true }],
  ['Mata-mata · jogo único agendado', F.MATCHES.knockoutSingle, { showRound: true }],
  ['Suspenso', F.MATCHES.suspended],
  ['Adiado', F.MATCHES.postponed],
  ['Cancelado', F.MATCHES.cancelled],
  ['Carregamento preguiçoso (abra o acordeão)', F.summaryOf(F.MATCHES.live), { lazy: true }],
];
let liveCard = null;
$('sg-matches').replaceChildren(...variants.map(([label, match, extra = {}]) => {
  const card = createMatchCard(match, {
    now,
    showRound: extra.showRound,
    onExpand: extra.lazy ? () => new Promise((resolve) => setTimeout(() => { updateMatchCard(card, F.MATCHES.live); resolve(); }, 900)) : undefined,
  });
  if (extra.open) {
    card.querySelector('details').open = true;
    liveCard = card;
  }
  return h('div', { class: 'stack', style: { '--stack-gap': '6px' } }, h('span', { class: 'sg-label', text: label }), card);
}));

/* Simular gol: o placar pisca e o lance novo ganha destaque */
let simulated = 0;
$('sg-simulate-goal').addEventListener('click', () => {
  if (!liveCard) return;
  simulated += 1;
  const base = F.MATCHES.live;
  const homeScore = base.home_score + simulated;
  const event = {
    ...base.events.find((e) => e.type === 'goal'),
    id: 9000 + simulated,
    sequence: base.events.length + simulated,
    minute: 73 + simulated,
    minute_label: `${73 + simulated}'`,
    payload: { player: 'Romarinho', origin: 'open_play' },
    player: { id: null, name: 'Romarinho' },
    score_after: { home: homeScore, away: base.away_score },
    created_at: new Date(now()).toISOString(),
  };
  const extraEvents = Array.from({ length: simulated }, (_, i) => ({ ...event, id: 9001 + i, sequence: base.events.length + i + 1, minute: 74 + i, minute_label: `${74 + i}'`, score_after: { home: base.home_score + i + 1, away: base.away_score } }));
  const next = {
    ...base,
    version: base.version + simulated,
    home_score: homeScore,
    events: [...base.events, ...extraEvents],
    goals: [...base.goals, ...extraEvents.map((e) => ({ event_id: e.id, match_id: base.id, team_id: e.team_id, team_side: 'home', player: 'Romarinho', origin: 'open_play', period: e.period, minute: e.minute, stoppage: null, minute_label: e.minute_label, score_after: e.score_after, created_at: e.created_at }))],
  };
  updateMatchCard(liveCard, next, { flash: true });
});

/* --- Home: últimos gols, aviso de gol e seção de competição (como a home monta) --------- */
$('sg-goals').replaceChildren(...F.LATEST_GOALS.map((g, i) => createLatestGoal(g, { isNew: i === 0 })));
$('sg-goal-alert').append(createGoalAlert(F.LATEST_GOALS[0]));

function renderCompetitionSection(comp) {
  const section = cloneTemplate('tpl-competition-section');
  const parts = hooks(section);
  parts['competition-name'].textContent = comp.name;
  parts['competition-link'].href = `#sg-mata`;
  parts['competition-more'].href = `#sg-mata`;
  for (const stage of comp.stages) {
    const block = cloneTemplate('tpl-stage-block');
    const b = hooks(block);
    b['stage-name'].textContent = stage.name;
    b['stage-name'].hidden = comp.stages.length === 1 && stage.format !== 'knockout';
    b.matches.replaceChildren(...stage.matches.map((m) => {
      const full = Object.values(F.MATCHES).find((x) => x.id === m.id);
      return createMatchCard(m, { now, onExpand: () => new Promise((resolve) => setTimeout(() => { if (full?.events) updateMatchCard(cardOf(m.id), full); resolve(); }, 600)) });
    }));
    if (stage.standings) b.standings.append(createStandings(stage.standings));
    else {
      b.standings.remove();
      b['stage-grid'].classList.add('split--no-aside');
    }
    parts.stages.append(block);
  }
  return section;
}
const cardOf = (id) => document.querySelector(`#sg-home .match[data-match-id="${id}"]`);
$('sg-home-competitions').replaceChildren(...F.HOME.competitions.map(renderCompetitionSection));

/* --- Classificação e mata-mata ------------------------------------------------------------- */
$('sg-standings').append(createStandings(F.STANDINGS));
$('sg-standings-groups').append(createStandings(F.STANDINGS_GROUPS, { compact: true }));
$('sg-ties').replaceChildren(...F.TIES.map((t) => createTieCard(t, { now })));

/* --- Estados: avisos ------------------------------------------------------------------------ */
$('sg-toast').addEventListener('click', () => showToast('Lance lançado: gol de Zé Roberto (72\').', { kind: 'ok' }));
$('sg-toast-error').addEventListener('click', () => showToast('Não deu certo agora. Tente de novo em instantes.', { kind: 'error' }));
$('sg-alert').addEventListener('click', () => $('sg-goal-alert').prepend(createGoalAlert(F.LATEST_GOALS[0])));
$('sg-correction').addEventListener('click', () => $('sg-goal-alert').prepend(createGoalAlert(F.LATEST_GOALS[0], { kind: 'correction', reason: 'annulled' })));
for (const btn of document.querySelectorAll('[data-sg-toggle]')) {
  btn.addEventListener('click', () => btn.setAttribute('aria-pressed', String(btn.getAttribute('aria-pressed') !== 'true')));
}

/* --- Operador: lista de partidas, ações, formulário, lançamentos --------------------------- */
// lista de listas, como em operator.js: <li> da competição › título + <ul> das partidas
const pickerGroup = h('ul', { class: 'match-picker__sublist', 'aria-labelledby': 'sg-picker-group' });
$('sg-picker').replaceChildren(h('li', { class: 'match-picker__group' },
  h('h3', { class: 'match-picker__group-title', id: 'sg-picker-group', text: F.MATCHES.live.competition.name }), pickerGroup));
for (const [i, m] of [F.MATCHES.live, F.MATCHES.halfTime, F.MATCHES.scheduledToday, F.MATCHES.finished].entries()) {
  const li = cloneTemplate('tpl-pick-item');
  const p = hooks(li);
  p['home-crest'].replaceWith(createCrest(m.home, { size: 20 }));
  p['away-crest'].replaceWith(createCrest(m.away, { size: 20 }));
  p.home.textContent = m.home.short_name;
  p.away.textContent = m.away.short_name;
  p.score.textContent = m.status === 'scheduled' ? '×' : `${m.home_score}–${m.away_score}`;
  const pillCls = m.status === 'live' ? (m.period === 'half_time' ? 'pill--interval' : 'pill--live') : m.status === 'finished' ? 'pill--finished' : 'pill--scheduled';
  p.status.replaceWith(h('span', { class: `pill pill--sm ${pillCls}`, text: m.period === 'half_time' ? 'Intervalo' : m.status_label }));
  p.time.textContent = formatTime(m.kickoff_at);
  p.time.dateTime = m.kickoff_at;
  p.competition.textContent = `${m.competition.short_name} · ${m.round.name}`;
  if (i === 0) p.pick.setAttribute('aria-current', 'true');
  pickerGroup.append(li);
}

$('sg-op-scoreboard').append(createMatchCard(F.summaryOf(F.MATCHES.live), { now, details: false, showRound: true, showCompetition: true }));

// como em operator.js: lances de jogo primeiro; estruturais no grupo "Andamento do jogo"
const available = new Set(F.AVAILABLE.events);
const actionSpecs = F.CATALOG.events.filter((spec) => available.has(spec.type));
const actionButton = (spec) => {
  const btn = cloneTemplate('tpl-action-button');
  const p = hooks(btn);
  p['action-icon'].setAttribute('href', `#i-${spec.icon}`);
  p['action-label'].textContent = spec.label;
  btn.dataset.type = spec.type;
  if (spec.type === 'goal') {
    btn.classList.add('action-btn--goal');
    btn.setAttribute('aria-pressed', 'true');
  }
  if (spec.kind === 'structural') btn.classList.add('action-btn--structural');
  return btn;
};
$('sg-actions').replaceChildren(...actionSpecs.filter((spec) => spec.kind !== 'structural').map(actionButton));
$('sg-flow-actions').replaceChildren(...actionSpecs.filter((spec) => spec.kind === 'structural').map(actionButton));
const STATUS_ICON = { postpone: 'calendar', suspend: 'pause', resume: 'play', reschedule: 'calendar', cancel: 'x-circle' };
$('sg-status').replaceChildren(...F.CATALOG.status_actions.map((a) => {
  const btn = cloneTemplate('tpl-status-button');
  const p = hooks(btn);
  p['status-icon'].setAttribute('href', `#i-${STATUS_ICON[a.action]}`);
  p['status-label'].textContent = a.label;
  if (a.action === 'cancel') btn.classList.replace('btn--secondary', 'btn--danger');
  btn.disabled = !F.AVAILABLE.status.includes(a.action);
  return btn;
}));

/* Formulário do gol, montado a partir dos templates de campo */
const fields = $('sg-fields');
const live = F.MATCHES.live;
const minute = cloneTemplate('tpl-field-minute');
hook(minute, 'input').value = '74';
fields.append(minute);
const teamField = cloneTemplate('tpl-field-team');
const tf = hooks(teamField);
tf.label.textContent = 'Time beneficiado';
for (const side of ['home', 'away']) {
  tf[`input-${side}`].name = 'team_id';
  tf[`input-${side}`].value = live[side].id;
  tf[`crest-${side}`].replaceWith(createCrest(live[side], { size: 26 }));
  tf[`name-${side}`].textContent = live[side].name;
}
tf['input-home'].checked = true;
fields.append(teamField);
const player = cloneTemplate('tpl-field-player');
const pf = hooks(player);
pf.label.textContent = 'Jogador';
pf.input.id = 'sg-f-player';
pf.label.htmlFor = 'sg-f-player';
pf.datalist.id = 'sg-dl-players';
pf.input.setAttribute('list', 'sg-dl-players');
pf.input.value = 'Romarinho';
for (const pl of live.lineups.home.starters) pf.datalist.append(h('option', { value: pl.name }));
fields.append(player);
const origin = cloneTemplate('tpl-field-choice');
const of = hooks(origin);
of.label.textContent = 'Origem';
of.input.id = 'sg-f-origin';
of.label.htmlFor = 'sg-f-origin';
for (const [v, l] of [['open_play', 'Jogada'], ['penalty', 'Pênalti'], ['own_goal', 'Contra']]) of.input.append(h('option', { value: v, text: l }));
fields.append(origin);
const bool = cloneTemplate('tpl-field-bool');
hook(bool, 'label').textContent = 'Confirmar mesmo com aviso';
fields.append(bool);

/* Lançamentos do operador (mais recente primeiro) com "cancelar lançamento" */
const opList = $('sg-op-timeline');
const events = [...live.events].reverse();
$('sg-op-count').textContent = String(events.length);
for (const e of events) {
  const li = cloneTemplate('tpl-op-event');
  const p = hooks(li);
  p.minute.textContent = e.minute_label;
  p.icon.setAttribute('href', `#i-${eventIconName(e)}`);
  p.title.textContent = e.type === 'goal' || e.type.endsWith('_card') ? `${e.type_label} · ${e.payload.player}` : e.type_label;
  p.sub.textContent = [e.team_side ? live[e.team_side].short_name : '', (e.payload.reason === 'second_yellow' ? '2º amarelo' : e.payload.reason) || e.payload.decision || (e.payload.player_in ? `entra ${e.payload.player_in}, sai ${e.payload.player_out}` : '')].filter(Boolean).join(' · ');
  if (e.type === 'goal') li.classList.add('op-event--goal');
  if (e.annulled) li.classList.add('op-event--annulled');
  if (e.kind === 'structural') li.classList.add('op-event--structural');
  if (e.derived) {
    p.void.disabled = true;
    p.void.title = 'Cai junto com o amarelo que o gerou';
  }
  opList.append(li);
}
$('sg-open-dialog').addEventListener('click', () => document.getElementById('sg-dialog').showModal());

/* --- Relógio: minuto ao vivo a cada segundo ------------------------------------------------- */
clock.onTick((ms) => tickMatchCards(document, ms));
