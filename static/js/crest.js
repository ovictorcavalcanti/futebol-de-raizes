/**
 * Escudos: <img> quando o time tem crest_url; senão, um escudo SVG desenhado
 * com as cores do time (color_primary/color_secondary) e a sigla.
 */
import { s } from './render.js';

const HEX = /^#[0-9a-f]{6}$/i;
const SHIELD = 'M20 2.5 37 7.5V21c0 10.6-6.8 18.6-17 22.5C9.8 39.6 3 31.6 3 21V7.5Z';
let uid = 0;

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Cor de texto legível sobre a cor dada (#RRGGBB): branco ou tinta. */
export function contrastInk(hex) {
  if (!HEX.test(hex || '')) return '#1B1712';
  const l = luminance(hex);
  // contraste com branco vs. com a tinta #1B1712 (luminância ≈ 0.0094)
  return (1.05 / (l + 0.05)) >= ((l + 0.05) / 0.0594) ? '#FFFFFF' : '#1B1712';
}

/** Escudo SVG gerado (sempre disponível, sem rede). */
export function shieldSvg(team, { size = 40, className = '' } = {}) {
  const primary = HEX.test(team?.color_primary || '') ? team.color_primary : '#12306B';
  let secondary = HEX.test(team?.color_secondary || '') ? team.color_secondary : '#FFFFFF';
  if (secondary.toLowerCase() === primary.toLowerCase()) secondary = contrastInk(primary);
  const label = (team?.short_name || team?.name || '?').slice(0, 4).toUpperCase();
  const id = `crest-${++uid}`;
  const svg = s('svg', {
    class: `crest ${className}`.trim(),
    viewBox: '0 0 40 46',
    width: size,
    height: size,
    'aria-hidden': 'true',
    focusable: 'false',
  });
  const clip = s('clipPath', { id }, s('path', { d: SHIELD }));
  const fontSize = label.length >= 4 ? 9.5 : label.length === 3 ? 11.5 : 14;
  svg.append(
    s('defs', null, clip),
    s('g', { 'clip-path': `url(#${id})` },
      s('path', { d: SHIELD, fill: primary }),
      s('path', { d: 'M0 33 40 15v8L0 41Z', fill: secondary }),
      s('path', { d: 'M0 46h40v-3H0Z', fill: secondary, opacity: '0.6' }),
    ),
    s('text', {
      x: '20',
      y: '22.5',
      'text-anchor': 'middle',
      'font-size': fontSize,
      fill: contrastInk(primary),
      stroke: primary,
      'stroke-width': '3',
      'paint-order': 'stroke',
      'stroke-linejoin': 'round',
      'letter-spacing': label.length >= 4 ? '0' : '0.4',
      text: label,
    }),
    s('path', { d: SHIELD, fill: 'none', stroke: 'var(--crest-outline, #1B1712)', 'stroke-width': '2.2', 'stroke-linejoin': 'round' }),
  );
  return svg;
}

/**
 * Escudo do time: <img loading="lazy"> com crest_url (cai para o SVG se falhar) ou SVG gerado.
 * @param {{name?: string, short_name?: string, color_primary?: string, color_secondary?: string, crest_url?: string}} team
 * @param {{size?: number, className?: string}} [opts]
 * @returns {HTMLImageElement|SVGSVGElement}
 */
export function createCrest(team, { size = 40, className = '' } = {}) {
  if (team?.crest_url) {
    const img = document.createElement('img');
    img.className = `crest ${className}`.trim();
    img.src = team.crest_url;
    img.alt = '';
    img.width = size;
    img.height = size;
    img.loading = 'lazy';
    img.decoding = 'async';
    img.addEventListener('error', () => img.replaceWith(shieldSvg(team, { size, className })), { once: true });
    return img;
  }
  return shieldSvg(team, { size, className });
}
