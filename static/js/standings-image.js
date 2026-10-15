/**
 * Imagem da classificação para compartilhar (WhatsApp, Instagram): um <canvas> na identidade do
 * Futebol de Raízes com a tabela completa de um grupo (#, Time, P, J, V, E, D, GP, GC, SG, %), o
 * título da competição, a fase e o grupo, as legendas e a fonte. O toque no botão abre direto o
 * menu de compartilhar do sistema (WhatsApp, Instagram…) com o arquivo; sem esse menu (desktop sem
 * compartilhamento de arquivo), a imagem é baixada.
 *
 * Só desenho: ordem, zonas, cores e punições chegam prontas do back (como em standings.js).
 */
import { h, showToast } from './render.js';
import { icon } from './icons.js';
import { SHIELD, contrastInk } from './crest.js';
import { COLUMNS, adjustmentNote, rangeLabel } from './standings.js';
import { LOCALE, TIME_ZONE } from './format.js';

export const SITE = 'futebolderaizes.com';
const BRAND = 'Futebol de Raízes';
const HEX = /^#[0-9a-f]{6}$/i;

/** Colunas da imagem: as da tabela da página e o aproveitamento. */
export const IMAGE_COLUMNS = [...COLUMNS, { key: 'performance', label: '%', title: 'Aproveitamento' }];
const COLUMN_WIDTH = { points: 72, played: 52, won: 52, drawn: 52, lost: 52, goals_for: 56, goals_against: 56, goal_difference: 64, performance: 88 };
const PLAYING = 'JOGANDO AGORA';

// cores e fontes da identidade (docs/IDENTIDADE.md), tema claro
const C = {
  paper: '#FBF6EC', surface: '#FFFFFF', surface2: '#F3EBDB', zebra: '#FCF9F2',
  ink: '#1B1712', ink2: '#4B443A', ink3: '#6F675A', line: '#E5DAC5',
  azul: '#12306B', vermelho: '#C8102E', amarelo: '#F2B705', verde: '#0E8A4A',
  pts: 'rgba(243, 235, 219, 0.6)', // coluna de pontos: --surface-2 a 60%, como na página
};
const FONT = {
  title: '"Alfa Slab One", Georgia, serif',
  num: '"Barlow Condensed", "Arial Narrow", sans-serif',
  ui: 'Barlow, "Segoe UI", system-ui, sans-serif',
};

// medidas (px da imagem): pelo menos 1080 de largura, e mais larga quando o nome mais longo com o
// "JOGANDO AGORA" pede (tudo no tamanho cheio); a altura acompanha o número de times e tenta caber em 4:5
const MIN_W = 1080;
const MAX_W = 1600;
const PAD = 56;
const CARD_X = 40;
const POS_W = 80; // da borda do card ao escudo
const NAME_X = POS_W + 36 + 14; // escudo e respiro
const NUMS_W = Object.values(COLUMN_WIDTH).reduce((a, b) => a + b, 0);
const NAME_GAP = 24; // do nome (ou da etiqueta) à coluna de pontos
const STRIPE = 12;
const THEAD = 56;
const FOOT_H = 124;
const TARGET_H = 1350;

/** Aproveitamento (%), com uma casa: pontos ganhos em campo (sem punição nem bonificação) sobre os disputados. */
export function performance(row, points) {
  const played = Number(row?.played) || 0;
  if (!played) return 0;
  const win = Number(points?.win) || 3;
  const earned = (Number(row.points) || 0) - (Number(row.points_adjustment) || 0);
  return Math.max(0, Math.min(100, Math.round((earned / (played * win)) * 1000) / 10));
}

const PERCENT = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });

/** Texto de uma célula numérica: saldo com sinal, como na tabela da página; aproveitamento "77,8". */
export function cellText(row, key, points) {
  if (key === 'performance') return PERCENT.format(performance(row, points));
  const n = Number(row?.[key]) || 0;
  return key === 'goal_difference' && n > 0 ? `+${n}` : String(n);
}

/** Título (competição e temporada) e a linha da fase e do grupo. */
export function imageHeading(standings, group, context = {}) {
  const title = [context.competition, context.season].filter(Boolean).join(' ') || BRAND;
  const stage = standings?.stage_name || '';
  const groupName = group?.name && group.name !== 'Tabela' && group.name !== stage ? group.name : '';
  return { title, sub: [stage, groupName].filter(Boolean).join(' · ') };
}

/** "classificacao-copa-pernambuco-2026-fase-de-grupos-grupo-a.png" */
export function imageFileName(heading) {
  const slug = `${heading.title} ${heading.sub}`.normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return `classificacao-${slug || 'futebol-de-raizes'}.png`;
}

const STAMP = new Intl.DateTimeFormat(LOCALE, { timeZone: TIME_ZONE, day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

/** "Atualizada em 15/10/2026 às 22:30" (horário de Brasília) */
export function stampText(now) {
  const p = Object.fromEntries(STAMP.formatToParts(now).map((part) => [part.type, part.value]));
  return `Atualizada em ${p.day}/${p.month}/${p.year} às ${p.hour}:${p.minute}`;
}

/* ---------------------------------------------------------------- desenho */

function loadImage(src) {
  return new Promise((resolve) => {
    const img = new Image();
    // escudo de outro domínio sem CORS falha aqui (em vez de sujar o canvas) e vira o escudo desenhado
    img.crossOrigin = 'anonymous';
    const timer = setTimeout(() => resolve(null), 4000);
    img.onload = () => { clearTimeout(timer); resolve(img); };
    img.onerror = () => { clearTimeout(timer); resolve(null); };
    img.src = src;
  });
}

async function loadFonts() {
  if (!document.fonts?.load) return;
  const sample = `${BRAND} ÁÂÃÇÉÊÍÓÔÕÚáâãçéêíóôõú 0123456789 ºª·×+%#*`;
  await Promise.all([
    `400 56px ${FONT.title}`, `700 30px ${FONT.num}`, `600 28px ${FONT.num}`,
    `400 24px ${FONT.ui}`, `600 28px ${FONT.ui}`,
  ].map((f) => document.fonts.load(f, sample).catch(() => null)));
}

const setFont = (ctx, weight, size, family) => { ctx.font = `${weight} ${size}px ${family}`; };
const setSpacing = (ctx, px) => { if ('letterSpacing' in ctx) ctx.letterSpacing = `${px}px`; };

function roundRect(ctx, x, y, w, hgt, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, hgt, r);
  else ctx.rect(x, y, w, hgt);
}

function wrapWords(ctx, text, maxWidth) {
  const lines = [];
  let line = '';
  for (const word of String(text).split(/\s+/).filter(Boolean)) {
    const next = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(next).width > maxWidth) { lines.push(line); line = word; } else line = next;
  }
  if (line) lines.push(line);
  return lines;
}

function fitText(ctx, text, maxWidth) {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let cut = text;
  while (cut.length > 1 && ctx.measureText(`${cut}…`).width > maxWidth) cut = cut.slice(0, -1);
  return `${cut.trimEnd()}…`;
}

/** Itens de legenda em fila, quebrando para a linha de baixo quando não cabem. */
function flowLines(ctx, items, maxWidth, gap) {
  const lines = [[]];
  let x = 0;
  for (const item of items) {
    let w = item.swatch ? 32 : 0;
    for (const part of item.parts) { ctx.font = part.font; w += ctx.measureText(part.text).width; }
    if (x > 0 && x + w > maxWidth) { lines.push([]); x = 0; }
    lines[lines.length - 1].push({ item, x });
    x += w + gap;
  }
  return items.length ? lines : [];
}

function drawFlowLine(ctx, line, y) {
  for (const { item, x } of line) {
    let cx = PAD + x;
    if (item.swatch) {
      roundRect(ctx, cx, y - 11, 22, 22, 5);
      ctx.fillStyle = item.swatch;
      ctx.fill();
      cx += 32;
    }
    for (const part of item.parts) {
      ctx.font = part.font;
      ctx.fillStyle = part.color;
      ctx.fillText(part.text, cx, y);
      cx += ctx.measureText(part.text).width;
    }
  }
}

/** Escudo desenhado com as cores do time e a sigla (o mesmo de crest.js), em x/y com altura `size`. */
function drawShield(ctx, team, x, y, size) {
  const primary = HEX.test(team?.color_primary || '') ? team.color_primary : C.azul;
  let secondary = HEX.test(team?.color_secondary || '') ? team.color_secondary : '#FFFFFF';
  if (secondary.toLowerCase() === primary.toLowerCase()) secondary = contrastInk(primary);
  const label = (team?.short_name || team?.name || '?').slice(0, 4).toUpperCase();
  const shield = new Path2D(SHIELD);
  ctx.save();
  ctx.translate(x, y);
  ctx.scale(size / 46, size / 46);
  ctx.save();
  ctx.clip(shield);
  ctx.fillStyle = primary;
  ctx.fill(shield);
  ctx.fillStyle = secondary;
  ctx.fill(new Path2D('M0 33 40 15v8L0 41Z'));
  ctx.globalAlpha = 0.6;
  ctx.fill(new Path2D('M0 46h40v-3H0Z'));
  ctx.restore();
  setFont(ctx, 700, label.length >= 4 ? 10.5 : label.length === 3 ? 12.5 : 15, FONT.num);
  ctx.textAlign = 'center';
  ctx.lineJoin = 'round';
  ctx.lineWidth = 3;
  ctx.strokeStyle = primary;
  ctx.strokeText(label, 20, 19);
  ctx.fillStyle = contrastInk(primary);
  ctx.fillText(label, 20, 19);
  ctx.lineWidth = 2.2;
  ctx.strokeStyle = C.ink;
  ctx.stroke(shield);
  ctx.restore();
}

function drawCrest(ctx, team, img, x, y, size) {
  if (!img) { drawShield(ctx, team, x, y, size); return; }
  const box = size * (40 / 46);
  const k = Math.min(box / img.naturalWidth, size / img.naturalHeight) || 1;
  const w = img.naturalWidth * k;
  const hgt = img.naturalHeight * k;
  ctx.drawImage(img, x + (box - w) / 2, y + (size - hgt) / 2, w, hgt);
}

/**
 * Desenha a imagem de um grupo da classificação.
 * @param {{standings: object, group: object, adjustments?: object[], context?: {competition?: string, season?: string}, now?: Date}} args
 * @returns {Promise<HTMLCanvasElement>}
 */
export async function drawStandingsImage({ standings, group, adjustments = [], context = {}, now = new Date() }) {
  const rows = group?.rows || [];
  const heading = imageHeading(standings, group, context);
  const live = standings?.kind === 'live' && rows.some((r) => r.playing);
  const [logo, crests] = await Promise.all([
    loadImage(new URL('../img/logo-dark.svg', import.meta.url).href),
    Promise.all(rows.map((r) => (r.team?.crest_url ? loadImage(r.team.crest_url) : null))),
    loadFonts(),
  ]);

  const canvas = document.createElement('canvas');
  let ctx = canvas.getContext('2d');
  const teamName = (row) => row.team?.name || row.team?.short_name || '';
  setFont(ctx, 700, 22, FONT.num);
  setSpacing(ctx, 1.5);
  const tagW = Math.ceil(ctx.measureText(PLAYING).width) + 20;
  setSpacing(ctx, 0);
  setFont(ctx, 600, 28, FONT.ui);
  const nameArea = Math.max(0, ...rows.map((r) => ctx.measureText(teamName(r)).width + (r.playing ? 14 + tagW : 0)));
  const W = Math.min(MAX_W, Math.max(MIN_W, Math.ceil(2 * CARD_X + NAME_X + nameArea + NAME_GAP + NUMS_W + 16)));
  const CARD_W = W - 2 * CARD_X;
  const inner = W - 2 * PAD;

  // --- medidas
  let titleSize = 60;
  setFont(ctx, 400, titleSize, FONT.title);
  let titleLines = wrapWords(ctx, heading.title, inner);
  while (titleLines.length > 2 && titleSize > 40) {
    titleSize -= 4;
    setFont(ctx, 400, titleSize, FONT.title);
    titleLines = wrapWords(ctx, heading.title, inner);
  }
  const titleLH = Math.round(titleSize * 1.2);
  const headH = 48 + 56 + 32 + titleLines.length * titleLH + (heading.sub ? 12 + 40 : 0) + 44;

  const zoneItems = (standings?.legend || []).map((item) => ({
    swatch: HEX.test(item.color || '') ? item.color : C.ink3,
    parts: [
      { text: item.name, font: `600 24px ${FONT.ui}`, color: C.ink },
      rangeLabel(item) ? { text: ` ${rangeLabel(item)}`, font: `400 24px ${FONT.ui}`, color: C.ink3 } : null,
      item.condition ? { text: ` (${item.condition})`, font: `400 24px ${FONT.ui}`, color: C.ink3 } : null,
    ].filter(Boolean),
  }));
  const columnItems = IMAGE_COLUMNS.map((col) => ({
    parts: [
      { text: col.label, font: `700 24px ${FONT.num}`, color: C.ink },
      { text: ` ${col.title}`, font: `400 22px ${FONT.ui}`, color: C.ink2 },
    ],
  }));
  const zoneLines = flowLines(ctx, zoneItems, inner, 32);
  const columnLines = flowLines(ctx, columnItems, inner, 22);
  setFont(ctx, 400, 22, FONT.ui);
  const noteLines = [
    ...adjustments.flatMap((a) => wrapWords(ctx, `* ${adjustmentNote(a)}`, inner)),
    ...(rows.some((r) => r.tied) ? ['= Empatados em todos os critérios de desempate'] : []),
  ];
  const notesH = (zoneLines.length ? zoneLines.length * 40 + 14 : 0)
    + (noteLines.length ? noteLines.length * 32 + 14 : 0)
    + columnLines.length * 32;

  const fixed = headH + STRIPE + 36 + THEAD + 36 + notesH + 44 + FOOT_H;
  const n = Math.max(rows.length, 1);
  const rowH = Math.max(48, Math.min(72, Math.floor((TARGET_H - fixed) / n)));
  canvas.width = W;
  canvas.height = fixed + n * rowH;
  ctx = canvas.getContext('2d'); // mudar o tamanho zera o estado
  ctx.textBaseline = 'middle';

  // --- fundo e cabeçalho
  ctx.fillStyle = C.paper;
  ctx.fillRect(0, 0, W, canvas.height);
  ctx.fillStyle = C.azul;
  ctx.fillRect(0, 0, W, headH);
  if (logo) ctx.drawImage(logo, PAD, 48, 190, 56);
  else {
    setFont(ctx, 400, 34, FONT.title);
    ctx.fillStyle = '#FFFFFF';
    ctx.fillText(BRAND, PAD, 76);
  }
  if (live) {
    setFont(ctx, 700, 24, FONT.num);
    setSpacing(ctx, 2);
    const w = ctx.measureText('AO VIVO').width + 36;
    roundRect(ctx, W - PAD - w, 58, w, 38, 19);
    ctx.fillStyle = C.vermelho;
    ctx.fill();
    ctx.fillStyle = '#FFFFFF';
    ctx.textAlign = 'center';
    ctx.fillText('AO VIVO', W - PAD - w / 2, 78);
    ctx.textAlign = 'left';
    setSpacing(ctx, 0);
  }
  let y = 48 + 56 + 32;
  setFont(ctx, 400, titleSize, FONT.title);
  ctx.fillStyle = '#FFFFFF';
  titleLines.forEach((line, i) => ctx.fillText(line, PAD, y + i * titleLH + titleLH / 2));
  y += titleLines.length * titleLH;
  if (heading.sub) {
    setFont(ctx, 700, 36, FONT.num);
    setSpacing(ctx, 2);
    ctx.fillStyle = C.amarelo;
    ctx.fillText(fitText(ctx, heading.sub.toUpperCase(), inner), PAD, y + 12 + 20);
    setSpacing(ctx, 0);
  }
  [C.vermelho, C.amarelo, C.verde, C.azul].forEach((color, i) => {
    ctx.fillStyle = color;
    ctx.fillRect((i * W) / 4, headH, W / 4, STRIPE);
  });

  // --- tabela
  const top = headH + STRIPE + 36;
  const cardH = THEAD + n * rowH;
  ctx.save();
  ctx.shadowColor = 'rgba(27, 23, 18, 0.12)';
  ctx.shadowBlur = 24;
  ctx.shadowOffsetY = 6;
  roundRect(ctx, CARD_X, top, CARD_W, cardH, 20);
  ctx.fillStyle = C.surface;
  ctx.fill();
  ctx.restore();

  let right = CARD_X + CARD_W - 16;
  const cols = IMAGE_COLUMNS.slice().reverse().map((col) => {
    right -= COLUMN_WIDTH[col.key];
    return { ...col, center: right + COLUMN_WIDTH[col.key] / 2 };
  }).reverse();
  const posX = CARD_X + 44;
  const teamX = CARD_X + POS_W;
  const nameX = CARD_X + NAME_X;
  const nameMax = right - NAME_GAP - nameX;
  const ptsCol = cols.find((col) => col.key === 'points');

  ctx.save();
  roundRect(ctx, CARD_X, top, CARD_W, cardH, 20);
  ctx.clip();
  ctx.fillStyle = C.surface2;
  ctx.fillRect(CARD_X, top, CARD_W, THEAD);
  setFont(ctx, 700, 24, FONT.num);
  setSpacing(ctx, 1.5);
  ctx.fillStyle = C.ink3;
  ctx.textAlign = 'center';
  ctx.fillText('#', posX, top + THEAD / 2);
  cols.forEach((col) => {
    ctx.fillStyle = col.key === 'points' ? C.ink : C.ink3;
    ctx.fillText(col.label, col.center, top + THEAD / 2);
  });
  ctx.fillStyle = C.ink3;
  ctx.textAlign = 'left';
  ctx.fillText('TIME', teamX, top + THEAD / 2);
  setSpacing(ctx, 0);

  rows.forEach((row, i) => {
    const ry = top + THEAD + i * rowH;
    const cy = ry + rowH / 2;
    if (i % 2) {
      ctx.fillStyle = C.zebra;
      ctx.fillRect(CARD_X, ry, CARD_W, rowH);
    }
    ctx.fillStyle = C.pts;
    ctx.fillRect(ptsCol.center - COLUMN_WIDTH.points / 2, ry, COLUMN_WIDTH.points, rowH);
    ctx.fillStyle = C.line;
    ctx.fillRect(CARD_X, ry, CARD_W, i ? 1.5 : 2);
    const zone = row.zone && HEX.test(row.zone.color || '') ? row.zone.color : null;
    if (zone) {
      ctx.fillStyle = zone;
      ctx.fillRect(CARD_X, ry, 8, rowH);
    }
    // rank: empatados em todos os critérios repetem a posição, com "=" (como na página)
    const rank = String(row.rank ?? row.position ?? i + 1);
    const rankX = posX - (row.tied ? 6 : 0);
    setFont(ctx, 700, 30, FONT.num);
    ctx.fillStyle = C.ink;
    ctx.textAlign = 'center';
    ctx.fillText(rank, rankX, cy);
    ctx.textAlign = 'left';
    if (row.tied) {
      const after = rankX + ctx.measureText(rank).width / 2 + 2;
      setFont(ctx, 600, 24, FONT.num);
      ctx.fillStyle = C.ink3;
      ctx.fillText('=', after, cy);
    }

    const crest = Math.min(40, rowH - 14);
    drawCrest(ctx, row.team, crests[i], teamX, cy - crest / 2, crest);
    // nome no tamanho cheio (a imagem alarga para caber); só encolhe além da largura máxima
    const room = nameMax - (row.playing ? 14 + tagW : 0);
    let size = 28;
    setFont(ctx, 600, size, FONT.ui);
    while (size > 22 && ctx.measureText(teamName(row)).width > room) setFont(ctx, 600, (size -= 1), FONT.ui);
    const name = fitText(ctx, teamName(row), room);
    ctx.fillStyle = C.ink;
    ctx.fillText(name, nameX, cy);
    if (row.playing) {
      // em jogo agora: etiqueta de fundo vermelho, cantos retos
      const tx = nameX + ctx.measureText(name).width + 14;
      ctx.fillStyle = C.vermelho;
      ctx.fillRect(tx, cy - 16, tagW, 32);
      setFont(ctx, 700, 22, FONT.num);
      setSpacing(ctx, 1.5);
      ctx.fillStyle = '#FFFFFF';
      ctx.textAlign = 'center';
      ctx.fillText(PLAYING, tx + tagW / 2 + 0.75, cy + 1);
      ctx.textAlign = 'left';
      setSpacing(ctx, 0);
    }

    ctx.textAlign = 'center';
    for (const col of cols) {
      const text = cellText(row, col.key, standings?.points);
      if (col.key === 'points') {
        setFont(ctx, 700, 30, FONT.num);
        ctx.fillStyle = C.ink;
        ctx.fillText(text, col.center, cy);
        if (Number(row.points_adjustment)) {
          ctx.textAlign = 'left';
          ctx.fillStyle = C.vermelho;
          ctx.fillText('*', col.center + ctx.measureText(text).width / 2 + 1, cy - 6);
          ctx.textAlign = 'center';
        }
      } else {
        setFont(ctx, 600, 28, FONT.num);
        ctx.fillStyle = C.ink2;
        ctx.fillText(text, col.center, cy);
      }
    }
    ctx.textAlign = 'left';
  });
  ctx.restore();
  roundRect(ctx, CARD_X, top, CARD_W, cardH, 20);
  ctx.lineWidth = 2;
  ctx.strokeStyle = C.line;
  ctx.stroke();

  // --- legendas: zonas, punições, empate e colunas
  let ly = top + cardH + 36;
  for (const line of zoneLines) { drawFlowLine(ctx, line, ly + 20); ly += 40; }
  if (zoneLines.length) ly += 14;
  setFont(ctx, 400, 22, FONT.ui);
  ctx.fillStyle = C.ink2;
  for (const line of noteLines) { ctx.fillText(line, PAD, ly + 16); ly += 32; }
  if (noteLines.length) ly += 14;
  for (const line of columnLines) { drawFlowLine(ctx, line, ly + 16); ly += 32; }

  // --- fonte
  const fy = canvas.height - FOOT_H;
  ctx.fillStyle = C.azul;
  ctx.fillRect(0, fy, W, FOOT_H);
  drawFlowLine(ctx, [{
    x: 0,
    item: {
      parts: [
        { text: 'Fonte: ', font: `400 28px ${FONT.ui}`, color: C.amarelo },
        { text: BRAND, font: `600 28px ${FONT.ui}`, color: '#FFFFFF' },
        { text: ` · ${SITE}`, font: `400 28px ${FONT.ui}`, color: '#FFFFFF' },
      ],
    },
  }], fy + 46);
  setFont(ctx, 400, 22, FONT.ui);
  ctx.fillStyle = 'rgba(255, 255, 255, 0.82)';
  ctx.fillText(stampText(now), PAD, fy + 88);
  return canvas;
}

/* ---------------------------------------------------------------- compartilhar */

/** PNG da imagem de um grupo (o nome do arquivo leva a competição, a fase e o grupo). */
export async function standingsImageFile(args) {
  const canvas = await drawStandingsImage(args);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  if (!blob) throw new Error('imagem vazia');
  return new File([blob], imageFileName(imageHeading(args.standings, args.group, args.context)), { type: 'image/png' });
}

function canShareFile(file) {
  try {
    return typeof navigator.share === 'function' && !!navigator.canShare?.({ files: [file] });
  } catch {
    return false;
  }
}

// só o arquivo: com texto junto, alguns apps (Instagram; WhatsApp no iPhone) somem do menu ou recebem só o texto
const shareFile = (file) => navigator.share({ files: [file] });

function download(file) {
  const url = URL.createObjectURL(file);
  const link = h('a', { href: url, download: file.name, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  showToast('Imagem da classificação baixada.', { kind: 'ok' });
}

let confirmBox = null;

/** O navegador só abre o menu de compartilhar logo depois de um toque: se a imagem demorou e o toque
 *  venceu, um diálogo pede mais um. */
function askToShare(file) {
  if (!confirmBox) {
    const share = h('button', { type: 'button', class: 'btn btn--primary', 'data-hook': 'standings-image-share' }, icon('share'), 'Compartilhar');
    const el = h('dialog', { class: 'dialog', 'aria-labelledby': 'standings-image-title', 'data-hook': 'standings-image' },
      h('form', { method: 'dialog', class: 'dialog__form' },
        h('div', { class: 'dialog__head' }, h('h2', { id: 'standings-image-title', class: 'dialog__title', text: 'Imagem pronta' })),
        h('div', { class: 'dialog__body' }, h('p', { text: 'Toque em Compartilhar para enviar no WhatsApp, no Instagram ou em outro app.' })),
        h('div', { class: 'dialog__foot' }, h('button', { type: 'submit', value: 'close', class: 'btn btn--ghost', text: 'Fechar' }), share)));
    document.body.append(el);
    confirmBox = { el, file: null };
    share.addEventListener('click', () => {
      const pending = confirmBox.file;
      shareFile(pending).catch((err) => { if (err?.name !== 'AbortError') download(pending); });
      el.close();
    });
  }
  confirmBox.file = file;
  if (!confirmBox.el.open) confirmBox.el.showModal();
}

/**
 * Compartilha a imagem (a promessa do arquivo, começada no toque): abre direto o menu do sistema
 * (WhatsApp, Instagram…), sem salvar antes; sem compartilhamento de arquivo no navegador, baixa a imagem.
 * @param {Promise<File>} pendingFile
 */
export async function shareStandingsImage(pendingFile) {
  let file;
  try {
    file = await pendingFile;
  } catch {
    showToast('Não deu para gerar a imagem agora. Tente de novo.', { kind: 'error' });
    return;
  }
  if (!canShareFile(file)) { download(file); return; }
  if (navigator.userActivation && !navigator.userActivation.isActive) { askToShare(file); return; }
  try {
    await shareFile(file);
  } catch (err) {
    if (err?.name === 'NotAllowedError') askToShare(file);
    else if (err?.name !== 'AbortError') download(file); // desistiu do menu: nada
  }
}
