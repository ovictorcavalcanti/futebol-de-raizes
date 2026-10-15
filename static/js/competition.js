/**
 * Página da competição (competition.html?slug=): jogos por rodada, classificação
 * com legenda e critérios; no mata-mata, os confrontos da rodada com agregado e
 * vencedor (docs/PLANO.md "Página da competição", docs/FRONTEND.md §4).
 *
 * Abre na fase e na rodada atuais; ‹ › trocam a rodada (GET /api/matches?roundId=; no
 * mata-mata, GET /api/competitions/:slug?stage=&round=, que traz os confrontos com todos
 * os jogos) e o <select> troca a fase (GET /api/competitions/:slug?stage=), atualizando a URL
 * sem recarregar. A tabela da fase fica ao lado dos jogos, com o topo alinhado ao do primeiro
 * jogo; as outras classificações (geral, melhores segundos) ficam no menu "Classificações" e
 * a escolhida ocupa o lugar dos jogos, na largura toda (&tabela=<id> na URL; voltar e avançar
 * do navegador restauram). Assina o stream como a home (esta página não alerta gols). O menu
 * marca as competições com jogo ao vivo (GET /api/matches?status=live, atualizado pelo stream).
 */
import { h, renderCompetitionNav, showToast } from './render.js';
import { icon } from './icons.js';
import { ServerClock, mountClock } from './clock.js';
import { createMatchCard, updateMatchCard, tickMatchCards, getCardMatch, createTieGroup, tiesFromMatches, syncTieCards, refreshMatchCards, sortMatchesForDisplay } from './match-card.js';
import { createStandings, updateStandings } from './standings.js';
import { createGroupRows, updateGroupRows, createMatchGroupBlocks } from './group-rows.js';
import { getCompetitions, getCompetition, listMatches, getMatch, getRanking } from './api.js';
import { createStream, liveStatusIndicator } from './stream.js';
import { shareResultsOnWhatsApp } from './share.js';

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
  roundNav: hook('round-nav'),
  prev: $('round-prev'),
  next: $('round-next'),
  roundLabel: $('round-label'),
  roundBar: $('round-bar'),
  roundBarLabel: $('round-bar-label'),
  share: $('round-share'),
  rankingsMenu: $('rankings-menu'),
  rankingsToggle: $('rankings-toggle'),
  rankingsChosen: hook('rankings-chosen'),
  rankingsList: $('rankings-list'),
  grid: $('competition-grid'),
  aside: document.querySelector('#competition-grid [data-hook="aside"]'),
  matches: $('round-matches'),
  roundEmpty: $('round-empty'),
  standings: $('stage-standings'),
  rankingView: $('ranking-view'),
  rankingBack: $('ranking-back'),
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
  groupLayout: null, // fase de grupos em linhas: {rows, tables, key} de createGroupRows
  view: null, // null: os jogos (com a tabela da fase); id: a classificação do menu, no lugar deles
  rankingStageIds: [], // fases que entram na classificação exibida (atualiza com o stream)
  rankingTimer: 0,
  rankingToken: 0,
  rankingPending: null, // fases que mudaram durante a carga da classificação (null: sem carga)
  pending: null, // troca de rodada/fase em andamento: mensagens do stream recebidas no meio
  rankingMatchKeys: new Map(), // match id → {key, stageId}: o que conta na classificação (último visto pelo stream)
  seq: 0, // ignora respostas fora de ordem (troca rápida de rodada/fase)
  competitions: [], // menu (GET /api/competitions)
  liveMatches: new Map(), // match id → slug da competição: jogos ao vivo agora, de todas (o ponto do menu)
  liveKey: null,
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
  els.roundBar.hidden = true;
  els.grid.hidden = true;
  els.rankingView.hidden = true;
  els.error.hidden = true;
  els.missing.hidden = false;
  document.title = `${BRAND} · Competição não encontrada`;
}

function showError() {
  els.roundBar.hidden = true;
  els.grid.hidden = true;
  els.rankingView.hidden = true;
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
  else card = createMatchCard(match, { now, onExpand: loadDetail, showRound: match.tie ? 'leg' : false }); // mata-mata: a navegação já diz a fase; o card, "Ida"
  next.set(match.id, card);
  return card;
}

/** Fase de grupos com tabela: cada grupo numa linha, com os jogos dele à esquerda e a tabela
 *  dele à direita, topo alinhado com o primeiro jogo (a tabela sai do aside). */
function groupRowsMode() {
  return state.stage?.format === 'groups' && !!state.standingsData?.groups?.length;
}

/** Rodada da tabela: a atual da competição, se é desta fase; senão a exibida. */
function standingsRound() {
  const round = state.rounds.find((r) => r.id === state.data?.current_round_id) || state.rounds[state.roundIndex];
  return round ? round.name || `Rodada ${round.number}` : '';
}

/** Compartilhar as tabelas no WhatsApp, com o link da página como está (fase e rodada);
 *  `groups`: tabela de fase de grupos (a classificação geral/personalizada não é grupo, nem
 *  tem rodada: junta fases). */
const standingsShare = (groups = false, round = standingsRound) => ({ title: state.data?.competition?.name, url: () => window.location.href, groups, round });

function leaveGroupRows() {
  els.matches.classList.remove('group-rows');
  state.groupLayout = null;
}

/** Cards da rodada na ordem da home; na fase de grupos, separados por grupo (com a tabela de
 *  cada um ao lado, createGroupRows). */
function matchBlocks(matches, card) {
  if (groupRowsMode()) {
    els.matches.classList.add('group-rows');
    state.groupLayout = createGroupRows(state.standingsData, matches, card, { share: standingsShare(true) });
    return state.groupLayout.rows;
  }
  leaveGroupRows();
  if (state.stage?.format !== 'groups') return sortMatchesForDisplay(matches).map(card);
  return createMatchGroupBlocks(matches, state.standingsData, card); // fase de grupos sem tabela
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
  els.share.hidden = next.size === 0;
  els.ties.hidden = true; // o agregado fica no card do jogo de volta
  els.tiesList.replaceChildren();
  paintRoundBar();
  updateAside();
}

function renderStandings(stage) {
  state.standingsData = stage.standings || null;
  if (stage.standings) {
    if (state.standingsEl && state.standingsStageId === stage.id) {
      updateStandings(state.standingsEl, stage.standings, { stageTitle: stageTitle() });
    } else {
      state.standingsEl = createStandings(stage.standings, { share: standingsShare(stage.format === 'groups'), stageTitle: stageTitle() });
      state.standingsStageId = stage.id;
      els.standings.replaceChildren(state.standingsEl);
    }
  } else {
    state.standingsEl = null;
    state.standingsStageId = null;
    els.standings.replaceChildren();
  }
  els.standings.hidden = !stage.standings || groupRowsMode(); // grupos: a tabela de cada um na linha dele
}

/* --- Outras classificações: menu "Classificações" e a visão em largura toda ----------------- */

/** Classificações do menu: as da competição e as da fase exibida (sem repetir). */
function rankingRefs(data) {
  const seen = new Set();
  return [...(data.rankings || []), ...(data.stage?.rankings || [])].filter((r) => !seen.has(r.id) && seen.add(r.id));
}

/** Com outras fases ou outras classificações, o título da tabela da fase diz qual recorte é
 *  ("Classificação · 2º turno"); as outras já têm o nome delas ("Classificação geral"). */
const stageTitle = () => (state.data?.stages || []).length > 1 || rankingRefs(state.data || {}).length > 0;

/** O menu: uma entrada por classificação (link com a URL da visão: abre em outra aba também),
 *  `aria-current` na exibida; o botão diz qual é ("Classificações · Melhores segundos"). Some
 *  sem classificação. */
function paintRankingsMenu() {
  const refs = rankingRefs(state.data || {});
  els.rankingsMenu.hidden = refs.length === 0;
  if (!refs.length) closeRankingsMenu();
  els.rankingsList.replaceChildren(...refs.map((ref) => h('li', null,
    h('a', { class: 'rankings-menu__item', href: urlFor(ref.id), 'data-ranking-id': ref.id, 'aria-current': ref.id === state.view ? 'page' : null },
      h('span', { text: ref.name }),
      ref.id === state.view ? icon('check', { className: 'icon--sm' }) : null))));
  const chosen = refs.find((r) => r.id === state.view);
  els.rankingsChosen.textContent = chosen ? ` · ${chosen.name}` : '';
  els.rankingsChosen.hidden = !chosen;
  els.rankingsToggle.classList.toggle('is-active', !!chosen);
}

function openRankingsMenu() {
  paintRankingsMenu(); // os links com a rodada de agora
  els.rankingsList.hidden = false;
  els.rankingsToggle.setAttribute('aria-expanded', 'true');
}

/** Fecha a lista; `refocus`: o foco volta ao botão (Esc). */
function closeRankingsMenu(refocus = false) {
  if (els.rankingsList.hidden) return;
  els.rankingsList.hidden = true;
  els.rankingsToggle.setAttribute('aria-expanded', 'false');
  if (refocus) els.rankingsToggle.focus();
}

/** Visão da página: null, os jogos da rodada (com a tabela da fase ao lado e a faixa do
 *  WhatsApp em cima); o id de uma classificação do menu, ela na largura toda no lugar deles (a
 *  navegação de rodada some). Id que não é desta página (outra fase) vira os jogos. `reload`:
 *  busca a classificação de novo mesmo sem trocar (recarga da página). */
function setView(view, { reload = false } = {}) {
  const ref = view == null ? null : rankingRefs(state.data || {}).find((r) => r.id === view) || null;
  const next = ref ? ref.id : null;
  const changed = next !== state.view;
  state.view = next;
  els.grid.hidden = !!ref;
  els.roundNav.hidden = !!ref;
  els.rankingView.hidden = !ref;
  paintRoundBar();
  paintRankingsMenu();
  if (ref) {
    if (changed) els.rankingStandings.replaceChildren(rankingSkeleton()); // a anterior não fica na tela durante a carga
    if (changed || reload) loadRanking(ref.id);
  } else if (changed) {
    clearTimeout(state.rankingTimer);
    state.rankingToken += 1; // a carga em andamento não pinta mais
    state.rankingPending = null;
    state.rankingStageIds = [];
    els.rankingStandings.replaceChildren();
  }
}

/** Escolhida no menu (ou "Voltar aos jogos"): um passo novo no histórico do navegador. O foco
 *  vai para o primeiro controle da visão nova ("Voltar aos jogos"; de volta aos jogos, o botão
 *  "Classificações"), não cai no fim da página com o botão que sumiu. */
function chooseView(view) {
  closeRankingsMenu();
  setView(view);
  writeUrl(true);
  (state.view != null ? els.rankingBack : els.rankingsToggle).focus();
}

/** Card provisório enquanto a classificação carrega. */
const rankingSkeleton = () => h('div', { class: 'card skeleton-card', 'aria-hidden': 'true' },
  h('span', { class: 'skeleton skeleton--line', style: { width: '40%' } }),
  ...[90, 80, 85, 70].map((w) => h('span', { class: 'skeleton skeleton--line', style: { width: `${w}%` } })));

async function loadRanking(id) {
  const token = ++state.rankingToken;
  const pending = new Set();
  state.rankingPending = pending;
  try {
    const data = await getRanking(id);
    if (token !== state.rankingToken || state.view !== id) return; // trocou de visão no meio
    state.rankingStageIds = data.stage_ids || [];
    els.rankingStandings.replaceChildren(createStandings(data, { full: true, heading: { level: 2, id: 'ranking-title' }, share: standingsShare(false, '') }));
  } catch {
    if (token === state.rankingToken) {
      showToast('Não deu para carregar a classificação agora.', { kind: 'error' });
      // sem a tabela ainda: o aviso no lugar do esqueleto (a que já estava na tela fica)
      if (!els.rankingStandings.querySelector('.standings')) els.rankingStandings.replaceChildren(h('p', { class: 'muted', text: 'Não deu para carregar a classificação agora.' }));
    }
  } finally {
    // a resposta pode ter vindo antes do que mudou no meio da carga: busca de novo
    if (token === state.rankingToken) {
      state.rankingPending = null;
      if ([...pending].some(rankingHas)) scheduleRanking();
    }
  }
}

/** Sem a tabela da fase (mata-mata, fase sem tabela), os jogos ocupam a largura toda. */
function updateAside() {
  const hasAside = !els.standings.hidden || !els.ties.hidden;
  els.grid.classList.toggle('split--no-aside', !hasAside);
  els.aside.hidden = !hasAside;
}

/** Faixa acima dos jogos: o que está listado ("Jogos da rodada 3", "Jogos da final") e o
 *  "Resultados no WhatsApp" (com jogo na rodada). Só na visão dos jogos. */
function paintRoundBar() {
  const round = state.rounds[state.roundIndex];
  els.roundBarLabel.textContent = gamesLabel(round);
  els.roundBar.hidden = state.view != null || !round;
}

const FEMININE = new Set(['rodada', 'final', 'semifinal', 'fase', 'etapa', 'decisão', 'repescagem', 'partida']);
const FEMININE_PLURAL = new Set(['quartas', 'oitavas', 'finais', 'semifinais', 'rodadas']);
const MASCULINE = new Set(['turno', 'returno', 'jogo', 'confronto', 'playoff', 'play-off', 'mata-mata', 'triangular', 'quadrangular', 'hexagonal', 'octogonal']);

/** "Jogos da rodada 3", "Jogos da final", "Jogos das quartas de final", "Jogos do 1º jogo";
 *  nome sem artigo conhecido fica como está ("Jogos · Grande Clássico"). */
function gamesLabel(round) {
  if (!round) return '';
  const name = String(round.name || `Rodada ${round.number}`).trim();
  const lower = name.charAt(0).toLocaleLowerCase('pt-BR') + name.slice(1);
  const first = lower.split(/\s+/)[0];
  let article = null;
  if (FEMININE_PLURAL.has(first)) article = 'das';
  else if (FEMININE.has(first) || /ª$/.test(first)) article = 'da';
  else if (MASCULINE.has(first) || /º$/.test(first)) article = 'do';
  return article ? `Jogos ${article} ${lower}` : `Jogos · ${name}`;
}

function paintRoundNav() {
  const round = state.rounds[state.roundIndex];
  els.roundLabel.textContent = round ? round.name || `Rodada ${round.number}` : 'Sem rodadas';
  els.prev.disabled = state.roundIndex <= 0;
  els.next.disabled = state.roundIndex < 0 || state.roundIndex >= state.rounds.length - 1;
}

/** Resultados da rodada exibida no WhatsApp, na ordem da página, com o link dela. */
function shareRound() {
  const round = state.rounds[state.roundIndex];
  const stages = state.data?.stages || [];
  const title = [
    state.data?.competition?.name,
    stages.length > 1 ? state.stage?.name : null,
    round ? round.name || `Rodada ${round.number}` : null,
  ].filter(Boolean).join(' · ');
  const matches = [...els.matches.querySelectorAll('.match')].map(getCardMatch).filter(Boolean);
  shareResultsOnWhatsApp(title, matches, { now: now(), url: window.location.href });
}

/** URL da tela: fase, rodada e, na visão de uma classificação, `tabela` (o id dela). */
function urlFor(view = state.view) {
  const search = new URLSearchParams({ slug: state.slug });
  if (state.stage) search.set('stage', String(state.stage.id));
  const round = state.rounds[state.roundIndex];
  if (round) search.set('round', String(round.id));
  if (view != null) search.set('tabela', String(view));
  return `${window.location.pathname}?${search}`;
}

/** Atualiza a URL sem recarregar. `push`: trocar de visão (jogos × classificação) por ação de
 *  quem lê é um passo novo no histórico (voltar e avançar do navegador); trocar de rodada ou de
 *  fase só substitui. */
function writeUrl(push = false) {
  const url = urlFor();
  if (url === `${window.location.pathname}${window.location.search}`) return;
  const shown = new URLSearchParams(window.location.search).get('tabela');
  if (push && shown !== (state.view == null ? null : String(state.view))) window.history.pushState(null, '', url);
  else window.history.replaceState(null, '', url);
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

/** Desenha a resposta da competição; `view`: a visão (null: os jogos; id: a classificação). */
function render(data, requestedRoundId = null, view = state.view) {
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
  els.missing.hidden = true;
  els.error.hidden = true;
  paintRoundNav();
  renderStandings(data.stage || {});
  renderMatches(data.stage?.matches || [], data.stage?.format === 'knockout' ? data.stage?.ties || null : null);
  setView(view, { reload: true });
}

/* --- Menu: competições com jogo ao vivo ------------------------------------------------------ */

/** O ponto "Jogando agora" do menu, como na home: as competições com jogo ao vivo agora. */
function paintNav(force = false) {
  if (!els.nav) return;
  const slugs = new Set(state.liveMatches.values());
  const key = [...slugs].sort().join('|');
  if (!force && key === state.liveKey) return;
  state.liveKey = key;
  renderCompetitionNav(els.nav, state.competitions, { activeSlug: state.slug, liveSlugs: slugs });
}

/** Jogos ao vivo agora, de todas as competições (GET /api/matches?status=live): a home tira o
 *  ponto do menu dos jogos do dia; esta página não os tem. Pedida só depois da resposta da
 *  competição (boot, reload): a foto é posterior ao cursor de onde o stream recomeça. */
const liveNow = () => listMatches({ status: 'live' });

function setLiveMatches(data) {
  state.liveMatches = new Map((data?.matches || []).filter((m) => m.status === 'live' && m.competition?.slug).map((m) => [m.id, m.competition.slug]));
}

/** Mensagem `match` de qualquer competição: entrou ou saiu do ao vivo. */
function liveOnMatch(message) {
  const match = message?.match;
  if (!match?.id) return;
  const slug = match.competition?.slug || state.competitions.find((c) => c.id === message.competition_id)?.slug;
  if (match.status === 'live' && slug) state.liveMatches.set(match.id, slug);
  else state.liveMatches.delete(match.id);
  paintNav();
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

/** Troca de rodada ou fase: a resposta pode ser de antes de uma mensagem que chegou durante a
 *  carga (e o card do jogo ainda não existia para recebê-la). Guarda as mensagens e, depois de
 *  desenhar a resposta, aplica de novo: o card descarta a versão mais velha (`version`), e a
 *  classificação só recebe as mensagens depois do cursor da resposta (quando ela traz um). */
function startLoad() {
  if (!state.pending) state.pending = [];
}

function finishLoad(cursor = null) {
  const queued = state.pending || [];
  state.pending = null;
  for (const { topic, message, id } of queued) {
    if (topic === 'match') onMatch(message, id);
    else if (cursor == null || id == null || id > cursor) onStandings(message, id);
  }
}

async function goToRound(index) {
  const round = state.rounds[index];
  if (!round || index === state.roundIndex) return;
  const previous = state.roundIndex;
  const seq = ++state.seq;
  startLoad();
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
      finishLoad(data?.cursor ?? null);
    } else {
      const data = await listMatches({ roundId: round.id });
      if (seq !== state.seq) return;
      sync(data);
      renderMatches(data?.matches || []);
      finishLoad(); // a lista não traz cursor (a classificação da fase não mudou de tela)
    }
    writeUrl();
  } catch {
    if (seq !== state.seq) return;
    state.pending = null; // a página continua a mesma: os cards dela já receberam as mensagens
    state.roundIndex = previous;
    paintRoundNav();
    els.matches.removeAttribute('aria-busy');
    showToast('Não deu certo agora. Tente de novo em instantes.', { kind: 'error' });
  }
}

/** Troca de fase: volta aos jogos (o <select> fica na visão da classificação também). Pelo
 *  voltar/avançar do navegador (`push: false`), com a rodada e a visão da URL. */
async function goToStage(stageId, { round = null, view = null, push = true } = {}) {
  const seq = ++state.seq;
  startLoad();
  els.stageSelect.disabled = true;
  els.matches.setAttribute('aria-busy', 'true');
  try {
    const data = await fetchCompetition({ stage: stageId, round });
    if (seq !== state.seq) return;
    sync(data);
    render(data, round, view);
    writeUrl(push);
    finishLoad(data?.cursor ?? null);
  } catch {
    if (seq !== state.seq) return;
    state.pending = null;
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
  const live = await liveNow().catch(() => null); // depois do cursor (boot); sem ela, o menu fica como estava
  if (live) {
    setLiveMatches(live);
    paintNav();
  }
  if (seq === state.seq) {
    sync(data);
    render(data, round?.id ?? null);
    writeUrl();
    state.pending = null; // o stream recomeça do cursor desta leitura: nada se perde
  }
  return data.cursor ?? null;
}

/** Voltar/avançar do navegador: a fase, a rodada e a visão da URL. */
function restoreFromUrl() {
  if (!state.data) return;
  closeRankingsMenu();
  const search = new URLSearchParams(window.location.search);
  const stage = toInt(search.get('stage'));
  const round = toInt(search.get('round'));
  const view = toInt(search.get('tabela'));
  if (stage != null && stage !== state.stage?.id) {
    goToStage(stage, { round, view, push: false });
    return;
  }
  const index = round != null ? state.rounds.findIndex((r) => r.id === round) : -1;
  setView(view);
  if (index >= 0 && index !== state.roundIndex) goToRound(index);
  else writeUrl(); // id que não vale mais sai da URL
}

/* --- Stream ------------------------------------------------------------------------------------ */

/** Status mudou ao vivo: reordena os cards da rodada (a ordem depende do status). */
function reorderCards() {
  const matches = [...state.cards.values()].map(getCardMatch).filter(Boolean);
  els.matches.replaceChildren(...matchBlocks(matches, (m) => state.cards.get(m.id)));
}

/** A classificação exibida soma esta fase? */
const rankingHas = (stageId) => state.view != null && state.rankingStageIds.includes(stageId);

/** Busca a classificação exibida de novo (agrupado: um lance publica várias mensagens). */
function scheduleRanking() {
  clearTimeout(state.rankingTimer);
  state.rankingTimer = setTimeout(() => state.view != null && loadRanking(state.view), 1500);
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

function onMatch(message, id = null) {
  const match = message?.match;
  if (!match) return;
  liveOnMatch(message);
  state.pending?.push({ topic: 'match', message, id });
  rankingOnMatch(message.stage_id, match);
  const card = state.cards.get(match.id);
  const before = card ? getCardMatch(card) : null;
  if (card) updateMatchCard(card, match, { flash: true });
  // a ordem depende do status; grupo ou time trocado no admin leva o card para outra linha
  const placement = (m) => `${m.status}|${m.group?.id ?? ''}|${m.home?.id ?? ''}|${m.away?.id ?? ''}`;
  if (card && before && placement(before) !== placement(getCardMatch(card)) && state.stage?.format !== 'knockout') reorderCards();
  // mata-mata: agregado e vencedor vêm no TieOut da partida e valem para o card do outro jogo
  syncTieCards(state.cards.values(), match);
}

function onStandings(message, id = null) {
  state.pending?.push({ topic: 'standings', message, id });
  rankingChanged([message?.stage_id]);
  if (state.standingsEl && message?.stage_id === state.standingsStageId && message.standings) {
    updateStandings(state.standingsEl, message.standings);
    state.standingsData = message.standings;
    if (groupRowsMode()) updateGroupTables(message.standings);
  }
}

/** Tabelas ao lado dos jogos de cada grupo: redesenham no lugar (os cards não saem do
 *  lugar); se mudam as linhas (grupos, jogos entre grupos), refaz. */
function updateGroupTables(standings) {
  const matches = [...state.cards.values()].map(getCardMatch).filter(Boolean);
  if (!updateGroupRows(state.groupLayout, standings, matches)) reorderCards();
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
  const requestedView = toInt(params.get('tabela'));
  const competition = fetchCompetition({ stage: toInt(params.get('stage')), round: requestedRound });
  const [compsResult, dataResult, liveResult] = await Promise.allSettled([
    getCompetitions(),
    competition,
    // o ponto "Jogando agora" do menu: depois da resposta da competição, para a foto ser
    // posterior ao cursor do stream (o que mudar entre os dois chega de novo, em ordem)
    competition.then(liveNow, liveNow),
  ]);
  state.competitions = compsResult.status === 'fulfilled' ? compsResult.value?.competitions || [] : [];
  if (liveResult.status === 'fulfilled') setLiveMatches(liveResult.value);
  paintNav(true);
  if (dataResult.status !== 'fulfilled') {
    if (dataResult.reason?.status === 404) showMissing();
    else showError();
    return;
  }
  const data = dataResult.value;
  sync(data);
  render(data, requestedRound, requestedView);
  writeUrl();

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

els.share.addEventListener('click', shareRound);
els.rankingsToggle.addEventListener('click', () => (els.rankingsList.hidden ? openRankingsMenu() : closeRankingsMenu()));
els.rankingsList.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-ranking-id]');
  // com Ctrl/⌘/Shift ou o botão do meio, o navegador abre o link (a URL da visão) em outra aba
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  chooseView(Number(link.dataset.rankingId));
});
els.rankingsMenu.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.rankingsList.hidden) {
    event.preventDefault();
    closeRankingsMenu(true);
  }
});
// clique fora fecha; o foco que sai do menu (Tab) também
document.addEventListener('click', (event) => els.rankingsMenu.contains(event.target) || closeRankingsMenu());
els.rankingsMenu.addEventListener('focusout', (event) => {
  if (event.relatedTarget && !els.rankingsMenu.contains(event.relatedTarget)) closeRankingsMenu();
});
els.rankingBack.addEventListener('click', () => chooseView(null));
window.addEventListener('popstate', restoreFromUrl);
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
