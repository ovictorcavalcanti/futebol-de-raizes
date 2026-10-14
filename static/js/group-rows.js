/**
 * Fase de grupos em linhas (home e página da competição): cada grupo numa linha, com os jogos
 * dele à esquerda e a tabela dele à direita, topo alinhado com o primeiro jogo; a linha tem a
 * altura do mais alto dos dois, então a tabela de um grupo nunca invade a linha do seguinte.
 *
 * Jogo entre times de grupos diferentes (pelos times da tabela, não pelo grupo cadastrado na
 * partida) vai para a seção "Jogos entre grupos", depois dos grupos, com as tabelas de todos os
 * grupos envolvidos uma abaixo da outra (o único lugar com mais de uma tabela ao lado dos
 * jogos). O grupo que só tem jogo entre grupos não ganha linha própria; numa rodada mista (caso
 * raro), o grupo com jogo dentro dele tem a linha dele e a tabela aparece também na seção.
 *
 * Embaixo de cada tabela: a legenda das cores, as punições dos times do grupo e a nota do "="
 * quando a tabela tem empate; os critérios de desempate ficam uma vez, no card do último grupo
 * exibido.
 */
import { h } from './render.js';
import { createStandings, updateStandings } from './standings.js';
import { sortMatchesForDisplay, createMatchGroup } from './match-card.js';

const idsOf = (groups) => new Set(groups.flatMap((g) => (g.rows || []).map((r) => r.team?.id)).filter((id) => id != null));

/** time → grupo, pelas linhas da tabela */
function teamGroups(standings) {
  const map = new Map();
  for (const g of standings?.groups || []) {
    for (const r of g.rows || []) if (r.team?.id != null) map.set(r.team.id, g.id);
  }
  return map;
}

/**
 * Separa os jogos do recorte: os de dentro de cada grupo e os entre grupos.
 * Jogo de dentro vai para o grupo dos times (ou, sem eles na tabela, para o da partida).
 * @param {object[]} matches MatchOut
 * @param {object} standings StageStandingsOut
 * @returns {{byGroup: Map<number|null, {group: object|null, matches: object[]}>, cross: object[], crossGroups: Set<number>}}
 */
export function splitGroupMatches(matches, standings) {
  const teams = teamGroups(standings);
  const byGroup = new Map();
  const cross = [];
  const crossGroups = new Set();
  for (const m of matches) {
    const home = teams.get(m.home?.id);
    const away = teams.get(m.away?.id);
    if (home != null && away != null && home !== away) {
      cross.push(m);
      crossGroups.add(home);
      crossGroups.add(away);
      continue;
    }
    const key = home ?? away ?? m.group?.id ?? null;
    if (!byGroup.has(key)) byGroup.set(key, { group: m.group || null, matches: [] });
    byGroup.get(key).matches.push(m);
  }
  for (const entry of byGroup.values()) entry.matches = sortMatchesForDisplay(entry.matches);
  return { byGroup, cross: sortMatchesForDisplay(cross), crossGroups };
}

/** Grupos com linha: todos ('all', competição) ou só os com jogo no recorte ('playing', home). */
function shownGroups(standings, split, only) {
  const groups = standings?.groups || [];
  if (only !== 'playing') return groups;
  const playing = groups.filter((g) => split.byGroup.has(g.id) || split.crossGroups.has(g.id));
  return playing.length ? playing : groups; // nada casou (dado incompleto): mostra todos
}

/** Onde ficam as tabelas: na linha do grupo (tem jogo dentro do grupo, ou nenhum jogo no
 *  recorte) e na seção dos jogos entre grupos (todos os grupos envolvidos neles). `order` é a
 *  ordem das tabelas na tela (a última leva os critérios de desempate). */
function placeGroups(standings, split, only) {
  const shown = shownGroups(standings, split, only);
  const own = shown.filter((g) => split.byGroup.has(g.id) || !split.crossGroups.has(g.id));
  const cross = shown.filter((g) => split.crossGroups.has(g.id));
  return { own, cross, order: [...own, ...cross] };
}

/** Tabela do grupo `index` dos exibidos: legenda e punições dos times dele (e a nota do "=",
 *  se ela tem empate); o último leva os critérios de desempate e a punição de time fora de
 *  todos os grupos. */
function tableArgs(standings, shown, index) {
  const group = shown[index];
  const last = index === shown.length - 1;
  const mine = idsOf([group]);
  const anywhere = idsOf(standings.groups || []);
  const adjustments = (standings.adjustments || []).filter((a) => mine.has(a.team?.id) || (last && !anywhere.has(a.team?.id)));
  return [{ ...standings, groups: [group], adjustments }, { criteria: last }];
}

/** O que muda as linhas (grupos exibidos e onde, jogos de cada um, jogos entre grupos). Não
 *  depende da ordem em que os jogos chegam (a da API ou a dos cards na tela). */
function layoutKey(place, split) {
  const ids = (matches) => matches.map((m) => m.id).sort((a, b) => a - b);
  return JSON.stringify([
    place.own.map((g) => g.id),
    place.cross.map((g) => g.id),
    [...split.byGroup].map(([key, entry]) => [String(key), ids(entry.matches)]).sort((a, b) => a[0].localeCompare(b[0])),
    ids(split.cross),
  ]);
}

function groupRow(title, id, cards, empty, tables = [], cls = null) {
  return h('section', { class: ['group-row', cls], 'data-group-id': id ?? null },
    h('h3', { class: 'match-group__title', text: title || 'Sem grupo' }),
    cards.length
      ? h('div', { class: 'match-group__games' }, ...cards)
      : h('p', { class: 'group-row__empty', text: empty }),
    tables.length ? h('div', { class: 'group-row__table' }, ...tables) : null,
  );
}

/**
 * Linhas da fase de grupos.
 * @param {object} standings StageStandingsOut da fase (todos os grupos)
 * @param {object[]} matches jogos do recorte (a rodada na competição, o dia na home)
 * @param {(match: object) => HTMLElement} card card de cada jogo (a página reaproveita os seus)
 * @param {{only?: 'all'|'playing', when?: string, share?: {title: string, url: string|Function}}} [opts]
 *   only 'playing': só os grupos com jogo no recorte; when: "nesta rodada" / "hoje", no aviso do
 *   grupo sem jogo; share: botão de compartilhar cada tabela no WhatsApp (standings.js)
 * @returns {{rows: HTMLElement[], tables: HTMLElement[], key: string}}
 */
export function createGroupRows(standings, matches, card, { only = 'all', when = 'nesta rodada', share = null } = {}) {
  const split = splitGroupMatches(matches, standings);
  const place = placeGroups(standings, split, only);
  const tables = []; // na ordem de place.order (um grupo pode aparecer duas vezes: rodada mista)
  const tableOf = (g) => {
    const [data, opts] = tableArgs(standings, place.order, tables.length);
    const table = createStandings(data, { ...opts, share: share && { ...share, groups: true } }); // cada tabela é de um grupo
    tables.push(table);
    return table;
  };
  const rest = new Map(split.byGroup);
  const rows = place.own.map((g) => {
    const games = rest.get(g.id)?.matches || [];
    rest.delete(g.id);
    return groupRow(g.name, g.id, games.map(card), `Não há jogos deste grupo ${when}.`, [tableOf(g)]);
  });
  // jogos entre grupos, com as tabelas dos grupos envolvidos, uma abaixo da outra
  if (split.cross.length) rows.push(groupRow('Jogos entre grupos', null, split.cross.map(card), '', place.cross.map(tableOf), 'group-row--cross'));
  // jogo de grupo que não está na tabela (ex.: sem grupo): linha sem tabela, no fim
  const leftovers = [...rest.values()].sort((a, b) => String(a.group?.name || '').localeCompare(String(b.group?.name || ''), 'pt-BR', { numeric: true }));
  rows.push(...leftovers.map((g) => groupRow(g.group?.name, g.group?.id, g.matches.map(card), '')));
  return { rows, tables, key: layoutKey(place, split) };
}

/**
 * Jogos separados por grupo, sem as tabelas ao lado (competição com a classificação geral na
 * lateral; home de fase sem tabela): um bloco por grupo, na ordem da tabela (sem ela, pelo
 * nome), e os jogos entre grupos num bloco à parte, depois dos grupos.
 * @param {object[]} matches MatchOut
 * @param {object|null} standings StageStandingsOut (dá o grupo de cada time e a ordem)
 * @param {(match: object) => HTMLElement} card
 * @returns {HTMLElement[]}
 */
export function createMatchGroupBlocks(matches, standings, card) {
  const split = splitGroupMatches(matches, standings);
  const order = new Map((standings?.groups || []).map((g, i) => [g.id, i]));
  const names = new Map((standings?.groups || []).map((g) => [g.id, g.name]));
  const byName = (a, b) => String(a.group?.name || '').localeCompare(String(b.group?.name || ''), 'pt-BR', { numeric: true });
  const entries = [...split.byGroup].map(([key, entry]) => ({ key, ...entry }))
    .sort((a, b) => (order.get(a.key) ?? Infinity) - (order.get(b.key) ?? Infinity) || byName(a, b));
  const blocks = entries.map((e) => createMatchGroup(names.has(e.key) ? { id: e.key, name: names.get(e.key) } : e.group, e.matches.map(card)));
  if (split.cross.length) blocks.push(createMatchGroup({ id: null, name: 'Jogos entre grupos' }, split.cross.map(card)));
  return blocks;
}

/**
 * Mensagem `standings`: redesenha as tabelas no lugar (os cards não saem do lugar).
 * @param {{tables: HTMLElement[], key: string}} layout o que createGroupRows devolveu
 * @returns {boolean} false se as linhas mudaram (grupos exibidos, jogos entre grupos): refaça
 */
export function updateGroupRows(layout, standings, matches, { only = 'all' } = {}) {
  const split = splitGroupMatches(matches, standings);
  const place = placeGroups(standings, split, only);
  if (!layout || layoutKey(place, split) !== layout.key) return false;
  place.order.forEach((g, i) => updateStandings(layout.tables[i], ...tableArgs(standings, place.order, i)));
  return true;
}
