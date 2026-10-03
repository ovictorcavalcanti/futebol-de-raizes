/**
 * Classificação: uma <table> por grupo com <caption>, faixa de zona com o nome
 * em texto, destaque de quem está em jogo, marca de empate, legenda (amostra SVG
 * com fill vindo da API) e critérios de desempate na ordem configurada.
 *
 * Nada de regra aqui: ordem, zonas, cores e critérios chegam prontos do back.
 */
import { h, s } from './render.js';
import { createCrest } from './crest.js';

const HEX = /^#[0-9a-f]{6}$/i;
const COLUMNS = [
  { key: 'points', label: 'P', title: 'Pontos', cls: 'col-pts' },
  { key: 'played', label: 'J', title: 'Jogos', cls: 'col-j' },
  { key: 'won', label: 'V', title: 'Vitórias', cls: 'col-w' },
  { key: 'drawn', label: 'E', title: 'Empates', cls: 'col-d' },
  { key: 'lost', label: 'D', title: 'Derrotas', cls: 'col-l' },
  { key: 'goals_for', label: 'GP', title: 'Gols pró', cls: 'col-gp' },
  { key: 'goals_against', label: 'GC', title: 'Gols contra', cls: 'col-gc' },
  { key: 'goal_difference', label: 'SG', title: 'Saldo de gols', cls: 'col-sg' },
];

function signed(n) {
  return n > 0 ? `+${n}` : String(n);
}

function swatch(color) {
  return s('svg', { class: 'legend__swatch', viewBox: '0 0 12 12', width: '12', height: '12', 'aria-hidden': 'true', focusable: 'false' },
    s('rect', { width: '12', height: '12', rx: '3', fill: HEX.test(color || '') ? color : 'currentColor' }));
}

function rangeLabel(item) {
  if (item.from == null) return '';
  return item.to != null && item.to !== item.from ? `${item.from}º–${item.to}º` : `${item.from}º`;
}

function groupCaption(standings, group, multiple, anyPlaying) {
  const name = !multiple && (group.name === 'Tabela' || !group.name) ? 'Classificação' : group.name;
  return h('caption', null, name,
    standings.kind === 'live' && anyPlaying ? h('span', { class: 'badge badge--live', text: 'Ao vivo' }) : null,
  );
}

function teamCell(row, previousZone) {
  const team = row.team || {};
  const zoneName = row.zone?.name || '';
  const showZone = zoneName && zoneName !== previousZone;
  return h('td', { class: 'col-team' },
    h('div', { class: 'team-cell' },
      createCrest(team, { size: 22 }),
      h('span', { class: 'team-cell__text' },
        h('span', { class: 'team-cell__name', title: team.name, text: team.name || team.short_name || '' }),
        h('abbr', { class: 'team-cell__short', title: team.name, text: team.short_name || team.name || '' }),
        showZone ? h('span', { class: 'team-cell__zone', 'aria-hidden': 'true', text: zoneName }) : null,
        zoneName ? h('span', { class: 'visually-hidden', text: `, zona: ${zoneName}` }) : null,
      ),
      row.playing ? h('span', { class: 'live-dot', title: 'Em jogo agora' }) : null,
      row.playing ? h('span', { class: 'visually-hidden', text: ', em jogo agora' }) : null,
    ),
  );
}

function groupTable(standings, group, opts, multiple) {
  const rows = group.rows || [];
  const anyPlaying = rows.some((r) => r.playing);
  const highlight = opts.highlightTeamIds;
  const thead = h('thead', null, h('tr', null,
    h('th', { scope: 'col', class: 'col-pos' }, h('abbr', { title: 'Posição', text: '#' })),
    h('th', { scope: 'col', class: 'col-team', text: 'Time' }),
    ...COLUMNS.map((c) => h('th', { scope: 'col', class: c.cls }, h('abbr', { title: c.title, text: c.label }))),
  ));
  let previousZone = '';
  const tbody = h('tbody', null, ...rows.map((row) => {
    const zone = row.zone && HEX.test(row.zone.color || '') ? row.zone.color : null;
    const tr = h('tr', {
      class: [row.playing && 'is-playing', highlight?.has(row.team?.id) && 'is-highlight'],
      'data-team-id': row.team?.id,
    },
      h('td', { class: 'col-pos', style: zone ? { '--zone': zone } : null },
        String(row.position),
        row.tied ? h('abbr', { class: 'tied-mark', title: 'Empate não desfeito pelos critérios (ordem alfabética)', text: '=' }) : null,
      ),
      teamCell(row, previousZone),
      ...COLUMNS.map((c) => h('td', { class: c.cls, text: c.key === 'goal_difference' ? signed(row[c.key] ?? 0) : (row[c.key] ?? 0) })),
    );
    previousZone = row.zone?.name || '';
    return tr;
  }));
  const table = h('table', { class: 'table' }, groupCaption(standings, group, multiple, anyPlaying), thead, tbody);
  return h('div', { class: 'card standings__group', 'data-group-id': group.id }, h('div', { class: 'table-scroll' }, table), multiple ? null : footer(standings, opts, rows));
}

function footer(standings, opts, rows = []) {
  const parts = [];
  if (opts.legend !== false && standings.legend?.length) {
    parts.push(h('ul', { class: 'legend', 'aria-label': 'Legenda das zonas' }, ...standings.legend.map((item) => h('li', { class: 'legend__item' },
      swatch(item.color), h('span', { text: item.name }), rangeLabel(item) ? h('span', { class: 'legend__range', text: rangeLabel(item) }) : null,
    ))));
  }
  if (opts.criteria !== false && standings.criteria?.length) {
    const tied = rows.some((r) => r.tied);
    parts.push(h('div', { class: 'criteria' },
      h('p', { class: 'criteria__title', text: 'Desempate: ' }),
      h('ol', { class: 'criteria__list' }, ...standings.criteria.map((c) => h('li', { text: c.label }))),
      tied ? h('p', { class: 'criteria__note' }, h('span', { class: 'tied-mark', 'aria-hidden': 'true', text: '=' }), ' empate que os critérios não desfizeram (ordem alfabética).') : null,
    ));
  }
  return parts.length ? h('div', { class: 'standings__foot' }, ...parts) : null;
}

/**
 * Classificação de uma fase (StageStandingsOut, CONTRACT §3).
 * @param {object} standings StageStandingsOut
 * @param {{compact?: boolean, legend?: boolean, criteria?: boolean, highlightTeamIds?: Set<number>}} [opts]
 *   compact força a versão enxuta (sem GP/GC); sem ela, colunas somem por container query.
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
  const parts = groups.map((g) => groupTable(standings, g, opts, multiple));
  if (multiple) {
    const allRows = groups.flatMap((g) => g.rows || []);
    const foot = footer(standings, opts, allRows);
    if (foot) parts.push(h('div', { class: 'card' }, foot));
  }
  if (!parts.length) parts.push(h('p', { class: 'muted', text: 'Classificação ainda sem jogos.' }));
  el.dataset.stageId = standings?.stage_id ?? '';
  el.replaceChildren(...parts);
  return el;
}
