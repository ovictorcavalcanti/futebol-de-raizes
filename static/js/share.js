/**
 * Compartilhar no WhatsApp (docs/FRONTEND.md §Compartilhar).
 *
 * Jogo: mensagem formatada para o WhatsApp (placar em negrito, competição, autores dos gols e o
 * link /?jogo=<id>, que abre a home no dia do jogo com o card aberto) e uma imagem PNG 3:2
 * (escudos, times, placar, autores dos gols) desenhada num canvas, sem servidor. A imagem entra
 * na conversa como arquivo pela folha nativa do celular (Web Share); sem ela (computador), abre o
 * WhatsApp com a mensagem (api.whatsapp.com) e copia a imagem para colar. Nunca há "baixar".
 * A folha nativa só abre no clique do usuário (o Safari exige), então a imagem é desenhada quando
 * a janela abre e o botão chama o share direto.
 *
 * Classificação: a mesma janela, com a imagem da tabela (posição, escudo, nome, P J V E D SG,
 * faixa da zona e legenda) e a mensagem com uma linha por time.
 *
 * Resultados (competição ou dia): a mensagem com os jogos e o link, direto no WhatsApp.
 */
import { h, showToast } from './render.js';
import { icon } from './icons.js';
import { shieldSvg } from './crest.js';
import { formatWhen } from './format.js';

const SCORED = new Set(['live', 'finished', 'suspended']);
const ORIGIN_TAG = { penalty: 'pên.', own_goal: 'contra' };
const INTERVALS = new Set(['half_time', 'extra_half_time']);
const name = (team) => team?.name || team?.short_name || '?';

/** Placar à mostra: jogo começado (o cancelado só com gol), como o card. */
export function hasScore(match) {
  return SCORED.has(match.status) || (match.status === 'cancelled' && !!(match.home_score || match.away_score));
}

/** Link do jogo: a home no dia dele, com o card aberto. */
export function matchUrl(match, origin = globalThis.location?.origin || '') {
  return `${origin}/?jogo=${match.id}`;
}

/** Conversa do WhatsApp com a mensagem pronta (app no celular, WhatsApp Web no computador). */
export function whatsappUrl(text) {
  return `https://api.whatsapp.com/send?text=${encodeURIComponent(text)}`;
}

/** "Sport 2 × 1 Náutico" (com os pênaltis, se houver) ou "Sport × Náutico". */
export function scoreline(match) {
  if (!hasScore(match)) return `${name(match.home)} × ${name(match.away)}`;
  let line = `${name(match.home)} ${match.home_score ?? 0} × ${match.away_score ?? 0} ${name(match.away)}`;
  if (match.home_penalties != null && match.away_penalties != null) line += ` (pênaltis ${match.home_penalties} × ${match.away_penalties})`;
  return line;
}

/** Autores dos gols de um lado, na ordem do jogo: ["Fulano 12'", "Ciclano 80' (pên.)"]. */
export function scorers(match, side) {
  return (match.goals || []).filter((g) => g.team_side === side).map((g) => {
    let text = g.player || 'a confirmar';
    if (g.minute_label) text += ` ${g.minute_label}`;
    if (ORIGIN_TAG[g.origin]) text += ` (${ORIGIN_TAG[g.origin]})`;
    return text;
  });
}

/** "hoje às 16:00", "sáb, 18/10 às 16:00" */
const whenText = (value, now) => formatWhen(value, now).replace(' · ', ' às ').toLowerCase();

/** Situação do jogo: "Encerrado", "Ao vivo · 2º tempo", "sáb, 18/10 às 16:00"... */
function situation(match, now) {
  if (match.status === 'live') return INTERVALS.has(match.period) ? 'Intervalo' : ['Ao vivo', match.period_label].filter(Boolean).join(' · ');
  if ((match.status === 'scheduled' || match.status === 'delayed') && match.kickoff_at) {
    const when = whenText(match.kickoff_at, now);
    return match.status === 'delayed' ? `${match.status_label || 'Atrasado'} · ${when}` : when;
  }
  return match.status_label || '';
}

function walkoverText(match) {
  if (match.status !== 'finished' || !match.walkover) return '';
  const favored = [match.home, match.away].find((team) => team?.id != null && team.id === match.walkover.team_id);
  return `W.O. em favor do ${name(favored)}`;
}

/**
 * Mensagem do jogo para o WhatsApp (*negrito*): placar; competição · rodada · situação; W.O.;
 * "⚽ *Time:* autores" por lado; o link.
 */
export function matchShareText(match, now = Date.now(), url = '') {
  const lines = [`*${scoreline(match)}*`];
  lines.push([match.competition?.short_name || match.competition?.name, match.round?.name, situation(match, now)].filter(Boolean).join(' · '));
  const wo = walkoverText(match);
  if (wo) lines.push(match.walkover.reason ? `${wo} — ${match.walkover.reason}` : wo);
  const goals = ['home', 'away'].map((side) => [side, scorers(match, side)]).filter(([, list]) => list.length);
  if (goals.length) lines.push('', ...goals.map(([side, list]) => `⚽ *${name(match[side])}:* ${list.join(', ')}`));
  if (url) lines.push('', `🔗 ${url}`);
  return lines.join('\n');
}

/**
 * Mensagem de resultados para o WhatsApp: o título em negrito e uma linha por jogo, na ordem da
 * página (placar; a situação entre parênteses se não acabou; o horário se não começou), e o link.
 */
export function resultsShareText(title, matches, now = Date.now(), url = '') {
  const lines = matches.map((m) => {
    if (!hasScore(m)) return `${scoreline(m)} — ${situation(m, now)}`;
    if (m.status === 'finished') return m.walkover ? `${scoreline(m)} (W.O.)` : scoreline(m);
    return `${scoreline(m)} (${situation(m, now).toLowerCase()})`;
  });
  return [`*${title}*`, '', ...lines, ...(url ? ['', `🔗 ${url}`] : [])].join('\n');
}

/** Resultados direto no WhatsApp (só texto: a conversa abre com a mensagem pronta). */
export function shareResultsOnWhatsApp(title, matches, { now = Date.now(), url = '', win = window } = {}) {
  win.open(whatsappUrl(resultsShareText(title, matches, now, url)), '_blank', 'noopener');
}

const signed = (n) => (n > 0 ? `+${n}` : String(n));
const pts = (n) => `${n} pt${Math.abs(n) === 1 ? '' : 's'}`;

/** Nome do grupo para o título: "A" (só letra/número) vira "Grupo A"; os outros ficam como vieram
 *  ("Grupo B", "Chave 1", "Zona Norte"); sem nome, vazio. */
export const groupLabel = (groupName) => (!groupName ? '' : /^[a-z0-9]{1,3}$/i.test(groupName) ? `Grupo ${groupName}` : groupName);

/** "Classificação", "Classificação do Grupo A" ou, noutro nome, "Classificação · Chave 1". */
export const standingsHeading = (label) => (!label ? 'Classificação' : /^grupo\b/i.test(label) ? `Classificação do ${label}` : `Classificação · ${label}`);

/**
 * Mensagem da classificação para o WhatsApp: `*Classificação*` (ou `*Classificação do Grupo A*`),
 * competição · fase, o aviso de parcial (com jogo em andamento, na tabela ao vivo), uma linha por
 * time na ordem da tabela ("1. Sport 🔴 — 13 pts · 5J · GP 11 · SG +8"; 🔴 = em jogo agora) e o link.
 */
export function standingsShareText({ competition = '', stage = '', group: label = '' }, group, { partial = false, url = '' } = {}) {
  const rows = (group?.rows || []).map((r) =>
    `${r.rank ?? r.position}. ${name(r.team)}${partial && r.playing ? ' 🔴' : ''} — ${pts(r.points ?? 0)} · ${r.played ?? 0}J · GP ${r.goals_for ?? 0} · SG ${signed(r.goal_difference ?? 0)}`);
  const head = [`*${standingsHeading(label)}*`, [competition, stage].filter(Boolean).join(' · ')];
  if (partial) head.push('🔴 Parcial: há jogos em andamento');
  return [...head.filter(Boolean), '', ...rows, ...(url ? ['', `🔗 ${url}`] : [])].join('\n');
}


/* --- Imagem ------------------------------------------------------------------------------- */

// 3:2, deitada: lê bem na conversa do WhatsApp
const W = 1200;
const H = 800;
const COLORS = { paper: '#FBF6EC', ink: '#1B1712', ink3: '#6F675A', line: '#E4DACA', azul: '#12306B', stripe: ['#C8102E', '#F2B705', '#0E8A4A', '#12306B'] };

function loadImage(src, { cors = false } = {}) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    if (cors) img.crossOrigin = 'anonymous'; // escudo de outro site sem CORS falha aqui (não suja o canvas)
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`imagem: ${src.slice(0, 60)}`));
    img.src = src;
  });
}

/** Escudo do time (crest_url) ou, sem ele ou se não carregar, o escudo desenhado do card. */
async function crestImage(team) {
  if (team?.crest_url) {
    try {
      return await loadImage(team.crest_url, { cors: true });
    } catch {
      // segue com o escudo desenhado
    }
  }
  const svg = shieldSvg(team, { size: 300 });
  svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  // como <img>, o SVG não vê as fontes nem o CSS da página (`.crest text`): sigla condensada do sistema
  svg.querySelector('text')?.setAttribute('style', 'font-family: "Barlow Condensed", "Arial Narrow", Arial, sans-serif; font-weight: 700');
  return loadImage(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(svg))}`);
}

/**
 * Um tamanho de fonte para um grupo de textos (o maior em que o mais largo cabe, até `min`):
 * os autores dos gols, ou os dois nomes, saem todos do mesmo tamanho. Deixa `ctx.font` nele.
 */
function groupFont(ctx, texts, { weight = 400, size, family, maxWidth, min = 20 }) {
  let px = size;
  for (;;) {
    ctx.font = `${weight} ${px}px ${family}`;
    if (px <= min || Math.max(0, ...texts.map((text) => ctx.measureText(text).width)) <= maxWidth) return px;
    px -= 2;
  }
}

/** Texto que ainda não cabe no tamanho mínimo: cortado com "…". */
function clip(ctx, text, maxWidth) {
  let out = text;
  while (out.length > 1 && ctx.measureText(out).width > maxWidth) out = `${out.slice(0, -2)}…`;
  return out;
}

function drawCrest(ctx, img, cx, cy, box) {
  const w = img.naturalWidth || box;
  const hgt = img.naturalHeight || box;
  const scale = Math.min(box / w, box / hgt);
  ctx.drawImage(img, cx - (w * scale) / 2, cy - (hgt * scale) / 2, w * scale, hgt * scale);
}

function stripe(ctx, y, height, width = W) {
  COLORS.stripe.forEach((color, i) => {
    ctx.fillStyle = color;
    ctx.fillRect((width / 4) * i, y, width / 4, height);
  });
}

/** As famílias do site (tokens do CSS), já carregadas para o canvas. */
async function siteFonts() {
  const root = getComputedStyle(document.documentElement);
  const font = {
    brand: root.getPropertyValue('--font-brand').trim() || 'Georgia, serif',
    ui: root.getPropertyValue('--font-ui').trim() || 'system-ui, sans-serif',
    num: root.getPropertyValue('--font-num').trim() || 'system-ui, sans-serif',
  };
  try {
    await Promise.all([`400 40px ${font.brand}`, `600 40px ${font.num}`, `700 40px ${font.num}`, `500 40px ${font.ui}`, `600 40px ${font.ui}`].map((f) => document.fonts.load(f)));
  } catch {
    // sem as fontes do site, o canvas usa as do sistema
  }
  return font;
}

const toBlob = (canvas) => new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('toBlob'))), 'image/png'));

/**
 * Imagem do jogo (PNG 1200 × 800): competição e situação, escudos, times, placar e autores dos
 * gols (só eles, todos no mesmo tamanho), com o nome do site no rodapé. Tipos moderados, com
 * respiro: a imagem é lida no celular, mas não é um cartaz. O bloco do jogo (escudos, placar,
 * nomes e gols) fica no meio da altura livre, com ou sem gols.
 * @returns {Promise<Blob>}
 */
export async function renderMatchImage(match, { now = Date.now(), brand = '', host = globalThis.location?.host || '' } = {}) {
  const font = await siteFonts();
  const [homeCrest, awayCrest] = await Promise.all([crestImage(match.home), crestImage(match.away)]);

  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = COLORS.paper;
  ctx.fillRect(0, 0, W, H);
  stripe(ctx, 0, 10);
  stripe(ctx, H - 10, 10);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  const line = (text, x, y, maxWidth) => ctx.fillText(clip(ctx, text, maxWidth), x, y);
  const [left, right, middle] = [330, W - 330, W / 2]; // centro de cada time e do placar

  // competição; rodada e situação
  const competition = match.competition?.name || '';
  ctx.fillStyle = COLORS.ink;
  groupFont(ctx, [competition], { size: 36, family: font.brand, maxWidth: 1000 });
  line(competition, middle, 88, 1000);
  const context = [match.round?.name, situation(match, now)].filter(Boolean).join(' · ').toUpperCase();
  ctx.fillStyle = COLORS.ink3;
  groupFont(ctx, [context], { weight: 700, size: 22, family: font.num, maxWidth: 1000 });
  line(context, middle, 124, 1000);

  // bloco do jogo: escudos, placar, nomes e os gols embaixo, centrado entre o cabeçalho e o rodapé
  const columns = [['home', left], ['away', right]].map(([side, x]) => {
    const goals = scorers(match, side);
    return [goals.length > 6 ? [...goals.slice(0, 5), `+${goals.length - 5} gols`] : goals, x];
  });
  const rows = Math.max(...columns.map(([texts]) => texts.length));
  const blockHeight = rows ? 302 + (rows - 1) * 36 + 8 : 216;
  const [top, bottom] = [150, H - 110];
  const y0 = Math.max(top, Math.round(top + (bottom - top - blockHeight) / 2));

  // escudos e nomes (os dois do mesmo tamanho)
  drawCrest(ctx, homeCrest, left, y0 + 80, 160);
  drawCrest(ctx, awayCrest, right, y0 + 80, 160);
  const names = [name(match.home).toUpperCase(), name(match.away).toUpperCase()];
  ctx.fillStyle = COLORS.ink;
  groupFont(ctx, names, { weight: 700, size: 28, family: font.num, maxWidth: 380, min: 20 });
  line(names[0], left, y0 + 208, 380);
  line(names[1], right, y0 + 208, 380);

  // placar (ou "×", antes do jogo)
  if (hasScore(match)) {
    ctx.font = `400 96px ${font.brand}`;
    ctx.textAlign = 'right';
    ctx.fillText(String(match.home_score ?? 0), middle - 34, y0 + 114);
    ctx.textAlign = 'left';
    ctx.fillText(String(match.away_score ?? 0), middle + 34, y0 + 114);
    ctx.textAlign = 'center';
    ctx.fillStyle = COLORS.ink3;
    ctx.font = `400 40px ${font.brand}`;
    ctx.fillText('×', middle, y0 + 100);
  } else {
    ctx.fillStyle = COLORS.ink3;
    ctx.font = `400 60px ${font.brand}`;
    ctx.fillText('×', middle, y0 + 104);
  }
  ctx.fillStyle = COLORS.ink3;
  const under = match.home_penalties != null && match.away_penalties != null
    ? `Pênaltis ${match.home_penalties} × ${match.away_penalties}` : walkoverText(match);
  if (under) {
    groupFont(ctx, [under], { weight: 600, size: 20, family: font.ui, maxWidth: 260 });
    line(under, middle, y0 + 156, 260);
  }

  // autores dos gols, cada lado embaixo do seu escudo, todos no mesmo tamanho (até 6 linhas por lado)
  if (rows) {
    ctx.strokeStyle = COLORS.line;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(120, y0 + 252);
    ctx.lineTo(W - 120, y0 + 252);
    ctx.stroke();
    ctx.fillStyle = COLORS.ink;
    groupFont(ctx, columns.flatMap(([texts]) => texts), { weight: 500, size: 22, family: font.ui, maxWidth: 420, min: 16 });
    for (const [texts, x] of columns) texts.forEach((text, i) => line(text, x, y0 + 302 + i * 36, 420));
  }

  // rodapé: site
  ctx.fillStyle = COLORS.azul;
  groupFont(ctx, [brand], { size: 24, family: font.brand, maxWidth: 1000 });
  line(brand, middle, H - 70, 1000);
  ctx.fillStyle = COLORS.ink3;
  groupFont(ctx, [host], { weight: 600, size: 17, family: font.ui, maxWidth: 1000 });
  line(host, middle, H - 42, 1000);

  return toBlob(canvas);
}

const HEX = /^#[0-9a-f]{6}$/i;
const SW = 1080; // largura da imagem da classificação; a altura cresce com o número de times
const ROW = 54;

/**
 * Imagem da classificação de um grupo (PNG 1080 de largura): competição; "Classificação" ou
 * "Classificação do Grupo A"; a fase (e "parcial", na tabela ao vivo com jogo em andamento); uma
 * linha por time — faixa da zona, posição, escudo, nome (todos do mesmo tamanho; bolinha vermelha
 * = em jogo agora), P J V E D GP SG —; a legenda (zonas e a bolinha) e o site no rodapé.
 * @returns {Promise<Blob>}
 */
export async function renderStandingsImage(standings, group, { competition = '', stage = '', group: label = '', partial = false, brand = '', host = globalThis.location?.host || '' } = {}) {
  const font = await siteFonts();
  const rows = group?.rows || [];
  const crests = await Promise.all(rows.map((row) => crestImage(row.team)));
  const top = 236; // topo da 1ª linha

  // legenda: as zonas (quadrado na cor) e, com time em jogo, a bolinha vermelha; em linhas
  // centradas, quebrando quando não cabe
  const measure = document.createElement('canvas').getContext('2d');
  measure.font = `500 20px ${font.ui}`;
  const items = (standings?.legend || []).filter((zone) => zone?.name).map((zone) => {
    const range = zone.from == null ? '' : zone.to != null && zone.to !== zone.from ? ` ${zone.from}º–${zone.to}º` : ` ${zone.from}º`;
    return { kind: 'zone', color: HEX.test(zone.color || '') ? zone.color : COLORS.ink3, text: `${zone.name}${range}` };
  });
  if (rows.some((row) => row.playing)) {
    items.push({ kind: 'dot', color: '#C8102E', text: partial ? 'Em jogo agora (parcial)' : 'Em jogo agora (ainda não conta)' });
  }
  const gap = 34;
  const legendLines = [];
  for (const item of items) {
    item.width = 26 + measure.measureText(item.text).width;
    const current = legendLines.at(-1);
    if (current && current.width + gap + item.width <= SW - 96) {
      current.items.push(item);
      current.width += gap + item.width;
    } else {
      legendLines.push({ items: [item], width: item.width });
    }
  }
  const height = top + rows.length * ROW + (legendLines.length ? 34 + legendLines.length * 34 : 24) + 110;

  const canvas = document.createElement('canvas');
  canvas.width = SW;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = COLORS.paper;
  ctx.fillRect(0, 0, SW, height);
  stripe(ctx, 0, 10, SW);
  stripe(ctx, height - 10, 10, SW);
  ctx.textBaseline = 'alphabetic';
  const line = (text, x, y, maxWidth) => ctx.fillText(clip(ctx, text, maxWidth), x, y);

  // competição; "Classificação" (do grupo, se for); a fase (e "parcial")
  ctx.textAlign = 'center';
  ctx.fillStyle = COLORS.ink;
  groupFont(ctx, [competition], { size: 36, family: font.brand, maxWidth: 960 });
  line(competition, SW / 2, 78, 960);
  const heading = standingsHeading(label).toUpperCase();
  groupFont(ctx, [heading], { weight: 700, size: 28, family: font.num, maxWidth: 960 });
  line(heading, SW / 2, 122, 960);
  const sub = [stage, partial ? 'parcial: há jogos em andamento' : ''].filter(Boolean).join(' · ').toUpperCase();
  ctx.fillStyle = partial ? '#A50D26' : COLORS.ink3;
  groupFont(ctx, [sub], { weight: 600, size: 20, family: font.num, maxWidth: 960 });
  line(sub, SW / 2, 156, 960);

  // cabeçalho das colunas
  const X = { pos: 96, crest: 142, name: 172, P: 680, J: 736, V: 788, E: 840, D: 892, GP: 948, SG: 1008 };
  const stats = [['P', 'points'], ['J', 'played'], ['V', 'won'], ['E', 'drawn'], ['D', 'lost'], ['GP', 'goals_for'], ['SG', 'goal_difference']];
  ctx.fillStyle = COLORS.ink3;
  ctx.font = `700 20px ${font.num}`;
  ctx.fillText('#', X.pos, 212);
  for (const [col] of stats) ctx.fillText(col, X[col], 212);
  ctx.textAlign = 'left';
  ctx.fillText('TIME', X.name, 212);

  // linhas: faixa da zona, posição, escudo, nome (um tamanho para todos) e os números
  const names = rows.map((row) => name(row.team));
  const nameMax = X.P - 40 - X.name;
  const namePx = groupFont(ctx, names, { weight: 600, size: 25, family: font.ui, maxWidth: nameMax, min: 18 });
  rows.forEach((row, i) => {
    const y = top + i * ROW;
    const base = y + 35;
    if (i % 2 === 0) {
      ctx.fillStyle = 'rgba(27, 23, 18, 0.045)';
      ctx.fillRect(48, y, SW - 96, ROW);
    }
    if (HEX.test(row.zone?.color || '')) {
      ctx.fillStyle = row.zone.color;
      ctx.fillRect(48, y + 6, 6, ROW - 12);
    }
    ctx.fillStyle = COLORS.ink;
    ctx.textAlign = 'center';
    ctx.font = `700 25px ${font.num}`;
    ctx.fillText(String(row.rank ?? row.position ?? i + 1), X.pos, base);
    drawCrest(ctx, crests[i], X.crest, y + ROW / 2, 34);
    ctx.textAlign = 'left';
    ctx.font = `600 ${namePx}px ${font.ui}`;
    const shown = clip(ctx, names[i], nameMax);
    ctx.fillText(shown, X.name, base);
    if (row.playing) { // em jogo agora
      ctx.fillStyle = '#C8102E';
      ctx.beginPath();
      ctx.arc(X.name + ctx.measureText(shown).width + 16, y + ROW / 2, 6, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.textAlign = 'center';
    for (const [col, key] of stats) {
      ctx.fillStyle = col === 'P' ? COLORS.ink : '#3D372F';
      ctx.font = `${col === 'P' ? 700 : 600} 24px ${font.num}`;
      const value = row[key] ?? 0;
      ctx.fillText(key === 'goal_difference' ? signed(value) : String(value), X[col], base);
    }
  });

  // legenda, centrada: "■ Semifinal 1º–4º   ■ Rebaixamento 7º–8º   ● Em jogo agora (parcial)"
  ctx.font = `500 20px ${font.ui}`;
  ctx.textAlign = 'left';
  legendLines.forEach((legendLine, i) => {
    let x = (SW - legendLine.width) / 2;
    const y = top + rows.length * ROW + 48 + i * 34;
    for (const item of legendLine.items) {
      ctx.fillStyle = item.color;
      if (item.kind === 'dot') {
        ctx.beginPath();
        ctx.arc(x + 8, y - 7, 7, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.fillRect(x, y - 15, 16, 16);
      }
      ctx.fillStyle = COLORS.ink3;
      ctx.fillText(item.text, x + 26, y);
      x += item.width + gap;
    }
  });

  // rodapé: site
  ctx.textAlign = 'center';
  ctx.fillStyle = COLORS.azul;
  groupFont(ctx, [brand], { size: 24, family: font.brand, maxWidth: 960 });
  line(brand, SW / 2, height - 66, 960);
  ctx.fillStyle = COLORS.ink3;
  groupFont(ctx, [host], { weight: 600, size: 17, family: font.ui, maxWidth: 960 });
  line(host, SW / 2, height - 38, 960);

  return toBlob(canvas);
}

/* --- Janela de compartilhar o jogo -------------------------------------------------------- */

/** Copia a imagem para colar na conversa (WhatsApp Web); falha em silêncio. */
async function copyImage(blob, nav) {
  if (!blob || !nav?.clipboard?.write || typeof ClipboardItem !== 'function') return false;
  try {
    await nav.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
    return true;
  } catch {
    return false;
  }
}

/**
 * Janela de compartilhar: a prévia da imagem e "Compartilhar no WhatsApp". Com Web Share de
 * arquivos (celular), a imagem vai como arquivo com a mensagem na legenda; sem ela, o botão é o
 * link do WhatsApp com a mensagem e copia a imagem para colar. Nunca há "baixar".
 * @returns {HTMLDialogElement}
 */
function openShareSheet({ heading, title, text, filename, alt, size: [width, height], render, nav }) {
  const probe = typeof File === 'function' ? new File([''], filename, { type: 'image/png' }) : null;
  const canShareFiles = !!(nav?.share && nav.canShare && probe && nav.canShare({ files: [probe] }));

  const preview = h('img', { class: 'share-sheet__img', alt, width, height, hidden: true });
  const status = h('p', { class: 'share-sheet__status', text: 'Gerando a imagem…' });
  const label = () => [icon('share'), 'Compartilhar no WhatsApp'];
  const link = () => h('a', { class: 'btn btn--primary', href: whatsappUrl(text), target: '_blank', rel: 'noopener', 'data-hook': 'share-whatsapp' }, ...label());
  const send = canShareFiles
    ? h('button', { type: 'button', class: 'btn btn--primary', disabled: true, 'data-hook': 'share-whatsapp' }, ...label())
    : link();
  const dialog = h('dialog', { class: 'dialog share-sheet', 'aria-labelledby': 'share-title' },
    h('form', { method: 'dialog', class: 'dialog__form' },
      h('div', { class: 'dialog__head' }, h('h2', { id: 'share-title', class: 'dialog__title', text: heading })),
      h('div', { class: 'dialog__body' },
        h('p', { class: 'share-sheet__title', text: title }),
        h('figure', { class: 'share-sheet__preview' }, preview, status),
      ),
      h('div', { class: 'dialog__foot' },
        h('button', { type: 'submit', value: 'cancel', class: 'btn btn--ghost', formnovalidate: true }, 'Fechar'),
        send),
    ));

  let blob = null;
  let objectUrl = '';
  if (canShareFiles) {
    send.addEventListener('click', () => {
      if (!blob) return;
      // a mensagem vai na legenda (com arquivo, o WhatsApp ignora o campo `url`)
      nav.share({ files: [new File([blob], filename, { type: 'image/png' })], text }).catch((error) => {
        if (error?.name !== 'AbortError') window.open(whatsappUrl(text), '_blank', 'noopener'); // pelo menos a mensagem
      });
    });
  } else {
    // o link abre o WhatsApp com a mensagem; a imagem vai para a área de transferência
    send.addEventListener('click', () => {
      copyImage(blob, nav).then((copied) => {
        if (copied) showToast('Imagem copiada: cole na conversa (Ctrl+V).', { kind: 'ok', timeout: 8000 });
      });
    });
  }
  dialog.addEventListener('close', () => {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    dialog.remove();
  });
  document.body.append(dialog);
  dialog.showModal();

  render().then((png) => {
    blob = png;
    objectUrl = URL.createObjectURL(png);
    preview.src = objectUrl;
    preview.hidden = false;
    status.hidden = true;
    send.disabled = false;
  }, () => {
    status.textContent = 'Não deu para gerar a imagem. A mensagem continua valendo.';
    if (canShareFiles) send.replaceWith(link());
  });
  return dialog;
}

const brandName = () => document.querySelector('meta[name="application-name"]')?.content || '';

/**
 * Compartilhar o jogo: imagem (renderMatchImage) e mensagem (matchShareText, com o link).
 * @param {object} match MatchOut
 * @param {{now?: number, nav?: Navigator}} [opts]
 */
export function openMatchShare(match, { now = Date.now(), nav = globalThis.navigator } = {}) {
  const title = scoreline(match);
  return openShareSheet({
    heading: 'Compartilhar jogo', title, text: matchShareText(match, now, matchUrl(match)),
    filename: `jogo-${match.id}.png`, alt: `Imagem do jogo: ${title}`, size: [600, 400], nav,
    render: () => renderMatchImage(match, { now, brand: brandName() }),
  });
}

/**
 * Compartilhar a classificação de um grupo: imagem (renderStandingsImage) e mensagem
 * (standingsShareText: "competição · fase · grupo", uma linha por time, o link).
 * @param {object} standings StageStandingsOut (legenda, fase, ao vivo)
 * @param {object} group o grupo da tabela
 * @param {{competition?: string, groupName?: string, url?: string, nav?: Navigator}} [opts]
 */
export function openStandingsShare(standings, group, { competition = '', groupName = '', url = '', nav = globalThis.navigator } = {}) {
  const parts = { competition, stage: standings?.stage_name || '', group: groupLabel(groupName) };
  const title = [standingsHeading(parts.group), competition].filter(Boolean).join(' · ');
  // parcial: tabela ao vivo com jogo em andamento (a oficial não conta o jogo que está rolando)
  const partial = standings?.kind === 'live' && (group?.rows || []).some((row) => row.playing);
  return openShareSheet({
    heading: 'Compartilhar classificação', title, text: standingsShareText(parts, group, { partial, url }),
    filename: `classificacao-${standings?.stage_id ?? ''}${group?.id ? `-${group.id}` : ''}.png`, alt: `Imagem da ${title}`,
    size: [600, 600], nav,
    render: () => renderStandingsImage(standings, group, { ...parts, partial, brand: brandName() }),
  });
}
