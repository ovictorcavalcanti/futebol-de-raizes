/**
 * Card de jogo do Futebol de Raízes (inspirado no wireframe docs/wireframe-jogo.webp).
 *
 * createMatchCard(match, opts) desenha; updateMatchCard(el, match, opts) atualiza no
 * lugar, preservando o <details> aberto e a aba ativa. Lances, escalações e ficha
 * são desenhados só com o acordeão aberto (economia de DOM com muitos jogos).
 *
 * Também exporta os componentes ligados ao jogo: confronto de mata-mata,
 * item de "Últimos gols" e aviso de gol.
 */
import { h, clear, setText } from './render.js';
import { icon, eventIconName } from './icons.js';
import { createCrest } from './crest.js';
import { formatTime, formatWhen, formatDay, formatInt, formatMoney, formatScore, toDate } from './format.js';
import { liveMinuteLabel } from './clock.js';

/** Estado por card: {match, opts, eventIds, requested, activeTab, failed}. */
const STATE = new WeakMap();
let seq = 0;

const POSITION_SHORT = { GK: 'GOL', LAD: 'LAD', DF: 'ZAG', LAE: 'LAE', VOL: 'VOL', MF: 'MEI', FW: 'ATA' };
const ORIGIN_TAG = { penalty: 'pên.', own_goal: 'contra' };
const ORIGIN_LABEL = { open_play: 'Jogada', penalty: 'De pênalti', own_goal: 'Gol contra' };
const MISS_LABEL = { saved: 'Defendido', off_target: 'Para fora', woodwork: 'Na trave' };
const SEPARATORS = {
  match_start: { label: 'Início de jogo', icon: 'whistle' },
  half_time: { label: 'Intervalo', icon: 'whistle', score: true },
  second_half_start: { label: '2º tempo', icon: 'whistle' },
  extra_time_start: { label: 'Prorrogação', icon: 'whistle', score: true },
  penalties_start: { label: 'Disputa de pênaltis', icon: 'ball-penalty', score: true },
  match_end: { label: 'Fim de jogo', icon: 'flag', score: true, end: true },
};
const STATUS_EVENTS = new Set(['delayed', 'postponed', 'suspended', 'resumed', 'rescheduled', 'cancelled']);
const TABS = [
  { key: 'events', label: 'Lances', icon: 'list' },
  { key: 'lineups', label: 'Escalações', icon: 'shirt' },
  { key: 'facts', label: 'Ficha', icon: 'clipboard' },
];

const nowOf = (opts) => (typeof opts.now === 'function' ? opts.now() : Date.now());

/** Link externo vindo da API: só http(s) (barra javascript:, data: etc.); inválido → null. */
export function safeHref(url) {
  if (!url) return null;
  try {
    const parsed = new URL(String(url), globalThis.location?.href || 'http://localhost/');
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch {
    return null;
  }
}
/**
 * Linha do gol anulado na linha do tempo — registro factual, sem sotaque (IDENTIDADE §5:
 * o "Oxe!" é do aviso ao vivo, createGoalAlert, nunca dos dados).
 * @param {{minute_label?: string, payload?: {reason?: string}}|null} annulment  o goal_annulled
 * @returns {string} "Gol anulado aos 60' — Impedimento (VAR)"
 */
export function annulledGoalNote(annulment) {
  const when = annulment?.minute_label ? ` aos ${annulment.minute_label}` : '';
  const reason = annulment?.payload?.reason;
  return `Gol anulado${when}${reason ? ` — ${reason}` : ''}`;
}

// cor do time vira variável CSS: só #RRGGBB (um valor livre como url(...) iria parar num `background`)
const HEX = /^#[0-9a-f]{6}$/i;
const hexOr = (color, fallback) => (HEX.test(color || '') ? color : fallback);
const teamStyle = (team) => ({ '--team-1': hexOr(team?.color_primary, '#12306B'), '--team-2': hexOr(team?.color_secondary, '#FFFFFF') });
const displayName = (team) => team?.name || team?.short_name || '';

/* ==========================================================================
   API pública
   ========================================================================== */

/**
 * Cria o card de um jogo (MatchOut resumido ou detalhado).
 * @param {object} match MatchOut (CONTRACT §3)
 * @param {MatchCardOptions} [opts]
 * @returns {HTMLElement} <article class="match">
 *
 * @typedef {object} MatchCardOptions
 * @property {() => number} [now] agora corrigido pelo servidor (ServerClock#nowMs)
 * @property {(matchId: number) => (void|Promise<any>)} [onExpand] chamado ao abrir o acordeão sem match.events
 * @property {boolean} [flash] (só no update) pisca o placar e destaca lances novos
 * @property {boolean} [details=true] false = sem acordeão (ex.: placar do operador)
 * @property {boolean} [showRound] mostra rodada/fase na faixa de meta
 * @property {boolean} [showCompetition] mostra a competição na faixa de meta
 */
export function createMatchCard(match, opts = {}) {
  const id = ++seq;
  const el = h('article', { class: 'match', 'data-hook': 'match' });
  const meta = h('header', { class: 'match__meta' });
  const board = h('div', { class: 'match__board', role: 'group' });
  const summary = h('div', { class: 'match__summary' });
  const tie = h('p', { class: 'match__tie' });
  el.append(meta, board, summary, tie);
  if (opts.details !== false) {
    const details = h('details', { class: 'match__details' });
    const toggle = h('summary', { class: 'match__toggle' },
      h('span', { class: 'match__toggle-closed', text: 'Lances, escalações e ficha' }),
      h('span', { class: 'match__toggle-open', text: 'Fechar detalhes' }),
      icon('chevron-down'),
    );
    const body = h('div', { class: 'match__body', id: `match-body-${id}` });
    details.append(toggle, body);
    details.addEventListener('toggle', () => onToggle(el));
    el.append(details);
  }
  STATE.set(el, { uid: id, match: null, opts: { ...opts, flash: false }, eventIds: null, requested: false, activeTab: null, failed: false });
  updateMatchCard(el, match, { flash: false });
  return el;
}

/**
 * Atualiza um card com o estado novo do jogo (substitui, sem merge).
 * Mantém o acordeão aberto e a aba ativa; com opts.flash, pisca o placar que
 * mudou e destaca os lances novos.
 * @param {HTMLElement} el
 * @param {object} match
 * @param {MatchCardOptions} [opts]
 */
export function updateMatchCard(el, match, opts = {}) {
  let st = STATE.get(el);
  if (!st) {
    st = { uid: ++seq, match: null, opts: {}, eventIds: null, requested: false, activeTab: null, failed: false };
    STATE.set(el, st);
  }
  const { flash = false, ...persistent } = opts;
  st.opts = { ...st.opts, ...persistent };
  const prev = st.match;
  const details = el.querySelector(':scope > .match__details');
  // Resposta atrasada (ex.: o detalhe pedido ao abrir chega depois de uma mensagem
  // mais nova do stream): `version` só cresce, então a versão menor é descartada.
  if (prev && prev.id === match.id && typeof match.version === 'number' && typeof prev.version === 'number' && match.version < prev.version) {
    // o detalhe veio velho: pede de novo, uma vez por versão (resposta em cache não vira laço)
    if (Array.isArray(match.events) && !Array.isArray(prev.events)) {
      if (st.retriedFor !== prev.version) {
        st.retriedFor = prev.version;
        st.requested = false;
        if (details?.open) requestDetail(el, st);
      } else {
        setMatchCardError(el); // de novo velho: mostra "Tentar de novo" em vez do esqueleto eterno
      }
    }
    return;
  }
  // detalhe já carregado continua valendo se a mensagem nova vier resumida
  if (prev && !Array.isArray(match.events) && Array.isArray(prev.events) && prev.version === match.version) {
    match = { ...prev, ...match, events: prev.events, lineups: prev.lineups, officials: prev.officials, broadcasts: prev.broadcasts, stats: prev.stats, attendance: prev.attendance, revenue_cents: prev.revenue_cents };
  }
  st.match = match;
  if (Array.isArray(match.events)) st.failed = false;
  else if (prev && Array.isArray(prev.events)) st.requested = false; // versão nova sem lances: o detalhe precisa ser pedido de novo
  const now = nowOf(st.opts);

  el.dataset.matchId = String(match.id);
  el.dataset.status = match.status;
  el.dataset.period = match.period || '';
  const winner = match.status === 'finished' && (match.winner === 'home' || match.winner === 'away') ? match.winner : '';
  if (winner) el.dataset.winner = winner;
  else delete el.dataset.winner;

  const [meta, board, summary, tie] = el.children;
  renderMeta(meta, match, now, st.opts);
  renderBoard(board, match, now);
  renderSummary(summary, match);
  renderTie(tie, match);

  if (flash && prev) flashScore(el, prev, match);

  if (details?.open) {
    if (!Array.isArray(match.events) && !st.requested && !st.failed && typeof st.opts.onExpand === 'function') requestDetail(el, st);
    renderBody(el, st, flash);
  } else if (Array.isArray(match.events)) {
    st.eventIds = null; // será redesenhado ao abrir
  }
}

/**
 * Atualiza só o minuto dos jogos ao vivo dentro de root (chame a cada segundo).
 * @param {ParentNode} root
 * @param {number} nowMs
 */
export function tickMatchCards(root, nowMs) {
  for (const el of root.querySelectorAll('.match[data-status="live"]')) {
    const st = STATE.get(el);
    const target = el.querySelector('[data-hook="minute"]');
    if (st && target) setText(target, liveMinuteLabel(st.match, nowMs));
  }
}

/** Último MatchOut desenhado no card (ou null). */
export function getCardMatch(el) {
  return STATE.get(el)?.match ?? null;
}

/**
 * Sinaliza falha ao carregar o detalhe (mostra mensagem com "Tentar de novo").
 * Útil quando onExpand não devolve Promise.
 */
export function setMatchCardError(el) {
  const st = STATE.get(el);
  if (!st) return;
  st.failed = true;
  st.requested = false;
  const details = el.querySelector(':scope > .match__details');
  if (details?.open) renderBody(el, st, false);
}

/* ==========================================================================
   Faixa de meta
   ========================================================================== */

function statusPill(match) {
  const { status } = match;
  if (status === 'scheduled') {
    const time = formatTime(match.kickoff_at);
    return h('span', { class: 'pill pill--scheduled' }, icon('clock'), h('span', { class: 'visually-hidden', text: 'Agendado para ' }), time || 'A definir');
  }
  if (status === 'live') {
    if (match.partial_info) return h('span', { class: 'pill pill--partial', text: 'Informações parciais' });
    if (match.period === 'half_time') return h('span', { class: 'pill pill--interval', text: 'Intervalo' });
    return h('span', { class: 'pill pill--live', text: match.period === 'penalties' ? 'Pênaltis' : 'Ao vivo' });
  }
  if (status === 'delayed') {
    return h('span', { class: 'pill pill--delayed', title: match.status_note || '' }, icon('clock'), 'Atrasado');
  }
  const cls = { finished: 'finished', postponed: 'postponed', suspended: 'suspended', cancelled: 'cancelled' }[status] || 'scheduled';
  return h('span', { class: `pill pill--${cls}`, text: match.status_label || status });
}

function roundLabel(match) {
  const tie = match.tie;
  if (tie) {
    const leg = tie.legs === 2 ? (tie.leg === 2 ? 'Volta' : 'Ida') : 'Jogo único';
    return `${tie.round?.name || match.round?.name || ''} · ${leg}`.replace(/^ · /, '');
  }
  return match.round?.name || '';
}

function renderMeta(meta, match, now, opts) {
  const parts = [];
  const status = h('span', { class: 'match__status' }, statusPill(match));
  if (match.status === 'live' && !match.partial_info && match.period !== 'half_time' && match.period !== 'penalties') {
    status.append(h('span', { class: 'match__minute' },
      match.period_short ? h('span', { class: 'match__minute-period', text: match.period_short }) : null,
      h('span', { 'data-hook': 'minute', text: liveMinuteLabel(match, now) }),
    ));
  } else if (match.status === 'suspended' && match.period_label) {
    status.append(h('span', { class: 'match__round', text: `parou no ${match.period_label}` }));
  }
  parts.push(status);

  const kickoff = toDate(match.kickoff_at);
  if (kickoff) {
    const text = match.status === 'scheduled' ? formatDay(kickoff, now) : formatWhen(kickoff, now);
    parts.push(h('time', { class: 'match__when', datetime: kickoff.toISOString() }, icon('calendar', { className: 'icon--sm' }), text));
  }

  const context = [opts.showCompetition && (match.competition?.short_name || match.competition?.name), opts.showRound && roundLabel(match)].filter(Boolean);
  if (context.length) parts.push(h('span', { class: 'match__round', text: context.join(' · ') }));

  if (match.venue || match.city) {
    parts.push(h('span', { class: 'match__place' },
      match.venue ? h('span', { class: 'match__place-item' }, icon('stadium'), h('span', { class: 'visually-hidden', text: 'Estádio: ' }), match.venue) : null,
      match.city ? h('span', { class: 'match__place-item' }, icon('pin'), h('span', { class: 'visually-hidden', text: 'Cidade: ' }), match.city) : null,
    ));
  }
  meta.replaceChildren(...parts);
}

/* ==========================================================================
   Placar
   ========================================================================== */

function hasScore(match) {
  return match.status === 'live' || match.status === 'finished' || match.status === 'suspended' ||
    (match.status === 'cancelled' && (match.home_score || match.away_score));
}

function teamBlock(team, side) {
  const name = h('span', { class: 'team__name', title: displayName(team) },
    h('span', { class: 'team__full', text: displayName(team) }),
    h('span', { class: 'team__short', 'aria-hidden': 'true', text: team?.short_name || displayName(team) }),
  );
  const crest = createCrest(team, { size: 48 });
  const band = h('span', { class: 'team__band', 'aria-hidden': 'true' });
  const children = side === 'home' ? [band, name, crest] : [band, crest, name];
  return h('div', { class: `team team--${side}`, style: teamStyle(team) }, ...children);
}

function boardLabel(match, now) {
  const home = displayName(match.home);
  const away = displayName(match.away);
  if (!hasScore(match)) {
    const when = match.kickoff_at ? `, ${formatWhen(match.kickoff_at, now).replace(' · ', ' às ').toLowerCase()}` : '';
    const note = match.status === 'delayed' && match.status_note ? `: ${match.status_note}` : '';
    return `${home} contra ${away}${when}. ${match.status_label || ''}${note}`.trim();
  }
  let label = `${home} ${match.home_score ?? 0} a ${match.away_score ?? 0} ${away}`;
  if (match.home_penalties != null && match.away_penalties != null) label += `, pênaltis ${match.home_penalties} a ${match.away_penalties}`;
  const minute = liveMinuteLabel(match, now);
  return `${label}. ${match.status_label || ''}${minute && match.period !== 'half_time' ? `, ${minute.replace("'", ' minutos')}` : ''}`;
}

function renderBoard(board, match, now) {
  const center = h('div', { class: 'score' });
  if (hasScore(match)) {
    center.append(h('div', { class: 'score__line', 'aria-hidden': 'true' },
      h('span', { class: 'score__n score__n--home', 'data-hook': 'score-home', text: match.home_score ?? 0 }),
      h('span', { class: 'score__sep', text: '×' }),
      h('span', { class: 'score__n score__n--away', 'data-hook': 'score-away', text: match.away_score ?? 0 }),
    ));
    if (match.home_penalties != null && match.away_penalties != null) {
      center.append(h('span', { class: 'score__pen', 'aria-hidden': 'true', text: `(${match.home_penalties}) × (${match.away_penalties}) pên.` }));
    }
  } else {
    center.append(h('span', { class: 'score__vs', 'aria-hidden': 'true', text: '×' }));
    if (match.status === 'delayed' && match.status_note) {
      center.append(h('span', { class: 'score__delay', 'aria-hidden': 'true', text: match.status_note }));
    }
  }
  board.replaceChildren(teamBlock(match.home, 'home'), center, teamBlock(match.away, 'away'));
  board.setAttribute('aria-label', boardLabel(match, now));
}

function flashScore(el, prev, match) {
  let changed = false;
  for (const side of ['home', 'away']) {
    if ((match[`${side}_score`] ?? 0) > (prev[`${side}_score`] ?? 0)) {
      el.querySelector(`[data-hook="score-${side}"]`)?.classList.add('is-flash');
      changed = true;
    }
  }
  if (changed) {
    el.classList.remove('is-goal');
    void el.offsetWidth; // reinicia a animação
    el.classList.add('is-goal');
    // animationend borbulha dos filhos (pílula, lances novos): só vale o do próprio card
    const done = (event) => {
      if (event.target !== el) return;
      el.classList.remove('is-goal');
      el.removeEventListener('animationend', done);
    };
    el.addEventListener('animationend', done);
  }
}

/* ==========================================================================
   Resumo (só os gols, sem abrir) e linha do confronto
   ========================================================================== */

function renderSummary(summary, match) {
  // Fora do acordeão, só os gols; cartões ficam nos lances.
  const goals = Array.isArray(match.goals) ? match.goals : [];
  if (!goals.length) {
    summary.hidden = true;
    summary.replaceChildren();
    return;
  }
  summary.hidden = false;
  const column = (side) => {
    const items = goals.filter((g) => g.team_side === side).map((g) => {
      const tag = ORIGIN_TAG[g.origin];
      return h('li', { class: 'facts-line__item' },
        icon(g.origin === 'own_goal' ? 'ball-own' : 'ball', { label: 'Gol' }),
        g.player || 'Gol',
        h('span', { class: 'facts-line__min', text: g.minute_label || '' }),
        tag ? h('span', { class: 'facts-line__tag', text: `(${tag})` }) : null,
      );
    });
    return h('ul', { class: `facts-line facts-line--${side}`, 'aria-label': `Gols de ${displayName(match[side])}` }, ...items);
  };
  summary.replaceChildren(column('home'), column('away'));
}

function teamById(tie, id) {
  if (tie.team_a?.id === id) return tie.team_a;
  if (tie.team_b?.id === id) return tie.team_b;
  return null;
}

function renderTie(p, match) {
  const tie = match.tie;
  if (!tie) {
    p.hidden = true;
    p.replaceChildren();
    return;
  }
  const parts = [];
  const winner = tie.winner_team_id != null ? teamById(tie, tie.winner_team_id) : null;
  const firstLeg = tie.legs === 2 && tie.leg === 1 && !tie.complete;
  if (tie.legs === 2 && !firstLeg && tie.aggregate) {
    // agregado na mesma ordem do placar do card (mandante à esquerda), não na ordem team_a/team_b
    const flip = match.home?.id != null && match.home.id === tie.team_b?.id;
    const [left, right] = flip ? [tie.team_b, tie.team_a] : [tie.team_a, tie.team_b];
    const [aggLeft, aggRight] = flip ? [tie.aggregate.team_b, tie.aggregate.team_a] : [tie.aggregate.team_a, tie.aggregate.team_b];
    parts.push(icon('trophy'), h('span', { text: tie.complete ? 'Agregado' : 'Agregado parcial' }),
      h('span', { class: 'match__tie-agg', text: `${left?.short_name || ''} ${formatScore(aggLeft, aggRight)} ${right?.short_name || ''}` }));
  } else if (firstLeg) {
    parts.push(icon('trophy'), h('span', { text: 'Jogo de ida · a vaga sai na volta' }));
  }
  if (winner && tie.complete) {
    if (!parts.length) parts.push(icon('trophy'));
    else parts.push(h('span', { class: 'divider-dot', 'aria-hidden': 'true', text: '·' }));
    const how = tie.decided_by && (tie.legs === 2 || tie.decided_by !== 'aggregate') ? ` ${tie.decided_by_label || ''}` : '';
    parts.push(h('span', null, h('strong', { text: displayName(winner) }), ' ', h('span', { class: 'tie__adv' }, icon('check'), `avança${how}`)));
  }
  p.hidden = parts.length === 0;
  p.replaceChildren(...parts);
}

/* ==========================================================================
   Acordeão: carregamento preguiçoso, abas e painéis
   ========================================================================== */

function onToggle(el) {
  const st = STATE.get(el);
  const details = el.querySelector(':scope > .match__details');
  if (!st || !details?.open) return;
  const match = st.match;
  if (!Array.isArray(match.events) && !st.requested && typeof st.opts.onExpand === 'function') {
    requestDetail(el, st);
  }
  renderBody(el, st, false);
}

function requestDetail(el, st) {
  st.requested = true;
  st.failed = false;
  let result;
  try {
    result = st.opts.onExpand(st.match.id);
  } catch {
    result = Promise.reject();
  }
  if (result && typeof result.then === 'function') {
    result.catch(() => setMatchCardError(el));
  }
}

function availableTabs(match) {
  const tabs = [];
  if (Array.isArray(match.events) && match.events.length) tabs.push('events');
  if (match.lineups && (match.lineups.home || match.lineups.away)) tabs.push('lineups');
  if (match.officials?.length || match.broadcasts?.length || match.stats?.length || match.attendance != null || match.revenue_cents != null) tabs.push('facts');
  return tabs;
}

function renderBody(el, st, flash) {
  const body = el.querySelector(':scope > .match__details > .match__body');
  const match = st.match;
  const focusedTab = body.contains(document.activeElement) ? document.activeElement.closest('[role="tab"]')?.dataset.tab : null;
  if (!Array.isArray(match.events)) {
    if (st.failed) {
      body.replaceChildren(h('div', { class: 'match__empty', role: 'alert' },
        h('p', { text: 'Não deu certo agora. Tente de novo em instantes.' }),
        h('button', { type: 'button', class: 'btn btn--secondary btn--sm', style: { 'margin-top': '8px' }, onClick: () => { requestDetail(el, st); renderBody(el, st, false); } }, icon('refresh'), 'Tentar de novo'),
      ));
    } else if (st.requested || typeof st.opts.onExpand !== 'function') {
      body.replaceChildren(detailSkeleton());
      body.setAttribute('aria-busy', 'true');
    }
    return;
  }
  body.removeAttribute('aria-busy');

  const previousIds = st.eventIds;
  const ids = new Set(match.events.map((e) => e.id));
  const newIds = flash && previousIds ? new Set([...ids].filter((id) => !previousIds.has(id))) : null;
  st.eventIds = ids;

  const tabs = availableTabs(match);
  if (!tabs.length) {
    const text = match.status === 'scheduled' ? 'Os lances aparecem aqui quando a bola rolar.' : 'Nenhum lance registrado ainda.';
    body.replaceChildren(h('p', { class: 'match__empty', text }));
    return;
  }
  if (!tabs.includes(st.activeTab)) st.activeTab = tabs[0];

  const prefix = `m${st.uid}`;
  const panels = tabs.map((key) => {
    const panel = h('div', { class: 'tabpanel', id: `${prefix}-panel-${key}`, role: tabs.length > 1 ? 'tabpanel' : null, tabindex: tabs.length > 1 ? '0' : null, 'aria-labelledby': tabs.length > 1 ? `${prefix}-tab-${key}` : null, 'data-tab': key });
    panel.hidden = key !== st.activeTab;
    if (key === 'events') panel.append(renderTimeline(match, newIds));
    else if (key === 'lineups') panel.append(renderLineups(match));
    else panel.append(renderFacts(match));
    return panel;
  });

  if (tabs.length > 1) {
    const tablist = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Detalhes do jogo' });
    for (const key of tabs) {
      const def = TABS.find((t) => t.key === key);
      const selected = key === st.activeTab;
      tablist.append(h('button', {
        type: 'button', class: 'tab', role: 'tab', id: `${prefix}-tab-${key}`, 'aria-selected': String(selected),
        'aria-controls': `${prefix}-panel-${key}`, tabindex: selected ? '0' : '-1', 'data-tab': key,
      }, icon(def.icon), def.label));
    }
    tablist.addEventListener('click', (event) => {
      const btn = event.target.closest('[role="tab"]');
      if (btn) selectTab(el, st, btn.dataset.tab, false);
    });
    tablist.addEventListener('keydown', (event) => {
      const keys = { ArrowRight: 1, ArrowLeft: -1, Home: 'first', End: 'last' };
      if (!(event.key in keys)) return;
      event.preventDefault();
      const i = tabs.indexOf(st.activeTab);
      const step = keys[event.key];
      const next = step === 'first' ? 0 : step === 'last' ? tabs.length - 1 : (i + step + tabs.length) % tabs.length;
      selectTab(el, st, tabs[next], true);
    });
    body.replaceChildren(tablist, ...panels);
    if (focusedTab) tablist.querySelector(`[data-tab="${focusedTab}"]`)?.focus();
  } else {
    body.replaceChildren(...panels);
  }
}

function selectTab(el, st, key, focus) {
  st.activeTab = key;
  const body = el.querySelector(':scope > .match__details > .match__body');
  for (const tab of body.querySelectorAll('[role="tab"]')) {
    const on = tab.dataset.tab === key;
    tab.setAttribute('aria-selected', String(on));
    tab.tabIndex = on ? 0 : -1;
    if (on && focus) tab.focus();
  }
  for (const panel of body.querySelectorAll('.tabpanel')) panel.hidden = panel.dataset.tab !== key;
}

function detailSkeleton() {
  const line = (w) => h('span', { class: 'skeleton skeleton--line', style: { width: w } });
  return h('div', { class: 'skeleton-card', 'aria-label': 'Carregando lances', role: 'status' },
    line('40%'), line('70%'), line('55%'), line('65%'),
  );
}

/* ==========================================================================
   Lances: linha do tempo de dois lados com trilho central
   ========================================================================== */

function eventPlayer(e) {
  return e.player?.name || e.payload?.player || '';
}

function timelineItem(e, side, { title, sub = [], score = null, extraClass = '', isNew = false, minuteText }) {
  const minute = minuteText ?? e.minute_label ?? '';
  const body = h('div', { class: 'tl-item__body' },
    // lance sem time (ex.: VAR) ocupa o centro e esconde o trilho: o minuto vai junto do texto
    !side && minute ? h('span', { class: 'tl-item__min-inline', text: minute }) : null,
    icon(eventIconName(e), { className: 'tl-item__icon', label: e.type_label || '' }),
    h('div', { class: 'tl-item__text' },
      h('span', { class: 'tl-item__title', text: title }),
      ...sub.filter(Boolean).map((sline) => (typeof sline === 'string' ? h('span', { class: 'tl-item__sub', text: sline }) : sline)),
    ),
  );
  // O placar do gol fica junto do minuto: "11' — 0 × 1" (empilhado em cards estreitos).
  const min = h('span', { class: ['tl-item__min', !minute && !score && 'tl-item__min--dot', score && 'tl-item__min--score'] },
    minute ? h('span', { text: minute }) : null,
    score && minute ? h('span', { class: 'tl-item__min-sep', 'aria-hidden': 'true', text: '—' }) : null,
    score ? h('span', { class: 'tl-item__min-score', text: score }) : null,
  );
  if (!minute && !score) min.setAttribute('aria-hidden', 'true');
  return h('li', {
    class: ['tl-item', side ? `tl-item--${side}` : 'tl-item--neutral', extraClass, isNew && 'is-new'],
    'data-event-id': e.id,
  }, min, body);
}

function separator(def, e, score) {
  const label = h('span', { class: 'tl-sep__label' }, icon(def.icon), def.label);
  if (def.score && score) label.append(h('span', { class: 'tl-sep__score', text: `· ${score}` }));
  return h('li', { class: ['tl-sep', def.end && 'tl-sep--end'], 'data-event-id': e.id }, label);
}

/**
 * Linha do tempo (exportada para reuso, ex.: guia de estilo).
 * @param {object} match MatchOut com events
 * @param {Set<number>|null} [newIds] ids a destacar como novos
 */
export function renderTimeline(match, newIds = null) {
  const events = [...match.events].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
  const byId = new Map(events.map((e) => [e.id, e]));
  const annulments = new Map(); // goalId → evento de anulação
  for (const e of events) {
    if (e.type === 'goal_annulled' && e.annuls_event_id != null && byId.has(e.annuls_event_id)) annulments.set(e.annuls_event_id, e);
  }
  const list = h('ol', { class: 'timeline', 'aria-label': 'Lances do jogo' });
  let score = '0 × 0';
  let shootout = { home: 0, away: 0 };
  for (const e of events) {
    const isNew = !!newIds?.has(e.id);
    const side = e.team_side === 'home' || e.team_side === 'away' ? e.team_side : null;
    if (SEPARATORS[e.type]) {
      const def = SEPARATORS[e.type];
      const shown = e.type === 'match_end' && match.home_penalties != null ? `${score} (${match.home_penalties}–${match.away_penalties} pên.)` : score;
      list.append(separator(def, e, shown));
      continue;
    }
    switch (e.type) {
      case 'goal': {
        const annul = annulments.get(e.id);
        const annulled = e.annulled || !!annul;
        if (!annulled && e.score_after) score = formatScore(e.score_after.home, e.score_after.away);
        const sub = [];
        if (annulled) {
          sub.push(h('span', { class: 'tl-item__sub tl-item__sub--alert', text: annulledGoalNote(annul) }));
        } else {
          const origin = e.payload?.origin;
          if (origin && origin !== 'open_play') sub.push(ORIGIN_LABEL[origin]);
          if (e.payload?.assist) sub.push(`Assistência: ${e.payload.assist}`);
          if (!sub.length) sub.push('Gol');
        }
        list.append(timelineItem(e, side, {
          title: eventPlayer(e) || 'Gol', sub, score: annulled ? null : (e.score_after ? formatScore(e.score_after.home, e.score_after.away) : null),
          extraClass: annulled ? 'tl-item--annulled' : 'tl-item--goal', isNew,
        }));
        break;
      }
      case 'goal_annulled': {
        if (annulments.get(e.annuls_event_id) === e) break; // já mostrado no gol riscado
        // dado, não emoção: o título já diz "Gol anulado"; a linha de baixo traz o motivo
        list.append(timelineItem(e, side, { title: 'Gol anulado', sub: [e.payload?.reason || 'Gol anulado'], isNew }));
        break;
      }
      case 'substitution': {
        const inName = e.payload?.player_in || 'Entra';
        const outName = e.payload?.player_out || '';
        list.append(timelineItem(e, side, {
          title: inName,
          sub: [h('span', { class: 'tl-item__sub tl-item__sub--in', text: '▲ entra' }), outName ? `▼ sai ${outName}` : null],
          isNew,
        }));
        break;
      }
      case 'yellow_card':
      case 'red_card': {
        const second = e.type === 'red_card' && e.payload?.reason === 'second_yellow';
        list.append(timelineItem(e, side, { title: eventPlayer(e) || e.type_label, sub: [second ? 'Segundo amarelo' : e.type_label], isNew }));
        break;
      }
      case 'penalty_awarded':
        list.append(timelineItem(e, side, { title: 'Pênalti marcado', sub: [], isNew }));
        break;
      case 'penalty_missed': {
        const how = MISS_LABEL[e.payload?.outcome];
        list.append(timelineItem(e, side, { title: eventPlayer(e) || 'Pênalti perdido', sub: [`Pênalti perdido${how ? ` · ${how}` : ''}`], isNew }));
        break;
      }
      case 'var_review': {
        const incident = e.payload?.incident || '';
        const decision = e.payload?.decision || '';
        list.append(timelineItem(e, side, { title: 'Revisão do VAR', sub: [incident, decision ? `Decisão: ${decision}` : null], isNew }));
        break;
      }
      case 'clock_adjust':
        break; // ajuste interno do relógio: não aparece nos lances do público
      case 'stoppage_time': {
        const minutes = e.payload?.minutes;
        list.append(timelineItem(e, null, { title: minutes ? `+${minutes} min de acréscimo` : 'Acréscimos', isNew, minuteText: '' }));
        break;
      }
      case 'shootout_kick': {
        const scored = e.payload?.scored !== false;
        if (side && scored) shootout = { ...shootout, [side]: shootout[side] + 1 };
        list.append(timelineItem(e, side, {
          title: eventPlayer(e) || 'Cobrança', sub: [scored ? 'Converteu' : 'Perdeu'],
          score: `${shootout.home} × ${shootout.away}`, extraClass: scored ? '' : 'tl-item--missed', isNew, minuteText: '',
        }));
        break;
      }
      default: {
        if (STATUS_EVENTS.has(e.type)) {
          const extra = e.type === 'rescheduled' && e.payload?.kickoff_at ? ` para ${formatWhen(e.payload.kickoff_at)}` : '';
          const reason = e.payload?.reason ? ` — ${e.payload.reason}` : '';
          list.append(timelineItem(e, null, { title: `${e.type_label || e.type}${extra}${reason}`, isNew, minuteText: '' }));
        } else {
          list.append(timelineItem(e, side, { title: e.type_label || e.type, sub: [], isNew }));
        }
      }
    }
  }
  return list;
}

/* ==========================================================================
   Escalações
   ========================================================================== */

function playerRow(p) {
  return h('li', { class: 'lineup__player' },
    h('span', { class: 'lineup__num', text: p.number ?? '' }),
    h('span', { class: 'lineup__name', text: p.name }),
    h('abbr', { class: 'lineup__pos', title: { GK: 'Goleiro', DF: 'Defensor', MF: 'Meio-campista', FW: 'Atacante' }[p.position] || '', text: POSITION_SHORT[p.position] || '' }),
  );
}

function renderLineups(match) {
  const col = (side) => {
    const team = match[side];
    const lineup = match.lineups?.[side];
    const head = h('div', { class: 'lineup__head' },
      h('span', { class: 'lineup__team' }, h('span', { class: 'lineup__swatch', style: teamStyle(team), 'aria-hidden': 'true' }), displayName(team)),
      lineup?.formation ? h('span', { class: 'lineup__formation', text: `(${lineup.formation})` }) : null,
    );
    if (!lineup) return h('section', { class: 'lineup' }, head, h('p', { class: 'lineup__missing', text: 'Escalação ainda não divulgada.' }));
    return h('section', { class: 'lineup', 'aria-label': `Escalação de ${displayName(team)}` },
      head,
      h('ol', { class: 'lineup__list', 'aria-label': 'Titulares' }, ...(lineup.starters || []).map(playerRow)),
      lineup.substitutes?.length ? h('h4', { class: 'lineup__subhead', text: 'Reservas' }) : null,
      lineup.substitutes?.length ? h('ol', { class: 'lineup__list lineup__list--subs', 'aria-label': 'Reservas' }, ...lineup.substitutes.map(playerRow)) : null,
      lineup.coach ? h('p', { class: 'lineup__coach' }, 'Técnico: ', h('strong', { text: lineup.coach })) : null,
    );
  };
  return h('div', { class: 'lineups' }, col('home'), col('away'));
}

/* ==========================================================================
   Ficha: arbitragem, público e renda, transmissões, estatísticas
   ========================================================================== */

function renderFacts(match) {
  const wrap = h('div');
  const blocks = [];
  if (match.officials?.length) {
    const byRole = new Map();
    for (const o of match.officials) {
      const key = o.role_label || o.role;
      if (!byRole.has(key)) byRole.set(key, []);
      byRole.get(key).push(o.state ? `${o.name} (${o.state})` : o.name);
    }
    const dl = h('dl', { class: 'fact__list' });
    for (const [role, names] of byRole) dl.append(h('dt', { text: role }), h('dd', { text: names.join(', ') }));
    blocks.push(h('section', { class: 'fact' }, icon('whistle', { className: 'fact__icon' }), h('h4', { class: 'fact__title', text: 'Arbitragem' }), dl));
  }
  if (match.attendance != null || match.revenue_cents != null) {
    const dl = h('dl', { class: 'fact__list' });
    if (match.attendance != null) dl.append(h('dt', { text: 'Público' }), h('dd', { class: 'num', text: `${formatInt(match.attendance)} pagantes` }));
    if (match.revenue_cents != null) dl.append(h('dt', { text: 'Renda' }), h('dd', { class: 'num', text: formatMoney(match.revenue_cents) }));
    blocks.push(h('section', { class: 'fact' }, icon('people', { className: 'fact__icon' }), h('h4', { class: 'fact__title', text: 'Público e renda' }), dl));
  }
  if (match.broadcasts?.length) {
    const ul = h('ul', { class: 'fact__links' });
    for (const b of match.broadcasts) {
      const href = safeHref(b.url);
      const li = h('li', null, href
        ? h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, b.name, icon('external', { label: '(abre em nova aba)' }))
        : b.name);
      if (b.kind_label) li.append(h('span', { class: 'fact__kind', text: ` · ${b.kind_label}` }));
      ul.append(li);
    }
    blocks.push(h('section', { class: 'fact' }, icon(match.broadcasts.some((b) => b.kind === 'radio') && match.broadcasts.every((b) => b.kind === 'radio') ? 'radio' : 'tv', { className: 'fact__icon' }), h('h4', { class: 'fact__title', text: 'Transmissões' }), ul));
  }
  if (blocks.length) wrap.append(h('div', { class: 'facts' }, ...blocks));
  if (match.stats?.length) wrap.append(renderStats(match));
  if (!blocks.length && match.stats?.length) wrap.lastChild.style.cssText = 'margin-top:0;border-top:0;padding-top:0';
  return wrap;
}

function renderStats(match) {
  const list = h('ul', { class: 'stats', 'aria-label': 'Estatísticas' });
  list.append(h('li', { class: 'stats__title' }, h('h4', { class: 'fact__title' }, icon('chart'), ' Estatísticas')));
  const pct = (a, b) => {
    const total = (Number(a) || 0) + (Number(b) || 0);
    return total ? Math.round(((Number(a) || 0) / total) * 100) : 0;
  };
  for (const st of match.stats) {
    const isPossession = st.key === 'possession';
    const hv = Number(st.home) || 0;
    const av = Number(st.away) || 0;
    const homeShare = isPossession ? hv : pct(hv, av);
    const awayShare = isPossession ? av : 100 - homeShare;
    const suffix = isPossession ? '%' : '';
    const label = (st.label || st.key).replace(/\s*\(%\)$/, '');
    list.append(h('li', { class: 'stat', 'aria-label': `${label}: ${displayName(match.home)} ${hv}${suffix}, ${displayName(match.away)} ${av}${suffix}` },
      h('span', { class: ['stat__value', hv >= av && 'is-lead'], 'aria-hidden': 'true', text: `${hv}${suffix}` }),
      h('span', { class: 'stat__label', 'aria-hidden': 'true', text: label }),
      h('span', { class: ['stat__value stat__value--away', av >= hv && 'is-lead'], 'aria-hidden': 'true', text: `${av}${suffix}` }),
      h('span', { class: 'stat__bars', 'aria-hidden': 'true' },
        h('span', { class: 'stat__track stat__track--home' }, h('span', { class: 'stat__fill', style: { '--w': `${homeShare}%`, '--team-color': hexOr(match.home?.color_primary, null) } })),
        h('span', { class: 'stat__track' }, h('span', { class: 'stat__fill', style: { '--w': `${awayShare}%`, '--team-color': hexOr(match.away?.color_primary, null) } })),
      ),
    ));
  }
  return list;
}

/* ==========================================================================
   Confronto de mata-mata (TieDetailOut)
   ========================================================================== */

/**
 * Card de confronto: agregado, vencedor em destaque, forma da decisão e jogos.
 * @param {object} tie TieDetailOut (TieOut sem leg + matches: [MatchOut])
 * @param {{now?: () => number}} [opts]
 * @returns {HTMLElement}
 */
export function createTieCard(tie, opts = {}) {
  const now = nowOf(opts);
  const complete = !!tie.complete && tie.winner_team_id != null;
  const started = complete || (tie.matches || []).some(hasScore);
  const row = (team, agg) => {
    const isWinner = complete && team?.id === tie.winner_team_id;
    return h('li', { class: ['tie__team', isWinner && 'is-winner', complete && !isWinner && 'is-loser'] },
      createCrest(team, { size: 28 }),
      h('span', { class: 'tie__name' }, displayName(team), isWinner ? h('span', { class: 'tie__adv' }, icon('check'), 'avança') : null),
      h('span', { class: 'tie__agg', text: started ? (agg ?? 0) : '–' }),
    );
  };
  const format = tie.legs === 2 ? 'Ida e volta' : 'Jogo único';
  const legs = (tie.matches || []).slice().sort((a, b) => (a.tie?.leg ?? 0) - (b.tie?.leg ?? 0) || String(a.kickoff_at).localeCompare(String(b.kickoff_at)));
  const card = h('article', { class: 'card tie', 'data-tie-id': tie.id },
    h('header', { class: 'tie__head' },
      h('span', { class: 'tie__round', text: tie.round?.name || 'Confronto' }),
      h('span', { class: 'tie__format', text: `${format}${tie.extra_time ? ' · com prorrogação' : ''}` }),
    ),
    h('ol', { class: 'tie__teams', 'aria-label': 'Agregado do confronto' }, row(tie.team_a, tie.aggregate?.team_a), row(tie.team_b, tie.aggregate?.team_b)),
  );
  if (complete && tie.decided_by) {
    const winner = teamById(tie, tie.winner_team_id);
    card.append(h('p', { class: 'tie__decided', text: `${displayName(winner)} classificado ${tie.decided_by_label || ''}`.trim() }));
  }
  if (legs.length) {
    card.append(h('ul', { class: 'tie__legs', 'aria-label': 'Jogos do confronto' }, ...legs.map((m, i) => {
      const label = tie.legs === 2 ? ((m.tie?.leg ?? i + 1) === 2 ? 'Volta' : 'Ida') : 'Jogo';
      const scoreText = hasScore(m) ? `${m.home?.short_name} ${formatScore(m.home_score, m.away_score)} ${m.away?.short_name}` : `${m.home?.short_name} × ${m.away?.short_name}`;
      return h('li', { class: 'tie__leg' },
        h('span', { class: 'tie__leg-label', text: label }),
        h('span', { class: 'tie__leg-score', text: scoreText }),
        m.home_penalties != null ? h('span', { text: `(${m.home_penalties}–${m.away_penalties} pên.)` }) : null,
        h('span', { class: 'muted', text: m.status === 'live' ? (m.status_label || 'Ao vivo') : formatWhen(m.kickoff_at, now) }),
      );
    })));
  }
  return card;
}

/* ==========================================================================
   Últimos gols e aviso de gol (home)
   ========================================================================== */

function matchScoreLine(goal) {
  const m = goal.match || {};
  const s = goal.score_after || { home: 0, away: 0 };
  const isHome = goal.team_side === 'home';
  return h('span', { class: 'goal-chip__match' },
    createCrest(m.home, { size: 16 }),
    h('span', { class: isHome ? 'is-scorer' : null, text: m.home?.short_name || '' }),
    h('span', { text: formatScore(s.home, s.away) }),
    h('span', { class: !isHome ? 'is-scorer' : null, text: m.away?.short_name || '' }),
    createCrest(m.away, { size: 16 }),
  );
}

/**
 * Item da lista "Últimos gols" (LatestGoalOut).
 * @param {object} goal LatestGoalOut
 * @param {{isNew?: boolean}} [opts]
 * @returns {HTMLLIElement}
 */
export function createLatestGoal(goal, { isNew = false } = {}) {
  const m = goal.match || {};
  const s = goal.score_after || { home: 0, away: 0 };
  const tag = ORIGIN_TAG[goal.origin];
  const label = `${goal.minute_label || ''} — ${goal.player || 'Gol'} (${displayName(goal.team)}). ${displayName(m.home)} ${s.home} a ${s.away} ${displayName(m.away)}`;
  return h('li', { class: ['goal-chip', isNew && 'is-new'], 'data-event-id': goal.event_id, 'aria-label': label },
    h('span', { class: 'goal-chip__min', 'aria-hidden': 'true', text: goal.minute_label || '' }),
    h('span', { class: 'goal-chip__player', 'aria-hidden': 'true' }, goal.player || 'Gol', tag ? h('small', { text: ` (${tag})` }) : null),
    matchScoreLine(goal),
  );
}

function teamNames(team) {
  return [h('span', { class: 'goal-alert__full', text: displayName(team) }), h('abbr', { class: 'goal-alert__short', title: displayName(team), text: team?.short_name || displayName(team) })];
}

/**
 * Aviso de gol ("É gol!") ou de correção ("Oxe! Gol anulado.") para a região aria-live.
 * @param {object} goal LatestGoalOut
 * @param {{kind?: 'goal'|'correction', reason?: string, onClose?: Function}} [opts]
 * @returns {HTMLElement}
 */
export function createGoalAlert(goal, { kind = 'goal', reason = '', onClose = null } = {}) {
  const m = goal.match || {};
  const s = goal.score_after || { home: 0, away: 0 };
  const isGoal = kind === 'goal';
  const title = isGoal ? 'É gol!' : (reason === 'voided' ? 'Lance corrigido.' : 'Oxe! Gol anulado.');
  const close = h('button', { type: 'button', class: 'btn btn--ghost btn--icon btn--sm', 'aria-label': 'Fechar aviso' }, icon('close'));
  // Na correção, score_after é o placar do gol que caiu: aparece riscado, nunca como placar atual.
  const score = isGoal
    ? h('span', { text: formatScore(s.home, s.away) })
    : h('s', { class: 'goal-alert__void-score' }, h('span', { class: 'visually-hidden', text: 'placar que não vale mais: ' }), formatScore(s.home, s.away));
  const el = h('div', { class: ['goal-alert', !isGoal && 'goal-alert--correction'], role: 'group', 'aria-label': title, 'data-event-id': goal.event_id },
    h('p', { class: 'goal-alert__title', text: title }),
    h('div', { class: 'goal-alert__text' },
      h('p', { class: 'goal-alert__score' },
        createCrest(m.home, { size: 22 }), teamNames(m.home), score, teamNames(m.away), createCrest(m.away, { size: 22 }),
      ),
      h('p', { class: 'goal-alert__who' }, isGoal
        ? [h('strong', { text: goal.player || 'Gol' }), ` · ${goal.minute_label || ''} · ${displayName(goal.team)}`]
        : [`Gol de ${goal.player || ''} (${goal.minute_label || ''}) não vale mais. `, 'Lance corrigido pelo operador.']),
    ),
    close,
  );
  close.addEventListener('click', () => { el.remove(); onClose?.(); });
  return el;
}
