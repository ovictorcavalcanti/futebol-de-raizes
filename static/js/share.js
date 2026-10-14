/**
 * Compartilhar no WhatsApp (docs/FRONTEND.md §Compartilhar).
 *
 * Jogo: mensagem formatada para o WhatsApp (placar em negrito, competição, autores dos gols e o
 * link /?jogo=<id>, que abre a home no dia do jogo com o card aberto) e uma imagem PNG quadrada
 * (escudos, times, placar, autores dos gols) desenhada num canvas, sem servidor. A imagem entra
 * na conversa como arquivo pela folha nativa do celular (Web Share); sem ela (computador), abre o
 * WhatsApp com a mensagem (api.whatsapp.com) e copia a imagem para colar. Nunca há "baixar".
 * A folha nativa só abre no clique do usuário (o Safari exige), então a imagem é desenhada quando
 * a janela abre e o botão chama o share direto.
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

/* --- Imagem ------------------------------------------------------------------------------- */

const SIZE = 1080;
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

function stripe(ctx, y, height) {
  COLORS.stripe.forEach((color, i) => {
    ctx.fillStyle = color;
    ctx.fillRect((SIZE / 4) * i, y, SIZE / 4, height);
  });
}

/**
 * Imagem do jogo (PNG 1080 × 1080): competição e situação, escudos, times, placar e autores dos
 * gols (só eles, todos no mesmo tamanho), com o nome do site no rodapé.
 * @returns {Promise<Blob>}
 */
export async function renderMatchImage(match, { now = Date.now(), brand = '', host = globalThis.location?.host || '' } = {}) {
  const root = getComputedStyle(document.documentElement);
  const font = {
    brand: root.getPropertyValue('--font-brand').trim() || 'Georgia, serif',
    ui: root.getPropertyValue('--font-ui').trim() || 'system-ui, sans-serif',
    num: root.getPropertyValue('--font-num').trim() || 'system-ui, sans-serif',
  };
  try {
    await Promise.all([`400 40px ${font.brand}`, `700 40px ${font.num}`, `400 40px ${font.ui}`, `600 40px ${font.ui}`].map((f) => document.fonts.load(f)));
  } catch {
    // sem as fontes do site, o canvas usa as do sistema
  }
  const [homeCrest, awayCrest] = await Promise.all([crestImage(match.home), crestImage(match.away)]);

  const canvas = document.createElement('canvas');
  canvas.width = SIZE;
  canvas.height = SIZE;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = COLORS.paper;
  ctx.fillRect(0, 0, SIZE, SIZE);
  stripe(ctx, 0, 18);
  stripe(ctx, SIZE - 18, 18);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  const line = (text, x, y, maxWidth) => ctx.fillText(clip(ctx, text, maxWidth), x, y);

  // competição; rodada e situação
  const competition = match.competition?.name || '';
  ctx.fillStyle = COLORS.ink;
  groupFont(ctx, [competition], { size: 52, family: font.brand, maxWidth: 960 });
  line(competition, SIZE / 2, 112, 960);
  const context = [match.round?.name, situation(match, now)].filter(Boolean).join(' · ').toUpperCase();
  ctx.fillStyle = COLORS.ink3;
  groupFont(ctx, [context], { weight: 700, size: 36, family: font.num, maxWidth: 960 });
  line(context, SIZE / 2, 166, 960);

  // escudos e nomes (os dois do mesmo tamanho)
  drawCrest(ctx, homeCrest, 250, 360, 250);
  drawCrest(ctx, awayCrest, SIZE - 250, 360, 250);
  const names = [name(match.home).toUpperCase(), name(match.away).toUpperCase()];
  ctx.fillStyle = COLORS.ink;
  groupFont(ctx, names, { weight: 700, size: 44, family: font.num, maxWidth: 400, min: 26 });
  line(names[0], 250, 552, 400);
  line(names[1], SIZE - 250, 552, 400);

  // placar (ou "×" com o horário, antes do jogo)
  if (hasScore(match)) {
    ctx.font = `400 150px ${font.brand}`;
    ctx.textAlign = 'right';
    ctx.fillText(String(match.home_score ?? 0), SIZE / 2 - 46, 412);
    ctx.textAlign = 'left';
    ctx.fillText(String(match.away_score ?? 0), SIZE / 2 + 46, 412);
    ctx.textAlign = 'center';
    ctx.fillStyle = COLORS.ink3;
    ctx.font = `400 64px ${font.brand}`;
    ctx.fillText('×', SIZE / 2, 392);
  } else {
    ctx.fillStyle = COLORS.ink3;
    ctx.font = `400 96px ${font.brand}`;
    ctx.fillText('×', SIZE / 2, 400);
  }
  ctx.fillStyle = COLORS.ink3;
  const under = match.home_penalties != null && match.away_penalties != null
    ? `Pênaltis ${match.home_penalties} × ${match.away_penalties}` : walkoverText(match);
  if (under) {
    groupFont(ctx, [under], { weight: 600, size: 32, family: font.ui, maxWidth: 300 });
    line(under, SIZE / 2, 470, 300);
  }

  // autores dos gols, cada lado embaixo do seu escudo, todos no mesmo tamanho (até 6 linhas por lado)
  ctx.strokeStyle = COLORS.line;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(80, 612);
  ctx.lineTo(SIZE - 80, 612);
  ctx.stroke();
  const columns = [['home', 250], ['away', SIZE - 250]].map(([side, x]) => {
    const goals = scorers(match, side);
    return [goals.length > 6 ? [...goals.slice(0, 5), `+${goals.length - 5} gols`] : goals, x];
  });
  ctx.fillStyle = COLORS.ink;
  groupFont(ctx, columns.flatMap(([texts]) => texts), { weight: 600, size: 34, family: font.ui, maxWidth: 440, min: 24 });
  for (const [texts, x] of columns) texts.forEach((text, i) => line(text, x, 676 + i * 50, 440));

  // rodapé: site
  ctx.fillStyle = COLORS.azul;
  groupFont(ctx, [brand], { size: 34, family: font.brand, maxWidth: 900 });
  line(brand, SIZE / 2, 1000, 900);
  ctx.fillStyle = COLORS.ink3;
  groupFont(ctx, [host], { weight: 600, size: 26, family: font.ui, maxWidth: 900 });
  line(host, SIZE / 2, 1040, 900);

  return new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('toBlob'))), 'image/png'));
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
 * Abre a janela de compartilhar o jogo: a prévia da imagem e "Compartilhar no WhatsApp".
 * Com Web Share de arquivos (celular), a imagem vai como arquivo com a mensagem na legenda;
 * sem ela, o botão é o link do WhatsApp com a mensagem e copia a imagem para colar.
 * @param {object} match MatchOut
 * @param {{now?: number, nav?: Navigator}} [opts]
 * @returns {HTMLDialogElement}
 */
export function openMatchShare(match, { now = Date.now(), nav = globalThis.navigator } = {}) {
  const brand = document.querySelector('meta[name="application-name"]')?.content || '';
  const title = scoreline(match);
  const text = matchShareText(match, now, matchUrl(match));
  const filename = `jogo-${match.id}.png`;
  const probe = typeof File === 'function' ? new File([''], filename, { type: 'image/png' }) : null;
  const canShareFiles = !!(nav?.share && nav.canShare && probe && nav.canShare({ files: [probe] }));

  const preview = h('img', { class: 'share-sheet__img', alt: `Imagem do jogo: ${title}`, width: 540, height: 540, hidden: true });
  const status = h('p', { class: 'share-sheet__status', text: 'Gerando a imagem…' });
  const label = [icon('share'), 'Compartilhar no WhatsApp'];
  const send = canShareFiles
    ? h('button', { type: 'button', class: 'btn btn--primary', disabled: true, 'data-hook': 'share-whatsapp' }, ...label)
    : h('a', { class: 'btn btn--primary', href: whatsappUrl(text), target: '_blank', rel: 'noopener', 'data-hook': 'share-whatsapp' }, ...label);
  const dialog = h('dialog', { class: 'dialog share-sheet', 'aria-labelledby': 'share-title' },
    h('form', { method: 'dialog', class: 'dialog__form' },
      h('div', { class: 'dialog__head' }, h('h2', { id: 'share-title', class: 'dialog__title', text: 'Compartilhar jogo' })),
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

  renderMatchImage(match, { now, brand }).then((png) => {
    blob = png;
    objectUrl = URL.createObjectURL(png);
    preview.src = objectUrl;
    preview.hidden = false;
    status.hidden = true;
    send.disabled = false;
  }, () => {
    status.textContent = 'Não deu para gerar a imagem. A mensagem continua valendo.';
    if (canShareFiles) send.replaceWith(h('a', { class: 'btn btn--primary', href: whatsappUrl(text), target: '_blank', rel: 'noopener', 'data-hook': 'share-whatsapp' }, ...label));
  });
  return dialog;
}
