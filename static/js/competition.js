/**
 * Página da competição (competition.html?slug=): jogos por rodada, classificação
 * com legenda e critérios; no mata-mata, os confrontos da rodada com agregado e
 * vencedor (docs/PLANO.md "Página da competição", docs/FRONTEND.md §4).
 *
 * Abre na fase e na rodada atuais; ‹ › trocam a rodada (GET /api/matches?roundId=; no
 * mata-mata, GET /api/competitions/:slug?stage=&round=, que traz os confrontos com todos
 * os jogos) e o <select> troca a fase (GET /api/competitions/:slug?stage=), atualizando a URL
 * sem recarregar. Assina o stream como a home (esta página não alerta gols).
 */
import { h, renderCompetitionNav, showToast } from './render.js';
import { ServerClock, mountClock } from './clock.js';
import { createMatchCard, updateMatchCard, tickMatchCards, getCardMatch, createTieGroup, tiesFromMatches, syncTieCards, refreshMatchCards, sortMatchesForDisplay, groupMatchesByGroup, createMatchGroup } from './match-card.js';
import { createStandings, updateStandings } from './standings.js';
import { getCompetitions, getCompetition, listMatches, getMatch, getRanking } from './api.js';
import { createStream, liveStatusIndicator } from './stream.js';

const $ = (id) => document.getElementById(id);
const hook = (name) => document.querySelector(`[data-hook="${name}"]`);
const els = {
  nav: document.querySelector('#competitions-nav [data-hook="competition-links"]'),
  clock: $('brasilia-clock'),
  liveStatus: $('live-status'),
  hero: document.querySelector('.comp-hero'),
  name: hook('competition-name'),
  season: hook('season'),
  eyebrow: hook('competition-eyebrow'),
  stageSelect: $('stage-select'),
  prev: $('round-prev'),
  next: $('round-next'),
  roundLabel: $('round-label'),
  grid: $('competition-grid'),
  aside: document.querySelector('#competition-grid [data-hook="aside"]'),
  matches: $('round-matches'),
  roundEmpty: $('round-empty'),
  standings: $('stage-standings'),
  standingsSwitch: $('standings-switch'),
  rankingStandings: $('ranking-standings'),
  ties: $('stage-ties'),
  tiesList: document.querySelector('#stage-ties [data-hook="ties-list"]'),
  missing: $('competition-missing'),
  error: $('competition-error'),
};

const BRAND = document.title.split(' · ')[0];
const params = new URLSearchParams(window.location.search);
const clock = new ServerClock();
const now = () => clock.nowMs();
const state = {
  slug: (params.get('slug') || '').trim(),
  data: null,
  stage: null, // {id, name, format}
  rounds: [],
  roundIndex: -1,
  cards: new Map(), // match id → card
  standingsEl: null,
  standingsStageId: null,
  standingsData: null, // StageStandingsOut da fase exibida
  groupTables: new Map(), // fase de grupos: group id → tabela do grupo, ao lado dos jogos dele
  view: 'stage', // 'stage' (tabela da fase) ou o id da classificação geral/personalizada exibida
  rankingStageIds: [], // fases que entram na classificação exibida (atualiza com o stream)
  rankingTimer: 0,
  rankingToken: 0,
  rankingPending: null, // fases que mudaram durante a carga da classificação (null: sem carga)
  rankingMatchKeys: new Map(), // match id → {key, stageId}: o que conta na classificação (último visto pelo stream)
  seq: 0, // ignora respostas fora de ordem (troca rápida de rodada/fase)
};
let stream = null;
let clockMounted = false;

const toInt = (value) => (value != null && /^\d+$/.test(String(value)) ? Number(value) : null);

function sync(data) {
  if (data?.server_time) clock.sync(data.server_time);
  if (!clockMounted && els.clock && clock.synced) {
    mountClock(els.clock, clock);
    clockMounted = true;
  }
}

/* --- Estados da página ---------------------------------------------------------------- */

function showMissing() {
  els.hero.hidden = true;
  els.grid.hidden = true;
  els.error.hidden = true;
  els.missing.hidden = false;
  document.title = `${BRAND} · Competição não encontrada`;
}

function showError() {
  els.grid.hidden = true;
  els.missing.hidden = true;
  els.error.hidden = false;
}

/* --- Desenho ----------------------------------------------------------------------------- */

function loadDetail(matchId) {
  return getMatch(matchId).then((data) => {
    sync(data);
    const card = state.cards.get(matchId);
    if (card && data?.match) updateMatchCard(card, data.match);
  });
}

function cardFor(match, next) {
  let card = state.cards.get(match.id);
  if (card) updateMatchCard(card, match);
  else card = createMatchCard(match, { now, onExpand: loadDetail, showRound: !!match.tie }); // mata-mata: "Semifinal · Ida"
  next.set(match.id, card);
  return card;
}

/** Fase de grupos com a tabela da fase exibida: cada grupo numa linha, com os jogos dele à
 *  esquerda e a tabela dele à direita, topo alinhado (a tabela sai do aside). */
function groupRowsMode() {
  return state.stage?.format === 'groups' && state.view === 'stage' && !!state.standingsData?.groups?.length;
}

const teamIds = (groups) => new Set(groups.flatMap((g) => (g.rows || []).map((r) => r.team?.id)).filter((id) => id != null));

/** Tabela do grupo `index`, desenhada sozinha ao lado dos jogos dele: embaixo, a legenda das
 *  cores e as punições dos times dele; o último grupo leva também os critérios de desempate
 *  (uma vez, dentro do card) e a punição de time fora dos grupos.
 *  @returns {[object, object]} [StageStandingsOut só com o grupo, opções da tabela] */
function groupTableArgs(standings, index) {
  const group = standings.groups[index];
  const last = index === standings.groups.length - 1;
  const mine = teamIds([group]);
  const anywhere = teamIds(standings.groups);
  const adjustments = (standings.adjustments || []).filter((a) => mine.has(a.team?.id) || (last && !anywhere.has(a.team?.id)));
  const tied = standings.groups.some((g) => (g.rows || []).some((r) => r.tied));
  return [{ ...standings, groups: [group], adjustments }, { criteria: last, tied }];
}

/** Linhas dos grupos: título, jogos da rodada (ou o aviso) e a tabela do grupo. Jogo de grupo
 *  sem tabela (ex.: sem grupo) fica numa linha sem tabela, no fim. */
function groupRows(matches, card) {
  const standings = state.standingsData;
  const byGroup = new Map(groupMatchesByGroup(matches).map((g) => [g.group?.id ?? null, g]));
  const row = (group, games, table) => h('section', { class: 'group-row', 'data-group-id': group?.id ?? null },
    h('h3', { class: 'match-group__title', text: group?.name || 'Sem grupo' }),
    games.length
      ? h('div', { class: 'match-group__games' }, ...games.map(card))
      : h('p', { class: 'group-row__empty', text: 'Não há jogos deste grupo nesta rodada.' }),
    table ? h('div', { class: 'group-row__table' }, table) : null,
  );
  state.groupTables = new Map();
  const rows = standings.groups.map((g, i) => {
    const games = byGroup.get(g.id)?.matches || [];
    byGroup.delete(g.id);
    const table = createStandings(...groupTableArgs(standings, i));
    state.groupTables.set(g.id, table);
    return row({ id: g.id, name: g.name }, games, table);
  });
  rows.push(...[...byGroup.values()].map((g) => row(g.group, g.matches, null)));
  if (!els.standingsSwitch.hidden) rows.unshift(h('div', { class: 'group-row group-row--head' }, els.standingsSwitch));
  return rows;
}

/** Fora das linhas de grupo: os botões das classificações voltam para o aside. */
function leaveGroupRows() {
  els.matches.classList.remove('group-rows');
  if (els.standingsSwitch.parentElement !== els.aside) els.aside.prepend(els.standingsSwitch);
  state.groupTables = new Map();
}

/** Cards da rodada na ordem da home; na fase de grupos, separados por grupo. */
function matchBlocks(matches, card) {
  if (groupRowsMode()) {
    els.matches.classList.add('group-rows');
    return groupRows(matches, card);
  }
  leaveGroupRows();
  if (state.stage?.format !== 'groups') return sortMatchesForDisplay(matches).map(card);
  return groupMatchesByGroup(matches).map((g) => createMatchGroup(g.group, g.matches.map(card)));
}

function renderMatches(matches, ties = null) {
  const next = new Map();
  const knockout = state.stage?.format === 'knockout';
  let blocks;
  if (knockout) {
    // Um bloco por confronto: "Time A × Time B" e os jogos (ida e volta, mesmo de outra
    // rodada); o agregado e quem avança vêm no card da volta.
    const sorted = (ties || tiesFromMatches(matches)).slice().sort((a, b) => (a.position ?? 0) - (b.position ?? 0) || a.id - b.id);
    blocks = sorted.map((tie) => {
      const legs = (tie.matches?.length ? tie.matches : matches.filter((m) => m.tie?.id === tie.id))
        .slice().sort((a, b) => (a.tie?.leg ?? 0) - (b.tie?.leg ?? 0) || String(a.kickoff_at).localeCompare(String(b.kickoff_at)));
      return createTieGroup(tie, legs.map((m) => cardFor(m, next)));
    });
    leaveGroupRows();
  } else {
    blocks = matchBlocks(matches, (match) => cardFor(match, next)); // mesma ordem da home
  }
  state.cards = next;
  els.matches.classList.toggle('tie-groups', knockout);
  els.matches.classList.toggle('match-groups', state.stage?.format === 'groups');
  els.matches.replaceChildren(...blocks);
  els.matches.removeAttribute('aria-busy');
  els.matches.hidden = blocks.length === 0;
  els.roundEmpty.hidden = blocks.length > 0;
  els.ties.hidden = true; // o agregado fica no card do jogo de volta
  els.tiesList.replaceChildren();
  updateAside();
}

function renderStandings(stage) {
  state.standingsData = stage.standings || null;
  if (stage.standings) {
    if (state.standingsEl && state.standingsStageId === stage.id) {
      updateStandings(state.standingsEl, stage.standings);
    } else {
      state.standingsEl = createStandings(stage.standings);
      state.standingsStageId = stage.id;
      els.standings.replaceChildren(state.standingsEl);
    }
    els.standings.hidden = false;
  } else {
    state.standingsEl = null;
    state.standingsStageId = null;
    els.standings.replaceChildren();
    els.standings.hidden = true;
  }
}

/* --- Classificações gerais e personalizadas (botões ao lado da tabela da fase) ------------ */

/** Classificações com botão: as da competição e as da fase exibida (sem repetir). */
function rankingRefs(data) {
  const seen = new Set();
  return [...(data.rankings || []), ...(data.stage?.rankings || [])].filter((r) => !seen.has(r.id) && seen.add(r.id));
}

function paintStandingsSwitch() {
  const data = state.data || {};
  const refs = rankingRefs(data);
  const hasStage = !!data.stage?.standings;
  if (!refs.length) {
    state.view = 'stage';
  } else if (state.view !== 'stage' ? !refs.some((r) => r.id === state.view) : !hasStage) {
    state.view = hasStage ? 'stage' : refs[0].id;
  }
  const button = (view, text) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn--secondary btn--sm btn--toggle';
    b.textContent = text;
    b.setAttribute('aria-pressed', String(state.view === view));
    b.addEventListener('click', () => {
      if (state.view === view) return;
      state.view = view;
      paintStandingsSwitch();
      if (state.stage?.format === 'groups') reorderCards(); // linhas de grupo × jogos por grupo
    });
    return b;
  };
  const buttons = [...(hasStage ? [button('stage', data.stage.name || 'Fase')] : []), ...refs.map((r) => button(r.id, r.name))];
  els.standingsSwitch.replaceChildren(...buttons);
  els.standingsSwitch.hidden = buttons.length < 2 && state.view === 'stage';
  els.standings.hidden = state.view !== 'stage' || !hasStage || groupRowsMode(); // grupos: tabela ao lado dos jogos
  els.rankingStandings.hidden = state.view === 'stage';
  if (state.view !== 'stage') loadRanking(state.view);
  else state.rankingStageIds = [];
  updateAside();
}

async function loadRanking(id) {
  const token = ++state.rankingToken;
  const pending = new Set();
  state.rankingPending = pending;
  try {
    const data = await getRanking(id);
    if (token !== state.rankingToken || state.view !== id) return; // trocou de botão no meio
    state.rankingStageIds = data.stage_ids || [];
    els.rankingStandings.replaceChildren(createStandings(data));
  } catch {
    if (token === state.rankingToken) showToast('Não deu para carregar a classificação agora.', { kind: 'error' });
  } finally {
    // a resposta pode ter vindo antes do que mudou no meio da carga: busca de novo
    if (token === state.rankingToken) {
      state.rankingPending = null;
      if ([...pending].some(rankingHas)) scheduleRanking();
    }
  }
}

function updateAside() {
  const hasAside = !els.standings.hidden || !els.ties.hidden || !els.rankingStandings.hidden;
  els.grid.classList.toggle('split--no-aside', !hasAside);
  // mata-mata no celular: o resumo dos confrontos (quem avança) vem antes dos cards
  els.grid.classList.toggle('split--aside-first', !els.ties.hidden);
  els.aside.hidden = !hasAside;
}

function paintRoundNav() {
  const round = state.rounds[state.roundIndex];
  els.roundLabel.textContent = round ? round.name || `Rodada ${round.number}` : 'Sem rodadas';
  els.prev.disabled = state.roundIndex <= 0;
  els.next.disabled = state.roundIndex < 0 || state.roundIndex >= state.rounds.length - 1;
}

function writeUrl() {
  const search = new URLSearchParams({ slug: state.slug });
  if (state.stage) search.set('stage', String(state.stage.id));
  const round = state.rounds[state.roundIndex];
  if (round) search.set('round', String(round.id));
  const url = `${window.location.pathname}?${search}`;
  if (url !== `${window.location.pathname}${window.location.search}`) window.history.replaceState(null, '', url);
}

/** Rodada exibida: a pedida, senão a atual da competição, senão a dos jogos devolvidos, senão a última. */
function displayedRoundIndex(data, requestedRoundId) {
  const ids = state.rounds.map((r) => r.id);
  const candidates = [
    requestedRoundId,
    data.current_round_id,
    data.stage?.matches?.[0]?.round?.id,
    data.stage?.ties?.[0]?.round?.id,
  ];
  for (const id of candidates) {
    const index = id != null ? ids.indexOf(id) : -1;
    if (index >= 0) return index;
  }
  return ids.length - 1;
}

function render(data, requestedRoundId = null) {
  state.data = data;
  const comp = data.competition || {};
  els.name.textContent = comp.name || '';
  els.season.textContent = data.season ? data.season.label || String(data.season.year || '') : ''; // "2026" ou "2026/2027"
  if (els.eyebrow) els.eyebrow.textContent = comp.short_name && comp.short_name !== comp.name ? comp.short_name : 'Competição';
  document.title = `${BRAND} · ${comp.name || 'Competição'}`;

  const stages = (data.stages || []).slice().sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
  const stageMeta = stages.find((s) => s.id === data.stage?.id) || null;
  state.stage = data.stage ? { id: data.stage.id, name: data.stage.name, format: data.stage.format } : null;
  state.rounds = stageMeta?.rounds || [];
  state.roundIndex = displayedRoundIndex(data, requestedRoundId);

  els.stageSelect.replaceChildren(...stages.map((s) => {
    const option = document.createElement('option');
    option.value = String(s.id);
    option.textContent = s.name;
    option.selected = s.id === state.stage?.id;
    return option;
  }));
  els.stageSelect.disabled = stages.length < 2;

  els.hero.hidden = false;
  els.grid.hidden = false;
  els.missing.hidden = true;
  els.error.hidden = true;
  paintRoundNav();
  renderStandings(data.stage || {});
  paintStandingsSwitch();
  renderMatches(data.stage?.matches || [], data.stage?.format === 'knockout' ? data.stage?.ties || null : null);
  writeUrl();
}

/* --- Navegação ----------------------------------------------------------------------------- */

async function fetchCompetition({ stage = null, round = null } = {}) {
  try {
    return await getCompetition(state.slug, { stage, round });
  } catch (error) {
    // fase/rodada da URL que não existe mais: tenta a competição sem elas
    if (error?.status === 404 && (stage != null || round != null)) return getCompetition(state.slug);
    throw error;
  }
}

async function goToRound(index) {
  const round = state.rounds[index];
  if (!round || index === state.roundIndex) return;
  const previous = state.roundIndex;
  const seq = ++state.seq;
  state.roundIndex = index;
  paintRoundNav();
  els.matches.setAttribute('aria-busy', 'true');
  try {
    if (state.stage?.format === 'knockout') {
      // mata-mata: a leitura da competição traz os confrontos da rodada com todos os jogos
      // (o jogo de ida pode estar em outra rodada)
      const data = await getCompetition(state.slug, { stage: state.stage.id, round: round.id });
      if (seq !== state.seq) return;
      sync(data);
      renderMatches(data?.stage?.matches || [], data?.stage?.ties || []);
    } else {
      const data = await listMatches({ roundId: round.id });
      if (seq !== state.seq) return;
      sync(data);
      renderMatches(data?.matches || []);
    }
    writeUrl();
  } catch {
    if (seq !== state.seq) return;
    state.roundIndex = previous;
    paintRoundNav();
    els.matches.removeAttribute('aria-busy');
    showToast('Não deu certo agora. Tente de novo em instantes.', { kind: 'error' });
  }
}

async function goToStage(stageId) {
  const seq = ++state.seq;
  els.stageSelect.disabled = true;
  els.matches.setAttribute('aria-busy', 'true');
  try {
    const data = await fetchCompetition({ stage: stageId });
    if (seq !== state.seq) return;
    sync(data);
    render(data);
  } catch {
    if (seq !== state.seq) return;
    els.stageSelect.value = String(state.stage?.id ?? '');
    els.stageSelect.disabled = false;
    els.matches.removeAttribute('aria-busy');
    showToast('Não deu certo agora. Tente de novo em instantes.', { kind: 'error' });
  }
}

/** Estado inteiro de novo (5 min sem stream): mesma fase e rodada; devolve o cursor. */
async function reload() {
  state.rankingMatchKeys.clear(); // as mensagens perdidas não voltam: o último visto pode estar velho
  const round = state.rounds[state.roundIndex];
  const seq = ++state.seq;
  const data = await fetchCompetition({ stage: state.stage?.id ?? null, round: round?.id ?? null });
  if (seq === state.seq) {
    sync(data);
    render(data, round?.id ?? null);
  }
  return data.cursor ?? null;
}

/* --- Stream ------------------------------------------------------------------------------------ */

/** Status mudou ao vivo: reordena os cards da rodada (a ordem depende do status). */
function reorderCards() {
  const matches = [...state.cards.values()].map(getCardMatch).filter(Boolean);
  els.matches.replaceChildren(...matchBlocks(matches, (m) => state.cards.get(m.id)));
}

/** A classificação exibida soma esta fase? */
const rankingHas = (stageId) => state.view !== 'stage' && state.rankingStageIds.includes(stageId);

/** Busca a classificação exibida de novo (agrupado: um lance publica várias mensagens). */
function scheduleRanking() {
  clearTimeout(state.rankingTimer);
  state.rankingTimer = setTimeout(() => state.view !== 'stage' && loadRanking(state.view), 1500);
}

/** Mudou o que conta nestas fases. Com a carga em andamento, as fases somadas ainda não
 *  chegaram (ou são as da classificação anterior): guarda e confere ao fim da carga. */
function rankingChanged(stageIds) {
  if (state.rankingPending) stageIds.forEach((id) => state.rankingPending.add(id));
  else if (stageIds.some(rankingHas)) scheduleRanking();
}

/** O mata-mata não publica `standings`: a partida de uma fase somada pela classificação
 *  exibida pede nova busca quando muda o que conta nela (status, placar, cartões, times,
 *  fase). Trocada de fase no admin, confere a antiga e a nova (sai de uma, entra na outra).
 *  Guarda o último visto de toda partida, mesmo fora da classificação exibida: ao voltar
 *  para ela (ou trocar de classificação), a comparação parte do estado certo. */
function rankingOnMatch(stageId, match) {
  const key = JSON.stringify([stageId, match.status, match.home_score, match.away_score, match.home?.id, match.away?.id, match.cards ?? null]);
  const seen = state.rankingMatchKeys.get(match.id);
  if (seen?.key === key) return;
  state.rankingMatchKeys.set(match.id, { key, stageId });
  rankingChanged(seen && seen.stageId !== stageId ? [seen.stageId, stageId] : [stageId]);
}

function onMatch(message) {
  const match = message?.match;
  if (!match) return;
  rankingOnMatch(message.stage_id, match);
  const card = state.cards.get(match.id);
  const before = card ? getCardMatch(card)?.status : null;
  if (card) updateMatchCard(card, match, { flash: true });
  if (card && before && before !== match.status && state.stage?.format !== 'knockout') reorderCards();
  // mata-mata: agregado e vencedor vêm no TieOut da partida e valem para o card do outro jogo
  syncTieCards(state.cards.values(), match);
}

function onStandings(message) {
  rankingChanged([message?.stage_id]);
  if (state.standingsEl && message?.stage_id === state.standingsStageId && message.standings) {
    updateStandings(state.standingsEl, message.standings);
    state.standingsData = message.standings;
    if (groupRowsMode()) updateGroupTables(message.standings);
  }
}

/** Tabelas ao lado dos jogos de cada grupo: os mesmos grupos redesenham no lugar (os cards
 *  não saem do lugar); grupo novo ou removido refaz as linhas. */
function updateGroupTables(standings) {
  const ids = standings.groups.map((g) => g.id);
  if (ids.length !== state.groupTables.size || ids.some((id) => !state.groupTables.has(id))) {
    reorderCards();
    return;
  }
  standings.groups.forEach((g, i) => updateStandings(state.groupTables.get(g.id), ...groupTableArgs(standings, i)));
}

/* --- Início ---------------------------------------------------------------------------------- */

async function boot() {
  els.error.hidden = true;
  if (!state.slug) {
    showMissing();
    getCompetitions().then((r) => renderCompetitionNav(els.nav, r?.competitions || []), () => renderCompetitionNav(els.nav, []));
    return;
  }
  const requestedRound = toInt(params.get('round'));
  const [compsResult, dataResult] = await Promise.allSettled([
    getCompetitions(),
    fetchCompetition({ stage: toInt(params.get('stage')), round: requestedRound }),
  ]);
  if (els.nav) renderCompetitionNav(els.nav, compsResult.status === 'fulfilled' ? compsResult.value?.competitions || [] : [], { activeSlug: state.slug });
  if (dataResult.status !== 'fulfilled') {
    if (dataResult.reason?.status === 404) showMissing();
    else showError();
    return;
  }
  const data = dataResult.value;
  sync(data);
  render(data, requestedRound);

  stream = createStream({
    clock,
    handlers: { match: onMatch, standings: onStandings },
    onStatus: liveStatusIndicator(els.liveStatus),
    onStale: reload,
  });
  stream.start(data.cursor ?? null);

  clock.onTick((ms) => tickMatchCards(els.matches, ms));
  document.addEventListener('minuteformatchange', () => refreshMatchCards(els.matches)); // formato do minuto trocado
  // virada do dia: "Hoje"/"Amanhã" dos cards mudam
  clock.onDayChange(() => {
    for (const card of state.cards.values()) {
      const match = getCardMatch(card);
      if (match) updateMatchCard(card, match);
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') clock.checkDay();
  });
}

els.prev.addEventListener('click', () => goToRound(state.roundIndex - 1));
els.next.addEventListener('click', () => goToRound(state.roundIndex + 1));
els.stageSelect.addEventListener('change', () => {
  const id = toInt(els.stageSelect.value);
  if (id != null && id !== state.stage?.id) goToStage(id);
});
els.error.querySelector('[data-hook="competition-retry"]')?.addEventListener('click', () => {
  if (stream) {
    reload().then((cursor) => stream.restart(cursor)).catch(() => showToast('Não deu certo agora. Tente de novo em instantes.', { kind: 'error' }));
  } else {
    boot();
  }
});

boot();
