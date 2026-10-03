/**
 * Ícones do sprite (templates/partials/icons.svg, embutido em base.html).
 */
import { svgUse } from './render.js';

/** Todos os ícones do sprite (para o guia de estilo). */
export const ICONS = Object.freeze([
  'ball', 'ball-penalty', 'ball-own', 'ball-x', 'whistle', 'stadium', 'pin', 'tv', 'radio', 'people', 'user',
  'card', 'card-yellow', 'card-red', 'card-second-yellow', 'sub', 'arrow-up', 'arrow-down', 'var', 'clock',
  'bell', 'bell-off', 'speaker', 'speaker-off', 'sun', 'moon', 'chevron-left', 'chevron-right', 'chevron-down',
  'chevron-up', 'external', 'star', 'trophy', 'shield', 'calendar', 'logout', 'lock', 'x-circle', 'target',
  'flag', 'pause', 'play', 'check', 'close', 'plus', 'undo', 'alert', 'info', 'refresh', 'list', 'shirt',
  'clipboard', 'chart', 'menu', 'live', 'umbrella',
]);

/** Ícone padrão de cada tipo de evento (espelha matches.domain.CATALOG). */
export const EVENT_ICONS = Object.freeze({
  match_start: 'whistle',
  half_time: 'whistle',
  second_half_start: 'whistle',
  extra_time_start: 'whistle',
  penalties_start: 'ball-penalty',
  match_end: 'flag',
  goal: 'ball',
  goal_annulled: 'ball-x',
  penalty_awarded: 'ball-penalty',
  penalty_missed: 'x-circle',
  var_review: 'var',
  substitution: 'sub',
  yellow_card: 'card-yellow',
  red_card: 'card-red',
  stoppage_time: 'clock',
  shootout_kick: 'target',
  postponed: 'calendar',
  suspended: 'pause',
  resumed: 'play',
  rescheduled: 'calendar',
  cancelled: 'x-circle',
});

/**
 * <svg class="icon"><use href="#i-name"/></svg>
 * @param {string} name nome do ícone (sem o prefixo "i-")
 * @param {{className?: string, label?: string}} [opts] label torna o ícone significativo (role="img")
 * @returns {SVGSVGElement}
 */
export function icon(name, { className = '', label = '' } = {}) {
  const svg = svgUse(`#i-${name}`, { class: `icon ${className}`.trim() });
  if (label) {
    svg.removeAttribute('aria-hidden');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
  }
  return svg;
}

/**
 * Nome do ícone de um evento, com as variações do payload (como domain.event_icon):
 * gol de pênalti/contra, vermelho do 2º amarelo, cobrança perdida.
 * @param {{type: string, payload?: object, icon?: string}} event
 */
export function eventIconName(event) {
  if (event.icon) return event.icon;
  const p = event.payload || {};
  if (event.type === 'goal') {
    if (p.origin === 'penalty') return 'ball-penalty';
    if (p.origin === 'own_goal') return 'ball-own';
  } else if (event.type === 'red_card' && p.reason === 'second_yellow') {
    return 'card-second-yellow';
  } else if (event.type === 'shootout_kick' && p.scored === false) {
    return 'x-circle';
  }
  return EVENT_ICONS[event.type] || 'info';
}
