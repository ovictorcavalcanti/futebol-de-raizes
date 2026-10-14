/**
 * Classificação: uma <table> por grupo com <caption>, faixa de zona com o nome
 * em texto, destaque de quem está em jogo, marca de empate, legenda (amostra SVG
 * com fill vindo da API e "Jogando agora" quando alguém da tabela está em campo),
 * critérios de desempate na ordem configurada e punições em pontos (marca "*" nos
 * pontos ajustados e a lista com o motivo embaixo da legenda).
 *
 * As mesmas colunas em todas as larguras: # Time P J V E D GP SG (GC fica só na imagem
 * compartilhada). No contêiner estreito, o nome do time vira a sigla (CSS).
 *
 * Nada de regra aqui: ordem, zonas, cores e critérios chegam prontos do back.
 */
import { h, s } from './render.js';
import { createCrest } from './crest.js';
import { icon } from './icons.js';
import { openStandingsShare } from './share.js';

const HEX = /^#[0-9a-f]{6}$/i;
const COLUMNS = [
  { key: 'points', label: 'P', title: 'Pontos', cls: 'col-pts' },
  { key: 'played', label: 'J', title: 'Jogos', cls: 'col-j' },
  { key: 'won', label: 'V', title: 'Vitórias', cls: 'col-w' },
  { key: 'drawn', label: 'E', title: 'Empates', cls: 'col-d' },
  { key: 'lost', label: 'D', title: 'Derrotas', cls: 'col-l' },
  { key: 'goals_for', label: 'GP', title: 'Gols pró', cls: 'col-gp' },
  { key: 'goal_difference', label: 'SG', title: 'Saldo de gols', cls: 'col-sg' },
];
/** O ponto vermelho de quem está em campo: o mesmo nome no título, no leitor de tela e na legenda. */
const PLAYING = 'Jogando agora';
const TIED = 'Empatados em todos os critérios de desempate';

function signed(n) {
  return n > 0 ? `+${n}` : String(n);
}

/** Pontos de um ajuste com o sinal tipográfico: −3, +2. */
export function adjustmentLabel(points) {
  const n = Number(points) || 0;
  return n < 0 ? `\u2212${Math.abs(n)}` : `+${n}`;
}

/** Linha da nota de punições: "Santa Cruz: −3 pts — escalação irregular". */
export function adjustmentNote(item) {
  const name = item?.team?.name || item?.team?.short_name || 'Time';
  const reason = item?.reason ? ` — ${item.reason}` : '';
  return `${name}: ${adjustmentLabel(item?.points)} pts${reason}`;
}

/** Texto do título (e do leitor de tela) da marca "*" nos pontos ajustados. */
export function adjustmentTitle(points) {
  const n = Number(points) || 0;
  const amount = Math.abs(n) === 1 ? '1 ponto' : `${Math.abs(n)} pontos`;
  return n < 0 ? `Punição: perdeu ${amount} fora de campo` : `Bonificação: ganhou ${amount} fora de campo`;
}

function pointsCell(row, col) {
  const adjustment = Number(row.points_adjustment) || 0;
  if (!adjustment) return h('td', { class: col.cls, text: row.points ?? 0 });
  return h('td', { class: [col.cls, 'is-adjusted'] },
    String(row.points ?? 0),
    h('abbr', { class: 'adj-mark', title: adjustmentTitle(adjustment), text: '*' }),
    h('span', { class: 'visually-hidden', text: ` (${adjustmentTitle(adjustment).toLowerCase()})` }),
  );
}

function swatch(color) {
  return s('svg', { class: 'legend__swatch', viewBox: '0 0 12 12', width: '12', height: '12', 'aria-hidden': 'true', focusable: 'false' },
    s('rect', { width: '12', height: '12', rx: '3', fill: HEX.test(color || '') ? color : 'currentColor' }));
}

function rangeLabel(item) {
  if (item.from == null) return '';
  return item.to != null && item.to !== item.from ? `${item.from}º–${item.to}º` : `${item.from}º`;
}

/** Item da legenda como uma frase: nome e faixa sem quebrar no meio ("Classificados 1º–2º") e,
 *  na zona condicional, a condição em texto corrido ("Melhor 2º (só o 1º em “Melhores
 *  segundos”)"). A faixa que o nome já diz ("Melhor 2º" + "2º") não se repete. */
function legendItem(item) {
  const range = rangeLabel(item);
  const named = range && String(item.name || '').split(/\s+/).includes(range);
  return h('li', { class: 'legend__item' }, swatch(item.color),
    h('span', { class: 'legend__text' },
      h('span', { class: 'legend__name', text: item.name }),
      // espaços de verdade entre as partes: a frase quebra entre elas e dentro da condição
      ...(range && !named ? [' ', h('span', { class: 'legend__range', text: range })] : []),
      ...(item.condition ? [' ', h('span', { class: 'legend__cond', text: `(${item.condition})` })] : []),
    ));
}

/** Título da tabela de grupo único: "Classificação" ou, quando há outras fases ou outras
 *  classificações, "Classificação · 2º turno" (opts.stageTitle; sem ela, quando a temporada
 *  tem mais de uma fase: `season_stages`). Grupo com nome ("Grupo A", "Classificação geral",
 *  "Melhores segundos") fica com o nome. */
function captionText(standings, group, multiple, opts) {
  if (multiple || (group.name && group.name !== 'Tabela')) return group.name;
  const withStage = opts.stageTitle ?? (standings.season_stages > 1);
  return withStage && standings.stage_name ? `Classificação · ${standings.stage_name}` : 'Classificação';
}

function groupCaption(standings, group, multiple, anyPlaying, opts) {
  return h('caption', null, captionText(standings, group, multiple, opts),
    standings.kind === 'live' && anyPlaying ? h('span', { class: 'badge badge--live', text: 'Ao vivo' }) : null,
  );
}

function teamCell(row) {
  const team = row.team || {};
  // A zona aparece só na cor da linha e na legenda; o nome fica para leitor de tela.
  const zoneName = row.zone?.name || '';
  return h('td', { class: 'col-team' },
    h('div', { class: 'team-cell' },
      createCrest(team, { size: 22 }),
      h('span', { class: 'team-cell__text' },
        h('span', { class: 'team-cell__name', title: team.name, text: team.name || team.short_name || '' }),
        h('abbr', { class: 'team-cell__short', title: team.name, text: team.short_name || team.name || '' }),
        zoneName ? h('span', { class: 'visually-hidden', text: `, zona: ${zoneName}` }) : null,
        // classificação "posição nos grupos": de que grupo é cada time
        row.group ? h('span', { class: 'team-cell__group', text: row.group }) : null,
      ),
      row.playing ? h('span', { class: 'live-dot', title: PLAYING }) : null,
      row.playing ? h('span', { class: 'visually-hidden', text: `, ${PLAYING.toLowerCase()}` }) : null,
    ),
  );
}

/** A tabela no WhatsApp (imagem e mensagem): "competição · fase · rodada" (`share.round`) e, numa fase de grupos
 *  (`share.groups` ou mais de um grupo na tabela), o grupo. A classificação geral ou personalizada
 *  (um "grupo" com o nome da classificação) não vira grupo. */
function shareGroup(standings, group, share, multiple) {
  const groupName = (multiple || share.groups) && group.name !== 'Tabela' ? group.name || '' : '';
  const value = (option) => (typeof option === 'function' ? option() : option || '');
  openStandingsShare(standings, group, { competition: share.title, groupName, round: value(share.round), url: value(share.url) });
}

function groupTable(standings, group, opts, multiple, last) {
  const rows = group.rows || [];
  const anyPlaying = rows.some((r) => r.playing);
  const highlight = opts.highlightTeamIds;
  const thead = h('thead', null, h('tr', null,
    h('th', { scope: 'col', class: 'col-pos' }, h('abbr', { title: 'Posição', text: '#' })),
    h('th', { scope: 'col', class: 'col-team', text: 'Time' }),
    ...COLUMNS.map((c) => h('th', { scope: 'col', class: c.cls }, h('abbr', { title: c.title, text: c.label }))),
  ));
  const tbody = h('tbody', null, ...rows.map((row) => {
    const zone = row.zone && HEX.test(row.zone.color || '') ? row.zone.color : null;
    const tr = h('tr', {
      class: [row.playing && 'is-playing', highlight?.has(row.team?.id) && 'is-highlight'],
      'data-team-id': row.team?.id,
    },
      // rank: empatados em todos os critérios repetem a posição (1, 2, 2, 4)
      h('td', { class: 'col-pos', style: zone ? { '--zone': zone } : null, title: row.zone?.name || null },
        String(row.rank ?? row.position),
        row.tied ? h('abbr', { class: 'tied-mark', title: 'Empatado em todos os critérios de desempate', text: '=' }) : null,
      ),
      teamCell(row),
      ...COLUMNS.map((c) => (c.key === 'points'
        ? pointsCell(row, c)
        : h('td', { class: c.cls, text: c.key === 'goal_difference' ? signed(row[c.key] ?? 0) : (row[c.key] ?? 0) }))),
    );
    return tr;
  }));
  const table = h('table', { class: 'table' }, groupCaption(standings, group, multiple, anyPlaying, opts), thead, tbody);
  // compartilhar a tabela no WhatsApp (opts.share = {title, url}): no canto, na altura do título
  const share = opts.share && rows.length ? h('button', {
    type: 'button', class: 'btn btn--secondary btn--icon btn--sm standings__share',
    'aria-label': 'Compartilhar a classificação no WhatsApp', title: 'Compartilhar no WhatsApp',
    onclick: () => shareGroup(standings, group, opts.share, multiple),
  }, icon('share')) : null;
  return h('div', { class: ['card standings__group', share && 'standings__group--shareable'], 'data-group-id': group.id },
    share, h('div', { class: 'table-scroll' }, table), footer(standings, opts, groupFoot(standings, group, multiple, last)));
}

const teamIdsOf = (groups) => new Set(groups.flatMap((g) => (g.rows || []).map((r) => r.team?.id)).filter((id) => id != null));

/** Embaixo de cada grupo: a legenda das cores, as punições dos times dele e a nota do "=" se
 *  a tabela dele tem empate; os critérios de desempate só no último grupo (uma vez, dentro do
 *  card, sem caixa à parte). */
function groupFoot(standings, group, multiple, last) {
  const groups = standings.groups || [];
  let adjustments = standings.adjustments || [];
  if (multiple) {
    const mine = teamIdsOf([group]);
    const anywhere = teamIdsOf(groups);
    // punição de time fora de todos os grupos fica no último, para não sumir
    adjustments = adjustments.filter((a) => mine.has(a.team?.id) || (last && !anywhere.has(a.team?.id)));
  }
  return { adjustments, criteria: last, rows: group.rows || [] };
}

/** Rodapé de uma tabela; `rows`: as linhas dela (quem está em campo, quem está empatado). */
function footer(standings, opts, { adjustments = [], criteria = true, rows = [] } = {}) {
  const parts = [];
  const playing = rows.some((r) => r.playing);
  if (opts.legend !== false && (standings.legend?.length || playing)) {
    parts.push(h('ul', { class: 'legend', 'aria-label': 'Legenda da tabela' },
      ...(standings.legend || []).map(legendItem),
      // o ponto vermelho das linhas (o mesmo nome da imagem compartilhada)
      playing ? h('li', { class: 'legend__item legend__item--playing' }, h('span', { class: 'live-dot', 'aria-hidden': 'true' }), h('span', { text: PLAYING })) : null,
    ));
  }
  if (adjustments.length) {
    // Punição muda a tabela: aparece sempre (mesmo sem legenda), com o motivo.
    parts.push(h('ul', { class: 'adjustments', 'aria-label': 'Punições e bonificações em pontos' },
      ...adjustments.map((item) => h('li', { class: 'adjustments__item' },
        h('span', { class: 'adj-mark', 'aria-hidden': 'true', text: '*' }),
        h('span', { text: adjustmentNote(item) }),
      ))));
  }
  // o "=" explicado embaixo de cada tabela que o tem (não só na última, a dos critérios)
  if (rows.some((r) => r.tied)) {
    parts.push(h('p', { class: 'standings__note' }, h('span', { class: 'tied-mark', 'aria-hidden': 'true', text: '=' }), ` ${TIED}`));
  }
  if (criteria && opts.criteria !== false && standings.criteria?.length) {
    parts.push(h('div', { class: 'criteria' },
      h('p', { class: 'criteria__title', text: 'Critérios de desempate' }),
      h('ol', { class: 'criteria__list' }, ...standings.criteria.map((c) => h('li', { text: c.label }))),
    ));
  }
  return parts.length ? h('div', { class: 'standings__foot' }, ...parts) : null;
}

/**
 * Classificação de uma fase (StageStandingsOut, CONTRACT §3).
 * @param {object} standings StageStandingsOut
 * @param {{compact?: boolean, legend?: boolean, criteria?: boolean, stageTitle?: boolean, highlightTeamIds?: Set<number>, share?: object}} [opts]
 *   compact: classe standings--compact (as colunas são as mesmas: # Time P J V E D GP SG);
 *   stageTitle: o título diz a fase ("Classificação · 2º turno"; padrão: temporada com mais de uma fase).
 * @returns {HTMLElement} <div class="standings">
 */
export function createStandings(standings, opts = {}) {
  const el = h('div', { class: ['standings', opts.compact && 'standings--compact'], 'data-stage-id': standings?.stage_id });
  updateStandings(el, standings, opts);
  return el;
}

/** Opções de cada tabela: a mensagem do stream redesenha sem precisar repassá-las. */
const OPTIONS = new WeakMap();

/**
 * Redesenha a classificação no mesmo elemento (estado completo, sem merge).
 * Sem `opts`, valem as da criação (ou da última atualização).
 */
export function updateStandings(el, standings, opts = undefined) {
  opts = { ...(OPTIONS.get(el) || {}), ...(opts || {}) };
  OPTIONS.set(el, opts);
  el.classList.toggle('standings--compact', !!opts.compact);
  const groups = standings?.groups || [];
  const multiple = groups.length > 1;
  const parts = groups.map((g, i) => groupTable(standings, g, opts, multiple, i === groups.length - 1));
  if (!parts.length) parts.push(h('p', { class: 'muted', text: 'Classificação ainda sem jogos.' }));
  el.dataset.stageId = standings?.stage_id ?? '';
  el.replaceChildren(...parts);
  return el;
}
